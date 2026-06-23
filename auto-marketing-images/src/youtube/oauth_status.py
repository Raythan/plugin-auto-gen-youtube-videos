from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from src.utils.config import Channel, ProjectConfig
from src.utils.oauth_helpers import (
    merge_oauth_from_yaml_dict,
    try_merge_oauth_from_convention_files,
)
from src.youtube.uploader import SCOPES, YouTubeUploader
from src.youtube.upload_settings import YouTubeUploadSettings

LOGGER = logging.getLogger(__name__)

_DEFAULT_UPLOAD_SETTINGS = YouTubeUploadSettings(
    category_id="28",
    contains_synthetic_media=True,
    privacy_status="public",
)


class OAuthHealth(str, Enum):
    OK = "ok"
    OK_REFRESHED = "ok_refreshed"
    EXPIRING_SOON = "expiring_soon"
    MISSING_TOKEN = "missing_token"
    MISSING_CLIENT = "missing_client"
    INVALID_TOKEN = "invalid_token"
    NEEDS_REAUTH = "needs_reauth"


@dataclass(frozen=True, slots=True)
class ChannelOAuthReport:
    channel_id: str
    youtube_account: str
    token_path: str
    health: OAuthHealth
    message: str
    expiry_utc: datetime | None = None
    has_refresh_token: bool = False
    has_client_credentials: bool = False

    @property
    def needs_attention(self) -> bool:
        """True when upload will fail until manual OAuth (not mere access-token expiry)."""
        return self.health in {
            OAuthHealth.MISSING_TOKEN,
            OAuthHealth.MISSING_CLIENT,
            OAuthHealth.INVALID_TOKEN,
            OAuthHealth.NEEDS_REAUTH,
        }

    @property
    def is_informational_warning(self) -> bool:
        return self.health == OAuthHealth.EXPIRING_SOON


def merge_youtube_account_for_channel(
    root: Path,
    config: ProjectConfig,
    channel: Channel,
) -> tuple[dict[str, Any], str]:
    account_name = channel.youtube_account
    youtube_account = dict(config.youtube_account(account_name))
    merge_oauth_from_yaml_dict(
        youtube_account,
        channel.raw,
        log_as=channel.id,
        logger=LOGGER,
    )
    try_merge_oauth_from_convention_files(
        root,
        youtube_account,
        account_name,
        logger=LOGGER,
    )
    return youtube_account, account_name


def _has_client_credentials(account_config: dict[str, Any]) -> bool:
    cid = str(account_config.get("oauth_client_id") or "").strip()
    sec = str(account_config.get("oauth_client_secret") or "").strip()
    if cid and sec:
        return True
    csf = account_config.get("client_secret_file")
    if csf:
        return True
    return False


def _parse_token_expiry(raw: dict[str, Any]) -> datetime | None:
    expiry = raw.get("expiry")
    if not expiry:
        return None
    text = str(expiry).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def probe_youtube_oauth(
    root: Path,
    account_config: dict[str, Any],
    *,
    account_label: str,
    refresh_if_expired: bool = True,
    expiry_warning_days: int = 7,
) -> ChannelOAuthReport:
    uploader = YouTubeUploader(
        root=root,
        account_config=account_config,
        upload_settings=_DEFAULT_UPLOAD_SETTINGS,
        account_label=account_label,
    )
    token_path = uploader._resolve_token_path()
    rel_token = str(token_path.relative_to(root)) if token_path.is_relative_to(root) else str(token_path)
    has_client = _has_client_credentials(account_config)

    if not token_path.is_file():
        return ChannelOAuthReport(
            channel_id=account_label,
            youtube_account=account_label,
            token_path=rel_token,
            health=OAuthHealth.MISSING_TOKEN,
            message="Token OAuth ausente. Rode o fluxo de reautorização para este canal.",
            has_client_credentials=has_client,
        )

    try:
        raw_token = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ChannelOAuthReport(
            channel_id=account_label,
            youtube_account=account_label,
            token_path=rel_token,
            health=OAuthHealth.INVALID_TOKEN,
            message=f"Token OAuth ilegível: {exc}",
            has_client_credentials=has_client,
        )

    if not isinstance(raw_token, dict):
        return ChannelOAuthReport(
            channel_id=account_label,
            youtube_account=account_label,
            token_path=rel_token,
            health=OAuthHealth.INVALID_TOKEN,
            message="Token OAuth deve ser um objeto JSON.",
            has_client_credentials=has_client,
        )

    expiry_utc = _parse_token_expiry(raw_token)
    has_refresh = bool(str(raw_token.get("refresh_token") or "").strip())

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (ValueError, OSError) as exc:
        return ChannelOAuthReport(
            channel_id=account_label,
            youtube_account=account_label,
            token_path=rel_token,
            health=OAuthHealth.INVALID_TOKEN,
            message=f"Não foi possível carregar o token: {exc}",
            expiry_utc=expiry_utc,
            has_refresh_token=has_refresh,
            has_client_credentials=has_client,
        )

    if credentials.valid:
        health = OAuthHealth.OK
        message = "Credenciais válidas."
        if expiry_utc:
            remaining = expiry_utc - datetime.now(timezone.utc)
            if remaining <= timedelta(days=expiry_warning_days):
                health = OAuthHealth.EXPIRING_SOON
                message = (
                    f"Access token expira em {remaining.days}d "
                    f"({expiry_utc.strftime('%Y-%m-%d %H:%M UTC')}). "
                    "Se o refresh falhar no upload, reautorize manualmente."
                )
        return ChannelOAuthReport(
            channel_id=account_label,
            youtube_account=account_label,
            token_path=rel_token,
            health=health,
            message=message,
            expiry_utc=expiry_utc,
            has_refresh_token=has_refresh,
            has_client_credentials=has_client,
        )

    if credentials.expired and credentials.refresh_token and refresh_if_expired:
        try:
            credentials.refresh(Request())
            uploader._save_credentials(credentials, token_path)
            return ChannelOAuthReport(
                channel_id=account_label,
                youtube_account=account_label,
                token_path=rel_token,
                health=OAuthHealth.OK_REFRESHED,
                message="Token estava expirado; refresh automático concluído com sucesso.",
                expiry_utc=_parse_token_expiry(json.loads(token_path.read_text(encoding="utf-8"))),
                has_refresh_token=True,
                has_client_credentials=has_client,
            )
        except RefreshError as exc:
            hint = (
                "Reautorize com: python -m src.pipeline oauth-reauth --channel <id>"
                if has_client
                else "Configure client_id/client_secret antes de reautorizar."
            )
            return ChannelOAuthReport(
                channel_id=account_label,
                youtube_account=account_label,
                token_path=rel_token,
                health=OAuthHealth.NEEDS_REAUTH,
                message=f"Refresh falhou ({exc}). {hint}",
                expiry_utc=expiry_utc,
                has_refresh_token=has_refresh,
                has_client_credentials=has_client,
            )

    if not has_refresh:
        return ChannelOAuthReport(
            channel_id=account_label,
            youtube_account=account_label,
            token_path=rel_token,
            health=OAuthHealth.NEEDS_REAUTH,
            message="Token sem refresh_token. Reautorize no browser (fluxo interativo).",
            expiry_utc=expiry_utc,
            has_refresh_token=False,
            has_client_credentials=has_client,
        )

    if not has_client:
        return ChannelOAuthReport(
            channel_id=account_label,
            youtube_account=account_label,
            token_path=rel_token,
            health=OAuthHealth.MISSING_CLIENT,
            message=(
                "Credenciais de cliente OAuth ausentes para reautorizar. "
                "Defina youtube_oauth no YAML do canal ou client_secret_file em youtube_accounts.yaml."
            ),
            expiry_utc=expiry_utc,
            has_refresh_token=has_refresh,
            has_client_credentials=False,
        )

    return ChannelOAuthReport(
        channel_id=account_label,
        youtube_account=account_label,
        token_path=rel_token,
        health=OAuthHealth.NEEDS_REAUTH,
        message="Reautorização manual necessária (browser).",
        expiry_utc=expiry_utc,
        has_refresh_token=has_refresh,
        has_client_credentials=has_client,
    )


def check_all_channels(
    root: Path,
    config: ProjectConfig,
    *,
    channels: list[Channel] | None = None,
    expiry_warning_days: int = 7,
) -> list[ChannelOAuthReport]:
    targets = channels if channels is not None else config.enabled_channels
    reports: list[ChannelOAuthReport] = []
    for channel in targets:
        account_config, account_name = merge_youtube_account_for_channel(root, config, channel)
        probe = probe_youtube_oauth(
            root,
            account_config,
            account_label=account_name,
            expiry_warning_days=expiry_warning_days,
        )
        reports.append(
            ChannelOAuthReport(
                channel_id=channel.id,
                youtube_account=account_name,
                token_path=probe.token_path,
                health=probe.health,
                message=probe.message,
                expiry_utc=probe.expiry_utc,
                has_refresh_token=probe.has_refresh_token,
                has_client_credentials=probe.has_client_credentials,
            )
        )
    return reports


def run_channel_oauth_reauth(root: Path, config: ProjectConfig, channel_id: str) -> Path:
    channel = config.channel(channel_id)
    account_config, account_name = merge_youtube_account_for_channel(root, config, channel)
    if not _has_client_credentials(account_config):
        raise ValueError(
            f"Canal {channel_id!r}: sem credenciais de cliente OAuth. "
            "Adicione youtube_oauth no YAML do canal ou client_secret_file em youtube_accounts.yaml."
        )
    uploader = YouTubeUploader(
        root=root,
        account_config=account_config,
        brand={},
        upload_settings=_DEFAULT_UPLOAD_SETTINGS,
        account_label=account_name,
    )
    token_path = uploader._resolve_token_path()
    LOGGER.info(
        "Iniciando reautorização OAuth para canal=%s conta=%s token=%s",
        channel_id,
        account_name,
        token_path,
    )
    uploader._interactive_flow(token_path)
    return token_path
