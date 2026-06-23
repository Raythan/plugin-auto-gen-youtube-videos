"""Shared Gemini API key pool for all local RPJ projects.

All keys live in one place (recommended). Same order everywhere:

1. ``GEMINI_KEYS_FILE`` — absolute or ``~`` path to a single shared text file
   (one API key per line; ``#`` comments allowed).
2. ``~/.config/gemini/keys.txt`` — default shared path if the file exists.
3. ``GEMINI_API_KEY`` / ``GEMINI_API_KEYS`` in the environment (after dotenv load).
4. ``pipeline.yaml`` → ``gemini.keys_file`` relative to the project root (optional fallback).

Duplicates are removed while preserving order. When one key hits quota, callers try the next.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from src.utils.config import ProjectConfig

LOGGER = logging.getLogger(__name__)


def read_keys_from_file(path: Path) -> list[str]:
    """Load API keys from a text file (one per line; ``#`` comments ignored)."""
    return _read_keys_from_file(path)


def _read_keys_from_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    keys: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Could not read Gemini keys file %s: %s", path, exc)
        return []
    for line in text.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        keys.append(token)
    return keys


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _expand_path(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw.strip())))


def resolve_gemini_keys(root: Path, config: ProjectConfig) -> list[str]:
    """Collect all Gemini API keys from the shared pool and optional project fallback."""
    pipeline_cfg = config.pipeline.get("gemini", {}) or {}
    sources: list[str] = []

    shared_pointer = os.environ.get("GEMINI_KEYS_FILE", "").strip()
    if shared_pointer:
        p = _expand_path(shared_pointer)
        loaded = _read_keys_from_file(p)
        sources.extend(loaded)
        if loaded:
            LOGGER.info(
                "Gemini: loaded %d key(s) from GEMINI_KEYS_FILE (%s).",
                len(loaded),
                p,
            )

    default_shared = Path.home() / ".config" / "gemini" / "keys.txt"
    if default_shared.is_file():
        try:
            same_as_explicit = shared_pointer and default_shared.resolve() == _expand_path(
                shared_pointer
            ).resolve()
        except OSError:
            same_as_explicit = False
        if not same_as_explicit:
            loaded = _read_keys_from_file(default_shared)
            sources.extend(loaded)
            if loaded:
                LOGGER.info(
                    "Gemini: loaded %d key(s) from default shared file (%s).",
                    len(loaded),
                    default_shared,
                )

    sources.extend(_split_csv(os.environ.get("GEMINI_API_KEY")))
    sources.extend(_split_csv(os.environ.get("GEMINI_API_KEYS")))

    keys_file = pipeline_cfg.get("keys_file")
    if keys_file:
        path = Path(keys_file)
        if not path.is_absolute():
            path = root / path
        loaded = _read_keys_from_file(path)
        sources.extend(loaded)
        if loaded:
            LOGGER.debug(
                "Gemini: loaded %d key(s) from pipeline gemini.keys_file (%s).",
                len(loaded),
                path,
            )

    seen: set[str] = set()
    unique: list[str] = []
    for key in sources:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)

    if not unique:
        LOGGER.warning(
            "No Gemini API keys found. Set GEMINI_KEYS_FILE, create ~/.config/gemini/keys.txt, "
            "use GEMINI_API_KEY(S), or configure gemini.keys_file in pipeline.yaml."
        )
    return unique
