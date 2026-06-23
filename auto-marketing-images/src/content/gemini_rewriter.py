from __future__ import annotations

import html
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import math

from src.models import NewsItem, ScriptResult, VisualScene
from src.utils.config import ProjectConfig, resolve_script_target
from src.utils.gemini_keys import resolve_gemini_keys
from src.utils.state_store import StateStore

LOGGER = logging.getLogger(__name__)



_QUOTA_HINTS = (
    "quota",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "too many requests",
    "429",
)

# Process-wide pacing state. Each call to ``run_generate_only`` creates a fresh
# ``GeminiRewriter`` instance, so we keep the throttle here so the spacing and
# cooldown survive across invocations within the long-running console.
_LAST_REQUEST_MONOTONIC_BY_CHANNEL: dict[str, float] = {}
_COOLDOWN_UNTIL_MONOTONIC_BY_CHANNEL: dict[str, float] = {}

# Captures most URLs (http/https/www.something) so we can strip them out of spoken text.
_URL_PATTERN = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_MULTI_WS_PATTERN = re.compile(r"\s+")


def _tts_spoken_script_rules() -> list[str]:
    """Extra Gemini constraints so Edge TTS reads the script more naturally."""
    return [
        "- Frases curtas (ideal 8 a 18 palavras); evite periodos com mais de duas virgulas.",
        "- Use pontuacao (. ? !) para pausas; nao abuse de reticencias nem exclamacoes em sequencia.",
        "- Numeros e siglas por extenso quando possivel (ex.: 'dois mil e vinte' em vez de '2020' cru).",
        "- Nomes estrangeiros e de anime: grafia simples em portugues ou forma facil de ler em voz alta.",
        "- Evite parenteses, barras, aspas aninhadas e listas; prefira texto que alguem falaria numa respiracao.",
    ]


def _gemini_cli_subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Env vars for headless/VM runs of the Gemini CLI (trusted workspace)."""
    env = dict(base or os.environ)
    env.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")
    return env


def _looks_like_trust_workspace_error(output: str) -> bool:
    lowered = output.lower()
    return (
        "trusted directory" in lowered
        or "skip-trust" in lowered
        or "gemini_cli_trust_workspace" in lowered
    )


def _effective_gemini_timeout_seconds(pipeline_cfg: dict[str, Any]) -> int:
    """Seconds for ``subprocess.run`` around the Gemini CLI; YAML default with env override."""
    try:
        base = int(pipeline_cfg.get("timeout_seconds", 120))
    except (TypeError, ValueError):
        base = 120
    raw_env = os.environ.get("PIPELINE_GEMINI_TIMEOUT_SECONDS")
    if raw_env is None or not str(raw_env).strip():
        return max(1, base)
    try:
        override = int(str(raw_env).strip())
    except ValueError:
        return max(1, base)
    return max(1, override)


def _argv_for_path(path: Path) -> list[str]:
    """Build the subprocess argv for a resolved Gemini CLI shim path.

    PowerShell scripts (``.ps1``) cannot be executed directly by ``subprocess.run``
    on Windows; they have to be invoked through ``powershell -File``.
    """
    if path.suffix.lower() == ".ps1":
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ]
    return [str(path)]


def _resolve_gemini_command(command: str) -> list[str]:
    """Resolve the configured Gemini CLI command to a runnable argv list.

    On Windows, ``npm i -g @google/gemini-cli`` only installs ``gemini.cmd`` and
    ``gemini.ps1`` shims (no native ``.exe``). ``subprocess.run`` does not honor
    ``PATHEXT`` when given an argv list, so a bare ``["gemini", ...]`` fails with
    ``[WinError 2]`` even when PowerShell finds the CLI fine. We try, in order:

    1. ``command`` as an absolute path that exists on disk.
    2. ``shutil.which(command)`` (respects ``PATHEXT`` on Windows so it picks
       up ``gemini.cmd``/``.exe`` automatically).
    3. Windows-only fallbacks under common npm prefixes
       (``%APPDATA%\\npm``, ``%LOCALAPPDATA%\\npm``, ``%USERPROFILE%\\.npm-global``)
       trying ``.cmd``, ``.exe`` and ``.ps1`` in that order.

    Raises ``FileNotFoundError`` with installation guidance if nothing is found.
    """
    raw = (command or "").strip()
    if not raw:
        raise FileNotFoundError("Gemini command is empty in pipeline config.")

    candidate = Path(raw)
    if candidate.is_absolute() and candidate.is_file():
        return _argv_for_path(candidate)

    located = shutil.which(raw)
    if located:
        return _argv_for_path(Path(located))

    tried: list[Path] = []
    stem = Path(raw).name
    if "." in stem:
        stem = Path(stem).stem

    if os.name == "nt":
        bases: list[Path] = []
        for env_name, sub in (
            ("APPDATA", "npm"),
            ("LOCALAPPDATA", "npm"),
            ("USERPROFILE", ".npm-global"),
        ):
            value = os.environ.get(env_name)
            if value:
                bases.append(Path(value) / sub)

        for base in bases:
            for ext in (".cmd", ".exe", ".ps1"):
                probe = base / f"{stem}{ext}"
                tried.append(probe)
                if probe.is_file():
                    return _argv_for_path(probe)
    else:
        for probe in (
            Path.home() / ".npm-global" / "bin" / stem,
            Path.home() / ".local" / "bin" / stem,
        ):
            tried.append(probe)
            if probe.is_file():
                return _argv_for_path(probe)

    hint_parts = [
        f"Gemini CLI not found (looked up {raw!r} via PATH",
    ]
    if tried:
        hint_parts.append(
            f" and tried Windows fallbacks: {', '.join(str(p) for p in tried)}"
        )
    hint_parts.append(
        "). Install with `npm i -g @google/gemini-cli` or set "
        "`gemini.command` to the absolute path in config/local.yaml."
    )
    raise FileNotFoundError("".join(hint_parts))


def sanitize_spoken_text(text: str) -> str:
    """Strip HTML, markdown noise and URLs so the output is safe for TTS/SRT.

    The Gemini CLI sometimes returns markup or links inside ``script_text``;
    those characters end up read aloud or shown literally in subtitles. This
    helper produces a clean, single-spaced spoken version while leaving the
    YouTube body untouched (URLs/hashtags are welcome there).
    """
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = _HTML_TAG_PATTERN.sub(" ", cleaned)
    cleaned = _URL_PATTERN.sub("", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = _MULTI_WS_PATTERN.sub(" ", cleaned).strip()
    return cleaned


def _extract_markdown_json_fence(text: str) -> str | None:
    """Return the first parseable JSON object after a ```json / ``` fence opener."""
    for chunk in _iter_markdown_json_fence_objects(text):
        return chunk
    return None


def _iter_markdown_json_fence_objects(text: str) -> list[str]:
    """Collect JSON object substrings after each markdown fence (brace-balanced, not ``` close)."""
    found: list[str] = []
    seen: set[str] = set()
    lower = text.lower()
    pos = 0
    while pos < len(text):
        json_idx = lower.find("```json", pos)
        plain_idx = lower.find("```", pos)
        if json_idx < 0 and plain_idx < 0:
            break
        if json_idx >= 0 and (plain_idx < 0 or json_idx <= plain_idx):
            needle = "```json"
            idx = json_idx
        else:
            needle = "```"
            idx = plain_idx
        start = idx + len(needle)
        while start < len(text) and text[start] in " \t\r\n":
            start += 1
        chunk = _extract_balanced_json_object(text[start:])
        if chunk and chunk not in seen:
            seen.add(chunk)
            found.append(chunk)
        pos = start + 1
    return found


def _extract_balanced_json_object(text: str) -> str | None:
    """First top-level {...} using brace depth, respecting JSON double-quoted strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _json_candidate_score(data: dict[str, object]) -> int:
    """Prefer payloads with spoken script and richer YouTube fields."""
    score = 0
    for key in ("script_text", "scriptText", "roteiro", "narration"):
        if str(data.get(key, "")).strip():
            score += 100
            break
    for key in ("title", "headline", "titulo"):
        if str(data.get(key, "")).strip():
            score += 10
            break
    for key in ("youtube_body", "youtubeBody", "description", "descricao"):
        if str(data.get(key, "")).strip():
            score += 5
            break
    if data.get("tags"):
        score += 2
    return score


def _parse_json_to_dict(raw: str) -> dict[str, object] | None:
    """Parse Gemini CLI output into a dict (preamble, markdown fences, extra prose)."""
    text = (raw or "").lstrip("\ufeff").strip()
    if not text:
        return None

    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(fragment: str) -> None:
        frag = fragment.strip()
        if frag and frag not in seen:
            seen.add(frag)
            candidates.append(frag)

    add_candidate(text)
    for fenced in _iter_markdown_json_fence_objects(text):
        add_candidate(fenced)
    scan = 0
    while scan < len(text):
        balanced = _extract_balanced_json_object(text[scan:])
        if not balanced:
            break
        add_candidate(balanced)
        next_pos = text.find(balanced, scan)
        if next_pos < 0:
            break
        scan = next_pos + len(balanced)

    best: dict[str, object] | None = None
    best_score = -1
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        score = _json_candidate_score(data)
        if score > best_score:
            best_score = score
            best = data
    return best


def _flatten_wrapped_json_dict(data: dict[str, Any]) -> dict[str, Any]:
    """If the model nests the payload (e.g. response/result/output), merge inner dict keys."""
    merged = dict(data)
    for nest_key in ("response", "result", "output", "data", "json"):
        inner = merged.get(nest_key)
        if isinstance(inner, dict) and inner:
            rest = {k: v for k, v in merged.items() if k != nest_key}
            return {**rest, **inner}
    return merged


def _normalize_model_json_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Map camelCase / alternate keys into title, script_text, youtube_body, tags."""
    lookup: dict[str, Any] = {}
    for key, value in data.items():
        lookup[str(key).strip().lower().replace("-", "_")] = value

    def pick(*aliases: str) -> Any:
        for alias in aliases:
            if alias in lookup:
                return lookup[alias]
        return None

    out: dict[str, Any] = {}
    title = pick("title", "headline", "titulo")
    script = pick(
        "script_text",
        "scripttext",
        "narration",
        "narracao",
        "spoken_text",
        "body",
        "script",
        "roteiro",
        "fala",
        "texto_fala",
    )
    youtube_body = pick("youtube_body", "youtubebody", "description", "descricao")
    tags_raw = pick("tags", "hashtags", "keywords", "palavras_chave")
    if title is not None:
        out["title"] = title
    if script is not None:
        out["script_text"] = script
    if youtube_body is not None:
        out["youtube_body"] = youtube_body
    if tags_raw is not None:
        out["tags"] = tags_raw
    visual_scenes_raw = pick("visual_scenes", "visualscenes", "scenes", "cenas_visuais")
    if visual_scenes_raw is not None:
        out["visual_scenes"] = visual_scenes_raw
    return out


def _coerce_tags_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
        return parts[:30]
    text = str(raw).strip()
    return [text] if text else []


class GeminiRewriter:
    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        *,
        brand: dict[str, Any] | None = None,
        channel_id: str = "default",
        script_target: dict[str, int] | None = None,
        channel_content: dict[str, Any] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        # Effective brand for prompts (channel YAML ``brand:`` merged over pipeline brand).
        self._brand = dict(brand) if brand is not None else dict(config.brand)
        self._channel_id = str(channel_id).strip() or "default"
        self._script_target = (
            dict(script_target)
            if script_target is not None
            else resolve_script_target(config.pipeline)
        )
        # Optional ``content:`` block from the channel YAML (editorial settings).
        self._channel_content: dict[str, Any] = dict(channel_content) if channel_content else {}
        self._keys = resolve_gemini_keys(root, config)
        gemini_cfg = self.config.pipeline.get("gemini", {}) or {}
        rotation = str(gemini_cfg.get("rotation", "round_robin")).strip().lower()
        if rotation not in {"round_robin", "failover"}:
            LOGGER.warning("Unknown gemini.rotation=%r; using round_robin.", rotation)
            rotation = "round_robin"
        self._rotation = rotation
        self._cycle = itertools.cycle(range(len(self._keys))) if self._keys else None
        self._min_seconds_between_requests = max(
            0, int(gemini_cfg.get("min_seconds_between_requests", 30))
        )
        self._cooldown_after_quota = max(
            0, int(gemini_cfg.get("cooldown_after_quota_seconds", 300))
        )
        # Cache the resolved Gemini CLI argv so we only run the lookup (and
        # log the resolved path) once per ``GeminiRewriter`` instance.
        self._resolved_command: list[str] | None = None

    def _resolve_command_cached(self, command: str) -> list[str]:
        if self._resolved_command is not None:
            return self._resolved_command
        argv = _resolve_gemini_command(command)
        LOGGER.info("Resolved Gemini CLI to: %s", " ".join(argv))
        self._resolved_command = argv
        return argv

    def _recent_runs_summary(self, limit: int = 10, max_chars: int = 120) -> str:
        """Build a compact summary of recent runs for the channel to inject into prompts.

        Returns a block like:
            RECENT_VIDEOS:
            - 2026-05-21 — rpjtechgroup_default — 'Titulo do ultimo short...'
            - 2026-05-20 — rpjtechgroup_default — 'Outro titulo recente...'
        """
        state_path = self.root / "data" / "processed" / "state.json"
        store = StateStore(state_path)
        runs = store.recent_runs(channel_id=self._channel_id, limit=limit)
        if not runs:
            return ""
        lines: list[str] = ["RECENT_VIDEOS (nao repetir esses angulos/temas):"]
        for run in runs:
            ts = run.get("timestamp_utc", "")[:10]
            template = run.get("video_template") or ""
            title_raw = run.get("generated_title") or ""
            title_clean = sanitize_spoken_text(title_raw).strip()
            if len(title_clean) > max_chars:
                title_clean = title_clean[: max_chars - 3].rstrip() + "..."
            if title_clean:
                lines.append(f"- {ts} — {template} — '{title_clean}'")
            elif template:
                lines.append(f"- {ts} — {template}")
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def _inject_recent_runs_context(self, prompt_lines: list[str]) -> list[str]:
        """Append the global RECENT_VIDEOS block when enabled and available."""
        gemini_cfg = self.config.pipeline.get("gemini", {}) or {}
        recent_cfg = gemini_cfg.get("recent_runs", {}) or {}
        if not recent_cfg.get("enabled", True):
            return prompt_lines
        recent_limit = int(recent_cfg.get("limit", 20))
        recent_max_chars = int(recent_cfg.get("summary_max_chars", 140))
        recent_block = self._recent_runs_summary(
            limit=recent_limit, max_chars=recent_max_chars
        )
        if recent_block:
            prompt_lines.extend(["", recent_block])
        return prompt_lines

    def _inject_recent_topics_context(self, prompt_lines: list[str]) -> list[str]:
        """Append RECENT_TOPICS block (ai_tools_business freeform mode).

        Lists the last N topics so Gemini picks a genuinely different
        tool / angle. N comes from ``content.topic_buffer_size`` (default 20).
        """
        buffer_size = int(self._channel_content.get("topic_buffer_size", 20))
        state_path = self.root / "data" / "processed" / "state.json"
        store = StateStore(state_path)
        topics = store.recent_topics(channel_id=self._channel_id, limit=buffer_size)
        if not topics:
            return prompt_lines
        lines = [f"RECENT_TOPICS (é PROIBIDO repetir ferramenta ou angulo equivalente — use tema diferente):"]
        for t in topics:
            topic_key = t.get("topic_key") or ""
            title = t.get("title") or ""
            date = t.get("date") or ""
            entry = f"- {date} — {topic_key or title}"
            lines.append(entry)
        prompt_lines.extend(["", "\n".join(lines)])
        return prompt_lines

    def generate_script(self, news_batch: list[NewsItem]) -> ScriptResult:
        if not self._keys:
            LOGGER.warning("No Gemini API key available; using fallback script.")
            return self._fallback(news_batch)

        if not self._wait_until_allowed():
            LOGGER.warning(
                "Gemini cooldown still active; using fallback script for this run."
            )
            return self._fallback(news_batch)

        pipeline_cfg = self.config.pipeline.get("gemini", {}) or {}
        command_setting = str(pipeline_cfg.get("command", "gemini"))
        extra_args = list(pipeline_cfg.get("args", ["generate"]))
        timeout_seconds = _effective_gemini_timeout_seconds(pipeline_cfg)

        try:
            argv_prefix = self._resolve_command_cached(command_setting)
        except FileNotFoundError as exc:
            # CLI is missing on this machine — rotating keys would not help
            # (every key would hit the same WinError 2). Skip rotation and
            # fall back immediately with a clear, actionable message.
            LOGGER.warning(
                "Skipping Gemini CLI rotation: %s. Falling back to local script.",
                exc,
            )
            return self._fallback(news_batch)

        prompt = self._build_prompt(news_batch)
        LOGGER.info(
            "Gemini prompt size: %d chars (~%d tokens rough order-of-magnitude), news_items=%d, keys_in_pool=%d",
            len(prompt),
            max(1, len(prompt) // 4),
            len(news_batch),
            len(self._keys),
        )
        order = self._key_order()
        last_error: Exception | None = None
        quota_exhausted_all = False
        for index in order:
            key = self._keys[index]
            command_env = _gemini_cli_subprocess_env()
            command_env["GEMINI_API_KEY"] = key
            LOGGER.info(
                "Calling Gemini CLI with key #%d (rotation=%s).", index + 1, self._rotation
            )
            self._mark_request_started()
            completed: subprocess.CompletedProcess[str] | None = None
            stop_trying_keys = False
            try_next_key = False
            for attempt in range(2):
                try:
                    completed = subprocess.run(
                        [*argv_prefix, *extra_args],
                        input=prompt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=True,
                        timeout=timeout_seconds,
                        env=command_env,
                    )
                    break
                except subprocess.TimeoutExpired as exc:
                    last_error = exc
                    LOGGER.warning(
                        "Gemini CLI subprocess timed out after %ss with key #%d (attempt %d/2): %s",
                        timeout_seconds,
                        index + 1,
                        attempt + 1,
                        exc,
                    )
                    if attempt == 0:
                        LOGGER.info(
                            "Retrying Gemini CLI once with the same API key (timeout=%ss).",
                            timeout_seconds,
                        )
                        continue
                    LOGGER.warning(
                        "Gemini CLI timed out again after retry; not trying remaining API keys."
                    )
                    stop_trying_keys = True
                    break
                except subprocess.CalledProcessError as exc:
                    last_error = exc
                    output = (exc.stderr or "") + "\n" + (exc.stdout or "")
                    if _looks_like_trust_workspace_error(output):
                        LOGGER.warning(
                            "Gemini CLI requires a trusted workspace in headless mode "
                            "(use --skip-trust or GEMINI_CLI_TRUST_WORKSPACE=true). "
                            "Output: %s",
                            output.strip()[:500],
                        )
                        stop_trying_keys = True
                        break
                    is_quota = self._looks_like_quota_error(output)
                    is_last_key = index == order[-1]
                    if is_quota:
                        if not is_last_key:
                            LOGGER.warning(
                                "Gemini key #%d hit a quota/limit error; trying next key.",
                                index + 1,
                            )
                            try_next_key = True
                            break
                        quota_exhausted_all = True
                        LOGGER.warning(
                            "Gemini key #%d quota/limit hit; no more keys to try.",
                            index + 1,
                        )
                        break
                    LOGGER.warning(
                        "Gemini CLI failed with key #%d (CLI returned non-zero): %s. Output: %s",
                        index + 1,
                        exc,
                        output.strip()[:500],
                    )
                    if not is_last_key:
                        LOGGER.info(
                            "Trying next Gemini key after non-quota CLI error."
                        )
                        try_next_key = True
                        break
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    LOGGER.warning(
                        "Gemini CLI failed with key #%d (unexpected error): %s",
                        index + 1,
                        exc,
                    )
                    if index != order[-1]:
                        LOGGER.info(
                            "Trying next Gemini key after unexpected error."
                        )
                        try_next_key = True
                        break
                    break

            if completed is not None:
                out = completed.stdout or ""
                err = completed.stderr or ""
                LOGGER.info(
                    "Gemini CLI stdout/stderr lengths: %d / %d chars (key #%d).",
                    len(out),
                    len(err),
                    index + 1,
                )
                try:
                    return self._parse_model_response(out, err)
                except (ValueError, TypeError, KeyError) as exc:
                    LOGGER.warning(
                        "Gemini output unusable for ScriptResult (key #%d): %s. Using fallback script.",
                        index + 1,
                        exc,
                    )
                    return self._fallback(news_batch)
            if stop_trying_keys:
                break
            if try_next_key:
                continue
            break

        if quota_exhausted_all:
            self._enter_quota_cooldown()
        LOGGER.warning("Falling back to local script. Last error: %s", last_error)
        return self._fallback(news_batch)

    def _wait_until_allowed(self) -> bool:
        """Sleep to honor min spacing; return False if a quota cooldown is still active."""
        now = time.monotonic()
        cooldown_until = _COOLDOWN_UNTIL_MONOTONIC_BY_CHANNEL.get(self._channel_id, 0.0)
        if cooldown_until > now:
            remaining = int(cooldown_until - now)
            LOGGER.info(
                "Gemini cooldown active for channel=%s (~%ss); skipping request.",
                self._channel_id,
                remaining,
            )
            return False
        if self._min_seconds_between_requests <= 0:
            return True
        last_request = _LAST_REQUEST_MONOTONIC_BY_CHANNEL.get(self._channel_id, 0.0)
        elapsed = now - last_request
        wait_for = self._min_seconds_between_requests - elapsed
        if wait_for > 0:
            LOGGER.info(
                "Throttling Gemini channel=%s: sleeping %.1fs to honor min spacing of %ss.",
                self._channel_id,
                wait_for,
                self._min_seconds_between_requests,
            )
            time.sleep(wait_for)
        return True

    def _mark_request_started(self) -> None:
        _LAST_REQUEST_MONOTONIC_BY_CHANNEL[self._channel_id] = time.monotonic()

    def _enter_quota_cooldown(self) -> None:
        if self._cooldown_after_quota <= 0:
            return
        _COOLDOWN_UNTIL_MONOTONIC_BY_CHANNEL[self._channel_id] = (
            time.monotonic() + self._cooldown_after_quota
        )
        LOGGER.warning(
            "All Gemini keys hit quota/limit for channel=%s; entering cooldown for %ss.",
            self._channel_id,
            self._cooldown_after_quota,
        )

    def generate_fallback_script(self, news_batch: list[NewsItem]) -> ScriptResult:
        return self._fallback(news_batch)

    def _key_order(self) -> list[int]:
        total = len(self._keys)
        if total == 0:
            return []
        if self._rotation == "round_robin" and self._cycle is not None:
            start = next(self._cycle)
        else:
            start = 0
        return [(start + offset) % total for offset in range(total)]

    @staticmethod
    def _looks_like_quota_error(text: str) -> bool:
        if not text:
            return False
        haystack = text.lower()
        return any(hint in haystack for hint in _QUOTA_HINTS)

    def _script_duration_rules(self) -> list[str]:
        t = self._script_target
        return [
            (
                f"Duracao alvo: {t['min_seconds']} a {t['max_seconds']} segundos quando lido em "
                f"ritmo natural (nao ultrapasse {t['max_seconds']} s)."
            ),
            (
                f"Tamanho do script_text: aproximadamente {t['min_words']} a {t['max_words']} "
                "palavras no total; se passar disso, enxugue o corpo antes do fecho."
            ),
        ]

    def _build_prompt(self, news_batch: list[NewsItem]) -> str:
        brand = self._brand
        # content.script_prompt_profile takes precedence over brand.script_prompt_profile
        content_profile = str(
            self._channel_content.get("script_prompt_profile", "")
        ).strip().lower()
        profile = content_profile or str(brand.get("script_prompt_profile", "rpj_tech")).strip().lower()
        if profile == "ai_tools_business":
            return self._build_prompt_ai_tools_business(brand)
        if profile == "rpj_ai":
            return self._build_prompt_rpj_ai(news_batch, brand)

        brand_name = brand.get("name", "RPJ Tech Group")
        site = brand.get("website", "https://rpjtechgroup.github.io/")
        email = brand.get("email", "rpjtechgroup@gmail.com")
        positioning = brand.get(
            "positioning",
            "Consultoria de tecnologia e software house com estrategia, economia e solucoes inteligentes sob demanda.",
        )
        cta_short = brand.get(
            "cta_short",
            "Ideia maluca no papel ou problema dificil? Chama a RPJ Tech Group: conversa de graca "
            "pra ver caminho. Site e e-mail na descricao.",
        )
        pillars = ", ".join(brand.get("pillars", [])) or (
            "estrategia, economia, solucoes inteligentes, entrega sob demanda"
        )

        rules = [
            f"Voce escreve roteiros de YouTube Shorts em PT-BR para a marca {brand_name}.",
            f"Escopo estrito: gerar conteudo apenas para o canal {self._channel_id}. Nunca misturar com outro canal.",
            f"Contexto institucional (para youtube_body e referencia; nao vire propaganda no meio da fala): {positioning}",
            f"Pilares de comunicacao (referencia; nao empurre na fala): {pillars}.",
            "Prioridade maxima: o nucleo do script_text deve passar claramente a mensagem da noticia do BRIEFING_INPUT (o que rolou e por que importa), usando title e summary como base. Nao distorcer fatos; nao inventar detalhe que nao esteja no briefing.",
            "Tom: linguagem informal de mano da quebrada — girias e ritmo de bate-papo de periferia em PT-BR, autentico, sem forcar piada. Proibido soar como comercial, urgencia falsa ou superlativo vazio. Evite repetir o nome da marca no meio do roteiro; se citar a marca, deixe so perto do fecho. Sem insulto a grupo, sem slur, sem caricatura grosseira.",
            "Estrutura: gancho curto; corpo = noticia contada de forma clara + um comentario leve em cima (opiniao de camarada). Proibido amarrar cada frase a consultoria, software house, automacao ou lista de servicos da marca.",
            "Fecho: nos ultimos 1 a 2 periodos, de forma curta e sutil, sugira que quem tem ideia maluca, projeto parado no papel ou problema dificil pode chamar a RPJ Tech Group. Deixe claro que site e contato estao na descricao e que rola conversa ou orientacao gratuita (tirar duvida de graca, sem compromisso), sem listar servicos e sem tom de infomercial.",
            *self._script_duration_rules(),
            "REGRAS RIGIDAS para script_text (sera lido por uma voz sintetica e virar legenda):",
            *_tts_spoken_script_rules(),
            "- Apenas texto corrido em portugues, em uma unica string.",
            "- PROIBIDO HTML, tags, markdown, listas com hifen ou asteriscos, emojis, hashtags, e URLs cruas. Nada de '<p>', '<br>', '**', '#', http:// ou www.",
            "- Nada de citacoes de fontes ou nomes de sites; reescreva o angulo com suas palavras.",
            "- Nao leia o e-mail nem a URL letra por letra; convide a pessoa a abrir a descricao do video para achar site e e-mail da RPJ Tech Group.",
            f"Para o campo youtube_body, escreva 2 a 4 paragrafos curtos para a descricao do video no YouTube, podendo incluir links, hashtags, palavras-chave de SEO/ATS e mencoes a redes sociais. Use o site {site} e o e-mail {email} aqui sim, por extenso.",
            "Para tags, retorne 5 a 10 strings curtas relacionadas a tecnologia, consultoria, software house e a RPJ Tech Group.",
            "Retorne SOMENTE um JSON valido (sem texto antes ou depois, sem cercas de codigo) com as chaves: title, script_text, youtube_body, tags.",
        ]
        payload = [
            {
                "title": item.title,
                "summary": item.summary,
                "source_url": item.source_url,
                "source_name": item.source_name,
            }
            for item in news_batch
        ]
        prompt_lines = [
            *rules,
            "",
            f"CTA falado sugerido (adapte para soar natural, sem soletrar URL): {cta_short}",
        ]
        prompt_lines = self._inject_recent_runs_context(prompt_lines)
        prompt_lines.extend(
            [
                "",
                "BRIEFING_INPUT:",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        return "\n".join(prompt_lines)

    def _build_prompt_ai_tools_business(self, brand: dict[str, Any]) -> str:
        """Prompt freeform sem RSS: Gemini inventa tema a partir do bloco editorial do YAML."""
        brand_name = brand.get("name", "RPJ Tech Group")
        site = brand.get("website", "https://rpjtechgroup.github.io/")
        email = brand.get("email", "rpjtechgroup@gmail.com")
        cta_short = brand.get(
            "cta_short",
            "Quer IA no seu negocio mas nao sabe por onde comecar? A RPJ Tech Group faz uma "
            "conversa gratuita pra te ajudar. Site e e-mail estao na descricao.",
        )
        pillars = ", ".join(brand.get("pillars", [])) or (
            "ferramentas de IA para o negocio, automacao pratica, atendimento, marketing e vendas com IA"
        )

        content = self._channel_content
        editorial = content.get("editorial") or {}
        focus = str(editorial.get("focus") or "Ferramentas de IA para negocios").strip()
        narrative = str(editorial.get("narrative") or "").strip()
        structure = str(editorial.get("structure") or "").strip()
        avoid = str(editorial.get("avoid") or "").strip()
        raw_cats = content.get("tool_categories") or []
        tool_categories = "\n".join(f"  - {c}" for c in raw_cats) if raw_cats else "  - ferramentas de IA em geral"

        target_secs = self._script_target.get("max_seconds", 35)
        scene_count_min = max(7, math.ceil(target_secs / 3))

        rules = [
            f"Voce escreve roteiros de YouTube Shorts em PT-BR para a marca {brand_name}.",
            f"Escopo estrito: gerar conteudo exclusivamente para o canal {self._channel_id}.",
            "",
            "=== MISSAO EDITORIAL ===",
            f"Foco: {focus}",
            f"Pilares do canal: {pillars}",
        ]
        if narrative:
            rules.append(f"Narrativa desejada: {narrative}")
        if structure:
            rules.append(f"Estrutura do roteiro: {structure}")
        if avoid:
            rules.append(f"Proibicoes editoriais: {avoid}")

        rules += [
            "",
            "=== CATEGORIAS DE FERRAMENTAS DISPONIVEIS ===",
            f"Escolha o tema dentro de uma dessas categorias (ou combine duas):",
            tool_categories,
            "",
            "=== TOM ===",
            "Linguagem informal de mano da quebrada — girias e ritmo de bate-papo de periferia em PT-BR, "
            "autentico, sem forcar piada. Proibido soar como comercial, urgencia falsa ou superlativo vazio. "
            "Nao mencione o nome da marca no corpo do roteiro; reserve para o fecho.",
            "Sem insulto a grupo, sem slur, sem caricatura grosseira.",
            "",
            "=== ESTRUTURA OBRIGATORIA ===",
            "1. Gancho forte nos primeiros 5 a 8 segundos: pergunta provocativa ou dado/situacao que para o scroll.",
            "2. Corpo: dois ou tres beneficios ou casos de uso reais da ferramenta para PME/empreendedor "
            "(economia de tempo, mais vendas, atendimento melhor, operacao mais barata).",
            "3. Fecho (ultimos 1 a 2 periodos): convide o espectador a contatar a RPJ Tech Group pelo site "
            "ou e-mail na descricao. Use o CTA sugerido abaixo como referencia — adapte para soar natural.",
            "",
            *self._script_duration_rules(),
            "",
            "=== REGRAS RIGIDAS PARA script_text (sera lido por voz sintetica e virar legenda) ===",
            *_tts_spoken_script_rules(),
            "- Apenas texto corrido em portugues, em uma unica string.",
            "- PROIBIDO HTML, tags, markdown, listas com hifen ou asteriscos, emojis, hashtags e URLs cruas.",
            "- Nao soletre URL nem e-mail no script_text; convide a pessoa a abrir a descricao do video.",
            "",
            f"Para youtube_body: 2 a 4 paragrafos para a descricao do YouTube com SEO, hashtags e o site {site} "
            f"e e-mail {email} escritos por extenso.",
            "Para tags: 5 a 10 strings curtas relacionadas a IA, ferramentas de IA para negocios e a RPJ Tech Group.",
            (
                f"Para visual_scenes: gere exatamente {scene_count_min} cenas (ou ceil(max_seconds / 3), o que for maior). "
                "Cada cena e um objeto JSON com: \"prompt_en\" (descricao em ingles para geracao de imagem — "
                "cinematografica, vertical 9:16, sem texto nem logos, foto ou ilustracao de alta qualidade "
                "relacionada ao tema da ferramenta de IA escolhida) e \"keywords_pt\" "
                "(palavras-chave em portugues separadas por virgula). Varie planos, angulos e elementos visuais."
            ),
            "Para topic_key: slug em kebab-case da ferramenta/tema escolhido (ex: chatgpt-atendimento, "
            "make-automacao-vendas, midjourney-marketing). Sera usado para anti-repeticao nos proximos runs.",
            "",
            "Retorne SOMENTE um JSON valido (sem texto antes ou depois, sem cercas de codigo) "
            "com as chaves: title, script_text, youtube_body, tags, visual_scenes, topic_key.",
        ]

        prompt_lines = [
            *rules,
            "",
            f"CTA falado sugerido: {cta_short}",
        ]
        prompt_lines = self._inject_recent_topics_context(prompt_lines)
        return "\n".join(prompt_lines)

    def _build_prompt_rpj_ai(
        self, news_batch: list[NewsItem], brand: dict[str, Any]
    ) -> str:
        """RPJ Tech Group com foco editorial em inteligencia artificial (noticias + angulo IA)."""
        brand_name = brand.get("name", "RPJ Tech Group")
        site = brand.get("website", "https://rpjtechgroup.github.io/")
        email = brand.get("email", "rpjtechgroup@gmail.com")
        positioning = brand.get(
            "positioning",
            "Canal tech com foco principal em inteligencia artificial: modelos generativos, LLMs, "
            "automacao com IA, dados, hardware e politicas — sempre com olhar critico e acessivel.",
        )
        cta_short = brand.get(
            "cta_short",
            "Ideia maluca no papel ou problema dificil com IA ou tech? Chama a RPJ Tech Group: "
            "rola conversa de graca pra ver caminho. Site e e-mail estao na descricao.",
        )
        pillars = ", ".join(brand.get("pillars", [])) or (
            "inteligencia artificial, LLMs, automacao inteligente, dados, etica e tendencias"
        )

        rules = [
            f"Voce escreve roteiros de YouTube Shorts em PT-BR para a marca {brand_name}.",
            f"Escopo estrito: gerar conteudo apenas para o canal {self._channel_id}. Nunca misturar com outro canal.",
            f"Contexto institucional (para youtube_body e referencia; nao vire propaganda no meio da fala): {positioning}",
            f"Pilares de comunicacao (referencia; nao empurre na fala): {pillars}.",
            "Prioridade maxima: o nucleo do script_text deve passar claramente a mensagem da noticia do BRIEFING_INPUT, "
            "com angulo principal em inteligencia artificial: modelos generativos, aprendizado de maquina aplicado, "
            "LLMs, copilotos, automacao cognitiva, dados, chips/aceleradores, regulacao e impacto na sociedade — "
            "quando a noticia for diretamente sobre IA. Se a noticia for tech geral ou adjacente, conecte de forma "
            "honesta ao ecossistema de IA (como afeta quem desenvolve ou usa IA) sem inventar que a noticia e "
            "exclusivamente sobre IA nem distorcer fatos.",
            "Tom: linguagem informal de mano da quebrada — girias e ritmo de bate-papo de periferia em PT-BR, autentico, sem forcar piada. Proibido soar como comercial, urgencia falsa ou superlativo vazio. Evite repetir o nome da marca no meio do roteiro; se citar a marca, deixe so perto do fecho. Sem insulto a grupo, sem slur, sem caricatura grosseira.",
            "Estrutura: gancho curto; corpo = noticia contada de forma clara com o angulo IA em primeiro plano quando couber + um comentario leve em cima (opiniao de camarada). Proibido amarrar cada frase a consultoria, software house, automacao generica ou lista de servicos da marca.",
            "Fecho: nos ultimos 1 a 2 periodos, de forma curta e sutil, sugira que quem tem ideia travada, projeto com IA ou problema dificil em tech pode chamar a RPJ Tech Group. Deixe claro que site e contato estao na descricao e que rola conversa ou orientacao gratuita (tirar duvida de graca, sem compromisso), sem listar servicos e sem tom de infomercial.",
            *self._script_duration_rules(),
            "REGRAS RIGIDAS para script_text (sera lido por uma voz sintetica e virar legenda):",
            *_tts_spoken_script_rules(),
            "- Apenas texto corrido em portugues, em uma unica string.",
            "- PROIBIDO HTML, tags, markdown, listas com hifen ou asteriscos, emojis, hashtags, e URLs cruas. Nada de '<p>', '<br>', '**', '#', http:// ou www.",
            "- Nada de citacoes de fontes ou nomes de sites; reescreva o angulo com suas palavras.",
            "- Nao leia o e-mail nem a URL letra por letra; convide a pessoa a abrir a descricao do video para achar site e e-mail da RPJ Tech Group.",
            f"Para o campo youtube_body, escreva 2 a 4 paragrafos curtos para a descricao do video no YouTube, podendo incluir links, hashtags, palavras-chave de SEO/ATS e mencoes a redes sociais. Use o site {site} e o e-mail {email} aqui sim, por extenso.",
            "Para tags, retorne 5 a 10 strings curtas relacionadas a inteligencia artificial, tecnologia e a RPJ Tech Group.",
            (
                "Para visual_scenes, gere exatamente ceil(duracao_audio / 3) cenas visuais — "
                "em media entre 7 e 12 para um Short de 20-35s. Cada cena e um objeto JSON com "
                "dois campos: \"prompt_en\" (descricao em ingles para geracao de imagem: "
                "cinematografica, vertical 9:16, sem texto nem logos, estilo foto ou ilustracao "
                "de alta qualidade relacionada ao conteudo da noticia) e \"keywords_pt\" "
                "(palavras-chave em portugues separadas por virgula). "
                "Varie as cenas: planos diferentes, angulos, elementos visuais distintos. "
                "Use o numero de cenas correto com base em max_seconds do script_target."
            ),
            "Retorne SOMENTE um JSON valido (sem texto antes ou depois, sem cercas de codigo) "
            "com as chaves: title, script_text, youtube_body, tags, visual_scenes.",
        ]
        payload = [
            {
                "title": item.title,
                "summary": item.summary,
                "source_url": item.source_url,
                "source_name": item.source_name,
            }
            for item in news_batch
        ]
        prompt_lines = [
            *rules,
            "",
            f"CTA falado sugerido (adapte para soar natural, sem soletrar URL): {cta_short}",
        ]
        prompt_lines = self._inject_recent_runs_context(prompt_lines)
        prompt_lines.extend(
            [
                "",
                "BRIEFING_INPUT:",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        return "\n".join(prompt_lines)

    def _parse_model_response(self, *response_parts: str) -> ScriptResult:
        """Parse Gemini stdout/stderr into ``ScriptResult`` (CLI often wraps JSON in prose)."""
        blobs: list[str] = []
        seen_blob: set[str] = set()
        for part in response_parts:
            t = (part or "").strip()
            if t and t not in seen_blob:
                seen_blob.add(t)
                blobs.append(t)
        if len(blobs) >= 2:
            merged = "\n".join(blobs)
            if merged not in seen_blob:
                blobs.append(merged)

        data: dict[str, object] | None = None
        for blob in blobs:
            data = _parse_json_to_dict(blob)
            if data is not None:
                break

        if data is None:
            excerpt = (blobs[-1] if blobs else "")[:800].replace("\n", " ")
            LOGGER.warning(
                "Gemini response is not valid JSON (excerpt): %s",
                excerpt,
            )
            raise ValueError("Gemini response is not valid JSON.")

        if not isinstance(data, dict):
            raise ValueError("Gemini JSON root is not an object.")

        data_any: dict[str, Any] = dict(data)  # type: ignore[arg-type]
        data_any = _flatten_wrapped_json_dict(data_any)
        normalized = _normalize_model_json_dict(data_any)
        for key, value in normalized.items():
            cur = data_any.get(key)
            if cur is None or (isinstance(cur, str) and not cur.strip()):
                data_any[key] = value

        raw_script = data_any.get("script_text")
        if raw_script is None or not str(raw_script).strip():
            keys_preview = sorted(str(k) for k in data_any.keys())
            LOGGER.warning(
                "Gemini JSON missing script_text after alias unwrap (keys: %s).",
                keys_preview[:40],
            )
            raise ValueError("Gemini JSON missing script_text.")

        raw_title = data_any.get("title")
        if raw_title is None or not str(raw_title).strip():
            data_any["title"] = str(raw_script).strip()[:90] or "Short"

        raw_script = str(data_any["script_text"])
        clean_script = sanitize_spoken_text(raw_script)
        if clean_script != raw_script:
            LOGGER.info("Sanitized HTML/markdown/URLs out of script_text before TTS.")

        youtube_body_value = data_any.get("youtube_body")
        youtube_body = str(youtube_body_value).strip() if youtube_body_value else None

        visual_scenes: list[VisualScene] = []
        raw_scenes = data_any.get("visual_scenes")
        if isinstance(raw_scenes, list):
            for scene in raw_scenes:
                if not isinstance(scene, dict):
                    continue
                prompt_en = str(scene.get("prompt_en") or scene.get("promptEn") or "").strip()
                if not prompt_en:
                    continue
                keywords_pt = str(scene.get("keywords_pt") or scene.get("keywordsPt") or "").strip()
                visual_scenes.append(VisualScene(prompt_en=prompt_en, keywords_pt=keywords_pt))

        raw_topic_key = data_any.get("topic_key") or ""
        topic_key = re.sub(r"[^a-z0-9\-]", "", str(raw_topic_key).strip().lower().replace(" ", "-").replace("_", "-"))

        result = ScriptResult(
            title=str(data_any["title"]).strip(),
            script_text=clean_script,
            tags=_coerce_tags_list(data_any.get("tags")),
            youtube_body=youtube_body or None,
            visual_scenes=visual_scenes,
            topic_key=topic_key,
        )

        content_profile = str(
            self._channel_content.get("script_prompt_profile", "")
        ).strip().lower()
        profile = content_profile or str(self._brand.get("script_prompt_profile", "")).strip().lower()
        is_freeform = profile == "ai_tools_business"

        from src.content.script_contract import validate_script_result
        try:
            validate_script_result(
                result,
                self._script_target,
                require_topic_key=is_freeform,
                require_visual_scenes=bool(visual_scenes),
            )
        except ValueError as exc:
            LOGGER.warning("Contrato JSON violado: %s", exc)
            raise

        return result

    def _fallback(self, news_batch: list[NewsItem]) -> ScriptResult:
        brand = self._brand
        content_profile = str(
            self._channel_content.get("script_prompt_profile", "")
        ).strip().lower()
        profile = content_profile or str(brand.get("script_prompt_profile", "rpj_tech")).strip().lower()
        if profile == "ai_tools_business":
            return self._fallback_ai_tools_business(brand)
        if profile == "rpj_ai":
            return self._fallback_rpj_ai(news_batch, brand)

        brand_name = brand.get("name", "RPJ Tech Group")
        site = brand.get("website", "https://rpjtechgroup.github.io/")
        email = brand.get("email", "rpjtechgroup@gmail.com")
        lead = news_batch[0]
        body = sanitize_spoken_text(lead.summary) or (
            "Tecnologia bem aplicada economiza tempo, dinheiro e energia do time."
        )
        spoken = (
            f"Mano, olha so: {sanitize_spoken_text(lead.title)}. "
            f"{body} "
            "Papo reto, e isso. Se tu ta com ideia travada no papel ou um problema que parece "
            f"sem saida, da um salve na {brand_name}: rola conversa de graca pra ver se a gente "
            "tira do papel ou da um norte, sem drama. Site e e-mail ta na descricao."
        )
        youtube_body = (
            f"{lead.title}\n\n"
            f"{lead.summary.strip() or 'Resumo da novidade comentada no Short.'}\n\n"
            f"Na {brand_name} a gente ajuda a tirar ideia do papel e a encarar problema de tech "
            "com conversa gratuita pra alinhar o rumo — consultoria sem custo nessa primeira troca.\n\n"
            f"Site: {site}\n"
            f"E-mail: {email}\n\n"
            "#RPJTechGroup #Tecnologia #Consultoria #SoftwareHouse"
        )
        target_secs = self._script_target.get("max_seconds", 35)
        scene_count = max(7, math.ceil(target_secs / 3))
        fallback_scenes = [
            VisualScene(
                prompt_en=(
                    "cinematic tech illustration, vertical 9:16 portrait, "
                    "software consulting technology theme, futuristic digital elements, "
                    f"no text no logos, high quality, scene {i + 1}"
                ),
                keywords_pt="tecnologia, consultoria, software",
            )
            for i in range(scene_count)
        ]
        return ScriptResult(
            title=f"{brand_name}: {lead.title[:70]}",
            script_text=sanitize_spoken_text(spoken),
            tags=[
                "rpjtechgroup",
                "consultoria de tecnologia",
                "software house",
                "estrategia",
                "tecnologia",
            ],
            youtube_body=youtube_body,
            visual_scenes=fallback_scenes,
        )

    def _fallback_rpj_ai(self, news_batch: list[NewsItem], brand: dict[str, Any]) -> ScriptResult:
        brand_name = brand.get("name", "RPJ Tech Group")
        site = brand.get("website", "https://rpjtechgroup.github.io/")
        email = brand.get("email", "rpjtechgroup@gmail.com")
        lead = news_batch[0]
        body = sanitize_spoken_text(lead.summary) or (
            "Inteligencia artificial continua mudando o jogo: da pesquisa ao produto, mano."
        )
        spoken = (
            f"Mano, olha so no angulo de IA: {sanitize_spoken_text(lead.title)}. "
            f"{body} "
            "Papo reto, e isso. Se tu ta com ideia com modelo, dado, automacao ou um problema "
            f"que parece sem saida, da um salve na {brand_name}: rola conversa de graca pra ver se a gente "
            "tira do papel ou da um norte, sem drama. Site e e-mail ta na descricao."
        )
        youtube_body = (
            f"{lead.title}\n\n"
            f"{lead.summary.strip() or 'Resumo da novidade comentada no Short.'}\n\n"
            f"Na {brand_name} a gente ajuda a encarar projeto e problema de tech com foco em IA — "
            "conversa gratuita pra alinhar o rumo, sem compromisso.\n\n"
            f"Site: {site}\n"
            f"E-mail: {email}\n\n"
            "#RPJTechGroup #InteligenciaArtificial #IA #LLM #Tecnologia"
        )
        target_secs = self._script_target.get("max_seconds", 35)
        scene_count = max(7, math.ceil(target_secs / 3))
        title_clean = sanitize_spoken_text(lead.title)
        fallback_scenes = [
            VisualScene(
                prompt_en=(
                    f"cinematic tech illustration, vertical 9:16 portrait, "
                    f"artificial intelligence concept, futuristic digital elements, "
                    f"high quality photography, no text no logos, scene {i + 1} of {scene_count}"
                ),
                keywords_pt="inteligencia artificial, tecnologia, inovacao",
            )
            for i in range(scene_count)
        ]
        if title_clean and fallback_scenes:
            fallback_scenes[0] = VisualScene(
                prompt_en=(
                    f"cinematic tech news illustration, vertical 9:16 portrait, "
                    f"AI and technology theme inspired by: {title_clean[:80]}, "
                    "no text no logos, high quality"
                ),
                keywords_pt="inteligencia artificial, tecnologia, noticia",
            )
        return ScriptResult(
            title=f"{brand_name}: {lead.title[:70]}",
            script_text=sanitize_spoken_text(spoken),
            tags=[
                "rpjtechgroup",
                "inteligencia artificial",
                "ia",
                "llm",
                "tecnologia",
            ],
            youtube_body=youtube_body,
            visual_scenes=fallback_scenes,
        )

    def _fallback_ai_tools_business(self, brand: dict[str, Any]) -> ScriptResult:
        """Local fallback for ai_tools_business when Gemini is unavailable.

        Rotates through tool categories by day so consecutive runs cover different themes.
        """
        brand_name = brand.get("name", "RPJ Tech Group")
        site = brand.get("website", "https://rpjtechgroup.github.io/")
        email = brand.get("email", "rpjtechgroup@gmail.com")

        content = self._channel_content
        raw_cats = content.get("tool_categories") or []
        categories = [str(c) for c in raw_cats] if raw_cats else [
            "chatbots e assistentes",
            "automacao de tarefas",
            "geracao de imagem e video",
            "analise de dados e planilhas",
            "atendimento e CRM com IA",
        ]
        day_index = datetime.utcnow().toordinal() % len(categories)
        chosen_category = categories[day_index]

        spoken = (
            f"Mano, tu ja parou pra pensar quanto tempo o teu negocio perde em tarefas que "
            f"uma IA podia resolver? A categoria de {chosen_category} esta mudando o jogo pra "
            "empreendedor e PME: menos custo, mais resultado. Se tu quer saber como aplicar "
            f"isso no seu negocio, a {brand_name} faz uma conversa gratuita contigo. "
            "Site e e-mail estao na descricao."
        )
        youtube_body = (
            f"Ferramentas de IA: {chosen_category}\n\n"
            f"A inteligencia artificial chegou pra ficar e o {brand_name} esta aqui para ajudar "
            "o seu negocio a aproveitar esse momento. Conversa gratuita, sem compromisso.\n\n"
            f"Site: {site}\n"
            f"E-mail: {email}\n\n"
            "#RPJTechGroup #InteligenciaArtificial #IAParaNegocio #Automacao #Empreendedorismo"
        )
        target_secs = self._script_target.get("max_seconds", 35)
        scene_count = max(7, math.ceil(target_secs / 3))
        topic_key = re.sub(r"[^a-z0-9\-]", "", chosen_category.lower().replace(" ", "-"))[:40]
        fallback_scenes = [
            VisualScene(
                prompt_en=(
                    f"cinematic AI technology illustration, vertical 9:16 portrait, "
                    f"theme: {chosen_category}, modern business setting, "
                    f"high quality photography, no text no logos, scene {i + 1} of {scene_count}"
                ),
                keywords_pt=f"inteligencia artificial, {chosen_category}, negocio",
            )
            for i in range(scene_count)
        ]
        return ScriptResult(
            title=f"{brand_name}: IA para {chosen_category[:50]}",
            script_text=sanitize_spoken_text(spoken),
            tags=[
                "rpjtechgroup",
                "inteligencia artificial",
                "ia para negocio",
                "automacao",
                "empreendedorismo",
            ],
            youtube_body=youtube_body,
            visual_scenes=fallback_scenes,
            topic_key=topic_key,
        )

