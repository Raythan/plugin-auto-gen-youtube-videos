from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload

from src.models import ScriptResult, VideoResult
from src.utils.oauth_helpers import extract_client_id_secret
from src.youtube.upload_settings import YouTubeUploadSettings

LOGGER = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_YOUTUBE_TITLE_MAX = 100
_YOUTUBE_DESCRIPTION_MAX = 5000


def _next_full_hour_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour.isoformat().replace("+00:00", "Z")


def _installed_app_client_config(client_id: str, client_secret: str) -> dict[str, Any]:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }


class YouTubeUploader:
    def __init__(
        self,
        root: Path,
        account_config: dict[str, Any],
        brand: dict[str, Any] | None = None,
        *,
        upload_settings: YouTubeUploadSettings,
        account_label: str | None = None,
    ) -> None:
        self.root = root
        self.account_config = account_config
        self.brand = brand or {}
        self.upload_settings = upload_settings
        self._account_label = (account_label or "").strip() or None

    def _oauth_inline(self) -> tuple[str, str] | None:
        cid = str(self.account_config.get("oauth_client_id") or "").strip()
        sec = str(self.account_config.get("oauth_client_secret") or "").strip()
        if cid and sec:
            return cid, sec
        return None

    def _resolve_token_path(self) -> Path:
        configured = self.account_config.get("token_file")
        if configured:
            return self.root / configured
        csf = self.account_config.get("client_secret_file")
        if csf:
            secret_path = self.root / csf
            return self._derive_token_path_from_secret(secret_path)
        if self._oauth_inline():
            raise ValueError(
                "Conta YouTube com credenciais inline (youtube_oauth no canal) precisa de "
                "`token_file` em config/youtube_accounts.yaml (ex.: config/secrets/rpjtechgroup_token.json)."
            )
        raise ValueError(
            "Conta YouTube precisa de `token_file` ou `client_secret_file` em config/youtube_accounts.yaml."
        )

    @staticmethod
    def _derive_token_path_from_secret(secret_path: Path) -> Path:
        name = secret_path.name
        if name.endswith("_client_secret.json"):
            token_name = name.replace("_client_secret.json", "_token.json")
        else:
            token_name = f"{secret_path.stem}_token.json"
        return secret_path.with_name(token_name)

    def _load_credentials(self, token_path: Path) -> Credentials | None:
        if not token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, OSError) as exc:
            LOGGER.warning("Failed to read cached token at %s: %s", token_path, exc)
            return None

    def _save_credentials(self, credentials: Credentials, token_path: Path) -> None:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        LOGGER.info("Saved YouTube OAuth token to %s", token_path)

    def _interactive_flow(self, token_path: Path) -> Credentials:
        inline = self._oauth_inline()
        if inline:
            LOGGER.info("Using OAuth client_id/client_secret from merged config (inline or YAML).")
            flow = InstalledAppFlow.from_client_config(
                _installed_app_client_config(inline[0], inline[1]),
                SCOPES,
            )
            credentials = flow.run_local_server(port=0, prompt="consent")
            self._save_credentials(credentials, token_path)
            return credentials

        csf = self.account_config.get("client_secret_file")
        if not csf:
            acct = self._account_label or "esta conta"
            raise ValueError(
                "Sem credenciais de cliente OAuth para iniciar o fluxo interativo "
                f"(conta YouTube: {acct}). "
                "Precisa de client_id e client_secret para o primeiro consentimento ou quando o token "
                "nao pode ser refrescado.\n"
                "- Adicione youtube_oauth (client_id + client_secret) no YAML do canal em "
                "config/channels_config_structure/<id>.yaml e confirme que schedule_profile na fila "
                "bate com o id do canal (ou use channel_id_aliases em config/pipeline.yaml apos renomear).\n"
                "- Ou defina client_secret_file em youtube_accounts.yaml apontando para um .json do Google "
                "Cloud ou .yaml com client_id/client_secret.\n"
                "- Ou crie (sem editar yaml de contas) config/channels_config_structure/<conta>_oauth.yaml "
                "ou config/secrets/<conta>_oauth.yaml (legado: secrets/<conta>_oauth.yaml) com "
                "client_id/client_secret ou bloco installed.\n"
                "- Veja .cursor/skills/channel-config-playbook/SKILL.md."
            )
        secret_path = self.root / csf
        if not secret_path.is_file():
            raise FileNotFoundError(
                f"OAuth client secret nao encontrado: {secret_path}. "
                "Use JSON do Google Cloud, YAML com client_id/client_secret, ou youtube_oauth no canal."
            )

        suffix = secret_path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            raw = yaml.safe_load(secret_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"OAuth YAML invalido (esperado mapa): {secret_path}")
            cid, sec = extract_client_id_secret(raw)
            if not cid or not sec:
                raise ValueError(
                    f"OAuth YAML em {secret_path} precisa de client_id e client_secret "
                    "(ou chave installed com esses campos)."
                )
            flow = InstalledAppFlow.from_client_config(
                _installed_app_client_config(cid, sec),
                SCOPES,
            )
            credentials = flow.run_local_server(port=0, prompt="consent")
            self._save_credentials(credentials, token_path)
            return credentials

        if suffix == ".json":
            with secret_path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and "installed" in payload:
                flow = InstalledAppFlow.from_client_config(payload, SCOPES)
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
            credentials = flow.run_local_server(port=0, prompt="consent")
            self._save_credentials(credentials, token_path)
            return credentials

        raise ValueError(
            f"client_secret_file deve ser .json, .yaml ou .yml: {secret_path}"
        )

    def _obtain_credentials(self, token_path: Path) -> Credentials:
        credentials = self._load_credentials(token_path)

        if credentials and credentials.valid:
            LOGGER.info("Using cached YouTube OAuth credentials.")
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                LOGGER.info("Refreshing expired YouTube OAuth credentials.")
                credentials.refresh(Request())
                self._save_credentials(credentials, token_path)
                return credentials
            except RefreshError as exc:
                LOGGER.warning(
                    "Could not refresh cached token (%s). Starting interactive auth.",
                    exc,
                )

        LOGGER.info("Starting interactive YouTube OAuth flow (one-time).")
        return self._interactive_flow(token_path)

    def _build_service(self):
        token_path = self._resolve_token_path()
        credentials = self._obtain_credentials(token_path)
        return build("youtube", "v3", credentials=credentials)

    def _sanitize_text(self, text: str, limit: int) -> str:
        """Remove control chars and clamp to YouTube-safe length."""
        cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", cleaned)
        # Drop invalid surrogate code points if present.
        cleaned = cleaned.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
        cleaned = cleaned.strip()
        if len(cleaned) > limit:
            cleaned = cleaned[:limit].rstrip()
        return cleaned

    def _compose_description(self, script: ScriptResult) -> str:
        footer = self._sanitize_text(
            str(self.brand.get("description_footer", "")), _YOUTUBE_DESCRIPTION_MAX
        )
        if not footer:
            site = self.brand.get("website", "https://rpjtechgroup.github.io/")
            email = self.brand.get("email", "rpjtechgroup@gmail.com")
            footer = (
                "Consultoria gratis: agende uma conversa sem compromisso.\n"
                f"Site: {site}\n"
                f"E-mail: {email}"
            )
        # Prefer the dedicated YouTube body (may contain URLs and hashtags) over the
        # spoken script, which is intentionally URL-free for TTS clarity.
        raw_body = script.youtube_body or script.script_text
        body = self._sanitize_text(raw_body, _YOUTUBE_DESCRIPTION_MAX)
        budget = _YOUTUBE_DESCRIPTION_MAX - len(footer) - 2
        if budget < 0:
            return footer[:_YOUTUBE_DESCRIPTION_MAX]
        if len(body) > budget:
            body = body[: budget - 1].rstrip() + "\u2026"
        description = f"{body}\n\n{footer}"
        return self._sanitize_text(description, _YOUTUBE_DESCRIPTION_MAX)

    def _fallback_description(self) -> str:
        brand_name = str(self.brand.get("name", "Canal")).strip() or "Canal"
        site = str(self.brand.get("website", "")).strip()
        email = str(self.brand.get("email", "")).strip()
        parts = [f"Conteudo do canal {brand_name}."]
        contact_bits: list[str] = []
        if site:
            contact_bits.append(site)
        if email:
            contact_bits.append(email)
        if contact_bits:
            parts.append("Contato: " + " | ".join(contact_bits))
        return self._sanitize_text("\n".join(parts), _YOUTUBE_DESCRIPTION_MAX)

    def upload(self, video: VideoResult, script: ScriptResult) -> dict[str, Any]:
        service = self._build_service()
        description = self._compose_description(script)
        title = self._sanitize_text(script.title, _YOUTUBE_TITLE_MAX)
        status: dict[str, Any] = {"selfDeclaredMadeForKids": False}
        if self.upload_settings.publish_mode == "schedule":
            status["privacyStatus"] = "private"
            status["publishAt"] = _next_full_hour_utc_iso()
        else:
            status["privacyStatus"] = self.upload_settings.privacy_status
        if self.upload_settings.contains_synthetic_media:
            status["containsSyntheticMedia"] = True

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": script.tags,
                "categoryId": self.upload_settings.category_id,
            },
            "status": status,
        }
        LOGGER.info(
            "YouTube upload metadata | mode=%s | privacy=%s | publishAt=%s | category=%s (%s) | containsSyntheticMedia=%s",
            self.upload_settings.publish_mode,
            status["privacyStatus"],
            status.get("publishAt", "-"),
            self.upload_settings.category_id,
            self.upload_settings.category_label,
            status.get("containsSyntheticMedia", False),
        )
        media = MediaFileUpload(str(video.video_path), chunksize=-1, resumable=True)
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)
        try:
            response = request.execute()
        except (HttpError, ResumableUploadError) as exc:
            text = str(exc)
            if "invalidDescription" not in text:
                raise
            LOGGER.warning(
                "YouTube rejected description; retrying with minimal safe fallback."
            )
            body["snippet"]["description"] = self._fallback_description()
            request = service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )
            response = request.execute()
        LOGGER.info("Uploaded video %s", response.get("id"))
        return response
