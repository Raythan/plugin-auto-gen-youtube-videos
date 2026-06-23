"""Geração de imagens IA com cadeia multi-provedor, rotação de chaves e retry progressivo.

Ordem de tentativa por cena (padrão):
  pollinations → huggingface → deepai → fal

Por cena, o gerador tenta a cadeia completa até obter uma imagem válida.
Se nenhum provedor tiver sucesso, aguarda um intervalo crescente e repete,
até atingir ``max_attempts_per_scene`` (config ``image_gen.max_attempts_per_scene``,
padrão 8).  Não existe fallback local — o pipeline aguarda as APIs.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import requests

from src.models import VisualScene
from src.utils.api_keys_pool import (
    looks_like_fatal_provider_lock,
    looks_like_quota_or_rate_limit,
    looks_like_transient_queue_full,
    resolve_provider_keys,
)
from src.utils.config import ProjectConfig

LOGGER = logging.getLogger(__name__)

# Content-Types aceitos como imagem válida
_IMAGE_CONTENT_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
}

# Magic bytes que identificam formatos de imagem comuns
_IMAGE_MAGIC: list[bytes] = [
    b"\x89PNG",       # PNG
    b"\xff\xd8\xff",  # JPEG
    b"RIFF",          # WebP (RIFF....WEBP)
    b"GIF8",          # GIF
]


def _is_image_bytes(data: bytes) -> bool:
    """True se os primeiros bytes indicam um arquivo de imagem real."""
    if not data or len(data) < 8:
        return False
    for magic in _IMAGE_MAGIC:
        if data[: len(magic)] == magic:
            return True
    # WebP: RIFF????WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


class ImageProvider(str, Enum):
    POLLINATIONS = "pollinations"
    HUGGINGFACE = "huggingface"
    DEEPAI = "deepai"
    FAL = "fal"


class ImageGenerator:
    def __init__(self, root: Path, config: ProjectConfig) -> None:
        self.root = root
        self.config = config
        self._img_cfg: dict[str, Any] = config.pipeline.get("image_gen", {}) or {}
        self._timeout = int(self._img_cfg.get("timeout_seconds", 60))
        self._pollinations_timeout = int(
            self._img_cfg.get("pollinations_timeout_seconds", self._timeout)
        )
        self._pollinations_queue_wait = float(
            self._img_cfg.get("pollinations_queue_full_wait_seconds", 180)
        )
        self._pollinations_queue_retries = int(
            self._img_cfg.get("pollinations_queue_full_max_retries", 6)
        )
        self._width = int(self._img_cfg.get("width", 1080))
        self._height = int(self._img_cfg.get("height", 1920))
        self._min_gap = float(self._img_cfg.get("min_seconds_between_requests", 16))
        self._max_attempts = int(self._img_cfg.get("max_attempts_per_scene", 8))

        raw_providers = self._img_cfg.get("providers") or [p.value for p in ImageProvider]
        self._providers: list[ImageProvider] = []
        for p in raw_providers:
            try:
                self._providers.append(ImageProvider(str(p).lower()))
            except ValueError:
                LOGGER.warning("Provedor de imagem desconhecido ignorado: %r", p)
        if not self._providers:
            self._providers = list(ImageProvider)

        self._provider_keys: dict[str, list[str]] = {
            pid: resolve_provider_keys(pid, root, config)
            for pid in ("huggingface", "deepai", "fal", "pollinations")
        }
        self._key_index: dict[str, int] = {pid: 0 for pid in self._provider_keys}
        # Provedores com erro fatal (saldo, lock, plano) — ignorados até fim da sessão.
        self._skipped_providers: set[str] = set()

        # Throttle global (entre cenas) e por-provedor (evita rate-limit por IP)
        self._last_request_time: float = 0.0
        self._provider_last_request: dict[str, float] = {}
        # Pollinations sem auth: ~1 req/min por IP
        self._provider_min_gap: dict[str, float] = {
            "pollinations": float(self._img_cfg.get("pollinations_min_gap_seconds", 70)),
            "huggingface": float(self._img_cfg.get("huggingface_min_gap_seconds", 5)),
            "deepai": float(self._img_cfg.get("deepai_min_gap_seconds", 3)),
            "fal": float(self._img_cfg.get("fal_min_gap_seconds", 3)),
        }

        for pid, keys in self._provider_keys.items():
            if keys:
                LOGGER.info("image_gen/%s: pool com %d chave(s).", pid, len(keys))
            else:
                LOGGER.warning(
                    "image_gen/%s: sem chaves — provedor so sera usado se permitir anonimo.",
                    pid,
                )
        LOGGER.info(
            "image_gen: ordem de provedores: %s",
            " → ".join(p.value for p in self._providers),
        )

    # ------------------------------------------------------------------
    # Throttle helpers
    # ------------------------------------------------------------------

    def _next_key_round_robin(self, provider_id: str) -> list[str]:
        """Retorna chaves a partir do próximo índice round-robin (tenta todas)."""
        pool = self._provider_keys.get(provider_id, [])
        if not pool:
            return []
        start = self._key_index.get(provider_id, 0) % len(pool)
        self._key_index[provider_id] = start + 1
        return pool[start:] + pool[:start]

    def _throttle_global(self) -> None:
        """Garante espaçamento mínimo global entre qualquer request."""
        elapsed = time.monotonic() - self._last_request_time
        wait = self._min_gap - elapsed
        if wait > 0:
            LOGGER.debug("Throttle global: aguardando %.1fs.", wait)
            time.sleep(wait)
        self._last_request_time = time.monotonic()

    def _throttle_provider(self, provider_id: str) -> None:
        """Garante espaçamento mínimo por provedor (evita rate-limit por IP)."""
        gap = self._provider_min_gap.get(provider_id, 0)
        if gap <= 0:
            return
        last = self._provider_last_request.get(provider_id, 0.0)
        elapsed = time.monotonic() - last
        wait = gap - elapsed
        if wait > 0:
            LOGGER.info(
                "image_gen/%s: rate-limit preventivo — aguardando %.0fs.",
                provider_id,
                wait,
            )
            time.sleep(wait)
        self._provider_last_request[provider_id] = time.monotonic()

    # ------------------------------------------------------------------
    # Key rotation helper
    # ------------------------------------------------------------------

    def _mark_provider_skipped(self, provider_id: str, status: int | None, body: str) -> None:
        if looks_like_fatal_provider_lock(status, body):
            if provider_id not in self._skipped_providers:
                LOGGER.warning(
                    "image_gen/%s: conta bloqueada ou sem saldo — ignorando ate fim da geracao.",
                    provider_id,
                )
                self._skipped_providers.add(provider_id)

    def _try_with_key_rotation(
        self,
        provider_id: str,
        request_fn: Callable[[str], tuple[bool, int | None, str]],
    ) -> tuple[bool, str]:
        if provider_id in self._skipped_providers:
            LOGGER.debug("image_gen/%s: ignorado (bloqueado nesta sessao).", provider_id)
            return False, ""
        keys = self._next_key_round_robin(provider_id)
        if not keys:
            LOGGER.debug("image_gen/%s: sem chaves configuradas.", provider_id)
            return False, ""
        last_status: int | None = None
        last_body = ""
        for key_index, api_key in enumerate(keys, start=1):
            ok, status, body = request_fn(api_key)
            last_status, last_body = status, body
            if ok:
                return True, ""
            LOGGER.warning(
                "image_gen/%s chave #%d: HTTP %s — %.120s",
                provider_id,
                key_index,
                status,
                body.strip(),
            )
            if looks_like_transient_queue_full(status, body):
                break
            self._mark_provider_skipped(provider_id, status, body)
            if looks_like_quota_or_rate_limit(status, body) and key_index < len(keys):
                LOGGER.info(
                    "image_gen/%s: chave #%d quota/rate-limit; tentando proxima chave.",
                    provider_id,
                    key_index,
                )
                continue
            # Para erros que não são quota, tentar outra chave ainda pode ajudar
            # (ex: chave inválida de uma conta, mas outra conta OK)
            if key_index < len(keys):
                continue
        if not looks_like_transient_queue_full(last_status, last_body):
            self._mark_provider_skipped(provider_id, last_status, last_body)
        LOGGER.warning(
            "image_gen/%s: todas as %d chave(s) falharam (ultimo HTTP %s).",
            provider_id,
            len(keys),
            last_status,
        )
        return False, last_body

    # ------------------------------------------------------------------
    # Provider implementations
    # ------------------------------------------------------------------

    def _try_pollinations(self, prompt: str, output_path: Path, seed: int) -> bool:
        self._throttle_provider("pollinations")
        encoded = urllib.parse.quote(prompt, safe="")
        base_url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            f"?width={self._width}&height={self._height}"
            f"&model=flux&nologo=true&nofeed=true&seed={seed}"
        )

        def _request(api_key: str) -> tuple[bool, int | None, str]:
            url = base_url
            if api_key.strip():
                url = f"{base_url}&token={urllib.parse.quote(api_key.strip(), safe='')}"
            try:
                resp = requests.get(url, timeout=self._pollinations_timeout, stream=False)
                ct = resp.headers.get("Content-Type", "")
                body_excerpt = resp.text[:300] if not resp.content[:4] else ""
                if resp.status_code == 200:
                    if _is_image_bytes(resp.content):
                        output_path.write_bytes(resp.content)
                        return True, resp.status_code, ""
                    # 200 mas não é imagem (rate-limit page, HTML erro, etc.)
                    LOGGER.warning(
                        "image_gen/pollinations: 200 mas conteudo nao e imagem "
                        "(Content-Type=%r, primeiros bytes=%r).",
                        ct,
                        resp.content[:32],
                    )
                    return False, 429, "content-type invalido; provavelmente rate-limit"
                return False, resp.status_code, resp.text[:300]
            except requests.RequestException as exc:
                return False, None, str(exc)

        def _wait_pollinations_queue(attempt: int, body: str) -> None:
            if not looks_like_transient_queue_full(402, body):
                return
            # Backoff leve: a geracao Flux pode levar varios minutos na fila gratuita.
            wait = self._pollinations_queue_wait * (1.0 + 0.25 * attempt)
            LOGGER.info(
                "image_gen/pollinations: fila cheia (402); aguardando %.0fs "
                "(tentativa fila %d/%d, timeout HTTP=%ds).",
                wait,
                attempt + 1,
                self._pollinations_queue_retries,
                self._pollinations_timeout,
            )
            time.sleep(wait)

        def _pollinations_with_queue_retries(
            attempt_fn: Callable[[], tuple[bool, int | None, str]],
        ) -> tuple[bool, int | None, str]:
            ok = False
            status: int | None = None
            body = ""
            for queue_attempt in range(self._pollinations_queue_retries):
                ok, status, body = attempt_fn()
                if ok:
                    return True, status, body
                if not looks_like_transient_queue_full(status, body):
                    return False, status, body
                if queue_attempt < self._pollinations_queue_retries - 1:
                    _wait_pollinations_queue(queue_attempt, body)
            return ok, status, body

        keys = self._provider_keys.get("pollinations", [])
        if keys:
            def _attempt_with_keys() -> tuple[bool, int | None, str]:
                key_ok, key_body = self._try_with_key_rotation("pollinations", _request)
                if key_ok:
                    return True, 200, ""
                return False, 402 if looks_like_transient_queue_full(402, key_body) else None, key_body

            ok, status, body = _pollinations_with_queue_retries(_attempt_with_keys)
            if not ok and not looks_like_transient_queue_full(status, body):
                self._mark_provider_skipped("pollinations", status, body)
            return ok

        ok, status, body = _pollinations_with_queue_retries(lambda: _request(""))
        if not ok:
            if not looks_like_transient_queue_full(status, body):
                self._mark_provider_skipped("pollinations", status, body)
            LOGGER.warning(
                "image_gen/pollinations (sem chave): HTTP %s — %.120s",
                status,
                body.strip(),
            )
        return ok

    def _try_huggingface(self, prompt: str, output_path: Path) -> bool:
        self._throttle_provider("huggingface")
        url = (
            "https://router.huggingface.co/hf-inference/models/"
            "black-forest-labs/FLUX.1-schnell"
        )
        payload = {"inputs": prompt, "parameters": {"width": self._width, "height": self._height}}

        def _request(api_key: str) -> tuple[bool, int | None, str]:
            headers = {"Authorization": f"Bearer {api_key}"}
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self._timeout)
                body = (resp.text or "")[:400]
                if resp.status_code == 200 and resp.content:
                    if _is_image_bytes(resp.content):
                        output_path.write_bytes(resp.content)
                        return True, resp.status_code, ""
                    return False, resp.status_code, f"conteudo nao e imagem: {body}"
                if resp.status_code == 503 and "loading" in body.lower():
                    # Modelo ainda carregando — sinaliza como rate-limit para retry
                    est = None
                    try:
                        est = resp.json().get("estimated_time")
                    except Exception:
                        pass
                    wait_secs = min(float(est or 30), 60)
                    LOGGER.info(
                        "image_gen/huggingface: modelo carregando (503); "
                        "aguardando %.0fs antes de nova tentativa.",
                        wait_secs,
                    )
                    time.sleep(wait_secs)
                    # Tenta uma vez mais com o mesmo key
                    resp2 = requests.post(url, headers=headers, json=payload, timeout=self._timeout)
                    body2 = (resp2.text or "")[:400]
                    if resp2.status_code == 200 and _is_image_bytes(resp2.content):
                        output_path.write_bytes(resp2.content)
                        return True, resp2.status_code, ""
                    return False, resp2.status_code, body2
                return False, resp.status_code, body
            except requests.RequestException as exc:
                return False, None, str(exc)

        ok, _ = self._try_with_key_rotation("huggingface", _request)
        return ok

    def _try_deepai(self, prompt: str, output_path: Path) -> bool:
        self._throttle_provider("deepai")

        def _request(api_key: str) -> tuple[bool, int | None, str]:
            try:
                resp = requests.post(
                    "https://api.deepai.org/api/text2img",
                    data={"text": prompt, "grid_size": "1", "width": str(self._width), "height": str(self._height)},
                    headers={"api-key": api_key},
                    timeout=self._timeout,
                )
                body = (resp.text or "")[:400]
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        return False, resp.status_code, f"JSON invalido: {body}"
                    img_url = data.get("output_url")
                    if img_url:
                        img_resp = requests.get(img_url, timeout=self._timeout)
                        if img_resp.status_code == 200 and _is_image_bytes(img_resp.content):
                            output_path.write_bytes(img_resp.content)
                            return True, resp.status_code, ""
                        return False, img_resp.status_code, f"download falhou: {img_resp.text[:200]}"
                    return False, resp.status_code, f"output_url ausente: {body}"
                return False, resp.status_code, body
            except requests.RequestException as exc:
                return False, None, str(exc)

        ok, _ = self._try_with_key_rotation("deepai", _request)
        return ok

    def _try_fal(self, prompt: str, output_path: Path) -> bool:
        self._throttle_provider("fal")
        body = {
            "prompt": prompt,
            "image_size": {"width": self._width, "height": self._height},
            "num_inference_steps": 4,
            "num_images": 1,
        }

        def _request(api_key: str) -> tuple[bool, int | None, str]:
            headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
            try:
                resp = requests.post(
                    "https://fal.run/fal-ai/flux/schnell",
                    json=body,
                    headers=headers,
                    timeout=self._timeout,
                )
                text = (resp.text or "")[:400]
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        return False, resp.status_code, f"JSON invalido: {text}"
                    images = data.get("images") or []
                    if images:
                        url = images[0].get("url")
                        if url:
                            img_resp = requests.get(url, timeout=self._timeout)
                            if img_resp.status_code == 200 and _is_image_bytes(img_resp.content):
                                output_path.write_bytes(img_resp.content)
                                return True, resp.status_code, ""
                            return False, img_resp.status_code, f"download falhou: {img_resp.text[:200]}"
                    return False, resp.status_code, f"images vazio: {text}"
                return False, resp.status_code, text
            except requests.RequestException as exc:
                return False, None, str(exc)

        ok, _ = self._try_with_key_rotation("fal", _request)
        return ok

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    def _generate_one(self, prompt: str, output_path: Path, index: int) -> bool:
        """Tenta cada provedor em sequência; retorna True se algum gerar imagem válida."""
        self._throttle_global()
        seed = (hash(prompt) + index) & 0x7FFFFFFF
        for provider in self._providers:
            if provider.value in self._skipped_providers:
                LOGGER.debug(
                    "image_gen/%s: ignorado (bloqueado nesta sessao).",
                    provider.value,
                )
                continue
            ok = False
            try:
                if provider == ImageProvider.POLLINATIONS:
                    ok = self._try_pollinations(prompt, output_path, seed)
                elif provider == ImageProvider.HUGGINGFACE:
                    ok = self._try_huggingface(prompt, output_path)
                elif provider == ImageProvider.DEEPAI:
                    ok = self._try_deepai(prompt, output_path)
                elif provider == ImageProvider.FAL:
                    ok = self._try_fal(prompt, output_path)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "image_gen/%s: erro inesperado na cena %d: %s",
                    provider.value,
                    index,
                    exc,
                )
            if ok:
                LOGGER.info("Imagem %d gerada via %s: %s", index, provider.value, output_path.name)
                return True
            LOGGER.debug(
                "image_gen/%s: nao gerou imagem para cena %d; tentando proximo provedor.",
                provider.value,
                index,
            )
        return False

    def generate_for_scenes(
        self, scenes: list[VisualScene], output_dir: Path, news_id: str
    ) -> list[Path]:
        """Gera imagens para cada cena; retorna lista de caminhos com sucesso.

        Estratégia de resiliência (sem fallback local):
        - Cada cena tem até ``max_attempts_per_scene`` rodadas completas pela cadeia.
        - Entre rodadas falhadas: delay progressivo (30s × attempt, máx 120s).
        - Se após todas as tentativas ainda falhar, a cena é pulada com WARNING.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []

        for i, scene in enumerate(scenes):
            output_path = output_dir / f"{news_id}_{i:02d}.png"
            success = False

            for attempt in range(self._max_attempts):
                if attempt > 0:
                    delay = min(30 * attempt, 120)
                    LOGGER.info(
                        "Cena %d: tentativa %d/%d — aguardando %ds antes de nova rodada.",
                        i,
                        attempt + 1,
                        self._max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                    if output_path.exists():
                        output_path.unlink(missing_ok=True)

                # Varia o seed para evitar cache de erro em provedores com cache de prompt
                seed_offset = attempt * 500
                ok = self._generate_one(scene.prompt_en, output_path, i + seed_offset)
                if ok and output_path.is_file() and output_path.stat().st_size > 0:
                    success = True
                    break

                if attempt < self._max_attempts - 1:
                    LOGGER.warning(
                        "Cena %d tentativa %d/%d: todos os provedores falharam. Vai tentar novamente.",
                        i,
                        attempt + 1,
                        self._max_attempts,
                    )

            if not success:
                LOGGER.error(
                    "Cena %d: FALHOU apos %d tentativas (prompt: %.80s...). Slide pulado.",
                    i,
                    self._max_attempts,
                    scene.prompt_en,
                )
                if output_path.exists():
                    output_path.unlink(missing_ok=True)
            else:
                results.append(output_path)

        LOGGER.info(
            "Geracao de imagens concluida: %d/%d cenas com sucesso.",
            len(results),
            len(scenes),
        )
        return results
