from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.utils.config import CHANNELS_DIR_NAME

LOGGER = logging.getLogger(__name__)


def extract_client_id_secret(data: dict[str, Any]) -> tuple[str, str]:
    """Parse Google-style installed credentials or flat client_id/client_secret."""
    installed = data.get("installed")
    if isinstance(installed, dict):
        cid = str(installed.get("client_id") or "").strip()
        sec = str(installed.get("client_secret") or "").strip()
        if cid and sec:
            return cid, sec
    cid = str(data.get("client_id") or "").strip()
    sec = str(data.get("client_secret") or "").strip()
    return cid, sec


def merge_oauth_from_yaml_dict(
    youtube_account: dict[str, Any],
    data: dict[str, Any],
    *,
    log_as: str,
    logger: logging.Logger | None = None,
) -> bool:
    """Merge OAuth client id/secret from channel YAML or standalone OAuth YAML.

    Supports ``youtube_oauth: {client_id, client_secret}``, or root / ``installed``
    keys as in Google client JSON. Returns True if credentials were merged.
    """
    log = logger or LOGGER
    raw_oauth = data.get("youtube_oauth")
    if isinstance(raw_oauth, dict):
        cid = str(raw_oauth.get("client_id") or "").strip()
        csec = str(raw_oauth.get("client_secret") or "").strip()
        if cid and csec:
            youtube_account["oauth_client_id"] = cid
            youtube_account["oauth_client_secret"] = csec
            return True
        if cid or csec:
            log.warning(
                "%s: youtube_oauth needs both client_id and client_secret; ignoring partial block.",
                log_as,
            )
            return False

    cid, csec = extract_client_id_secret(data)
    if cid and csec:
        youtube_account["oauth_client_id"] = cid
        youtube_account["oauth_client_secret"] = csec
        return True
    return False


def try_merge_oauth_from_convention_files(
    root: Path,
    youtube_account: dict[str, Any],
    account_name: str,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """If still no inline OAuth and no client_secret_file, try ``<account>_oauth.yaml`` convention paths."""
    log = logger or LOGGER
    if youtube_account.get("oauth_client_id") and youtube_account.get("oauth_client_secret"):
        return
    if youtube_account.get("client_secret_file"):
        return
    name = (account_name or "").strip()
    if not name:
        return
    candidates = [
        root / "config" / CHANNELS_DIR_NAME / f"{name}_oauth.yaml",
        root / "config" / "secrets" / f"{name}_oauth.yaml",
        root / "secrets" / f"{name}_oauth.yaml",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            continue
        if merge_oauth_from_yaml_dict(
            youtube_account,
            raw,
            log_as=str(path),
            logger=log,
        ):
            log.info("Loaded OAuth client credentials from convention file %s", path)
            return
