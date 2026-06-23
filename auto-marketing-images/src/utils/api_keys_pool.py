"""Shared API key pools for image providers (same pattern as ``gemini_keys``).

Per provider, keys are collected in order (deduplicated):

1. ``<PROVIDER>_KEYS_FILE`` env — path to a text file (one key per line).
2. Single env var (e.g. ``HF_TOKEN``) and optional CSV multi (e.g. ``HF_API_KEYS``).
3. ``pipeline.yaml`` → ``image_gen.keys_files.<provider>`` relative to project root.

When a request hits quota/rate limit (HTTP 429/403 or message), the image generator
tries the next key before moving to the next provider.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from src.utils.config import ProjectConfig
from src.utils.gemini_keys import _split_csv, read_keys_from_file

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderKeySpec:
    """Env and config keys for one image API provider."""

    provider_id: str
    env_single: str
    env_multi: str
    env_keys_file: str


_PROVIDER_SPECS: dict[str, ProviderKeySpec] = {
    "huggingface": ProviderKeySpec(
        "huggingface", "HF_TOKEN", "HF_API_KEYS", "HF_KEYS_FILE"
    ),
    "deepai": ProviderKeySpec(
        "deepai", "DEEPAI_API_KEY", "DEEPAI_API_KEYS", "DEEPAI_KEYS_FILE"
    ),
    "fal": ProviderKeySpec("fal", "FAL_KEY", "FAL_API_KEYS", "FAL_KEYS_FILE"),
    "pollinations": ProviderKeySpec(
        "pollinations",
        "POLLINATIONS_API_KEY",
        "POLLINATIONS_API_KEYS",
        "POLLINATIONS_KEYS_FILE",
    ),
}


def _expand_path(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw.strip())))


def resolve_provider_keys(
    provider_id: str,
    root: Path,
    config: ProjectConfig,
) -> list[str]:
    """Return deduplicated API keys for an image provider."""
    spec = _PROVIDER_SPECS.get(provider_id)
    if spec is None:
        return []

    img_cfg = config.pipeline.get("image_gen", {}) or {}
    keys_files = img_cfg.get("keys_files") or {}
    pipeline_path = ""
    if isinstance(keys_files, dict):
        pipeline_path = str(keys_files.get(provider_id) or "").strip()

    sources: list[str] = []

    file_pointer = os.environ.get(spec.env_keys_file, "").strip()
    if file_pointer:
        loaded = read_keys_from_file(_expand_path(file_pointer))
        sources.extend(loaded)
        if loaded:
            LOGGER.info(
                "image_gen/%s: %d chave(s) de %s.",
                provider_id,
                len(loaded),
                spec.env_keys_file,
            )

    if pipeline_path:
        path = Path(pipeline_path)
        if not path.is_absolute():
            path = root / path
        loaded = read_keys_from_file(path)
        sources.extend(loaded)
        if loaded:
            LOGGER.debug(
                "image_gen/%s: %d chave(s) de keys_files (%s).",
                provider_id,
                len(loaded),
                path,
            )

    sources.extend(_split_csv(os.environ.get(spec.env_single)))
    sources.extend(_split_csv(os.environ.get(spec.env_multi)))

    seen: set[str] = set()
    unique: list[str] = []
    for key in sources:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def looks_like_quota_or_rate_limit(status_code: int | None, text: str = "") -> bool:
    """True when the API response suggests trying another key."""
    if status_code in {429, 402, 403}:
        return True
    lowered = (text or "").lower()
    hints = (
        "quota",
        "rate limit",
        "rate_limit",
        "too many requests",
        "resource exhausted",
        "exceeded",
        "billing",
        "limit reached",
        "queue full",
    )
    return any(h in lowered for h in hints)


def looks_like_transient_queue_full(status_code: int | None, text: str = "") -> bool:
    """True when the provider is temporarily busy (wait and retry, do not skip)."""
    if status_code != 402:
        return False
    return "queue full" in (text or "").lower()


def looks_like_fatal_provider_lock(status_code: int | None, text: str = "") -> bool:
    """True when the provider/account is unusable until manual action (billing, lock, plan)."""
    lowered = (text or "").lower()
    if looks_like_transient_queue_full(status_code, text):
        return False
    if status_code == 403 and (
        "locked" in lowered or "exhausted balance" in lowered or "forbidden" in lowered
    ):
        return True
    if status_code == 402:
        billing_hints = (
            "pro members",
            "depleted",
            "included credits",
            "pre-paid",
            "prepaid",
            "purchase",
            "payment required",
            "insufficient",
            "billing",
            "subscribe",
            "get unlimited",
        )
        if any(h in lowered for h in billing_hints):
            return True
    if status_code in {401, 403} and any(
        h in lowered for h in ("invalid", "unauthorized", "api-key", "api key")
    ):
        return True
    return False
