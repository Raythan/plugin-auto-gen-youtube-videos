from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# YouTube Data API categoryId → rótulo no Studio (pt-BR).
YOUTUBE_CATEGORY_LABELS: dict[str, str] = {
    "2": "Automóveis",
    "24": "Entretenimento",
    "28": "Ciência e tecnologia",
}


@dataclass(frozen=True, slots=True)
class YouTubeUploadSettings:
    category_id: str
    contains_synthetic_media: bool = True
    privacy_status: str = "public"
    publish_mode: str = "immediate"

    @property
    def category_label(self) -> str:
        return YOUTUBE_CATEGORY_LABELS.get(self.category_id, f"categoryId={self.category_id}")


def youtube_upload_settings_from_channel(raw: dict[str, Any]) -> YouTubeUploadSettings | None:
    """Read ``youtube_upload`` from a channel YAML dict."""
    block = raw.get("youtube_upload")
    if not isinstance(block, dict):
        return None
    category_id = str(block.get("category_id") or "").strip()
    if not category_id:
        return None
    csm = block.get("contains_synthetic_media", True)
    privacy = str(block.get("privacy_status") or "public").strip().lower()
    if privacy not in {"public", "private", "unlisted"}:
        privacy = "public"
    publish_mode = str(block.get("publish_mode") or "immediate").strip().lower()
    if publish_mode not in {"immediate", "schedule"}:
        publish_mode = "immediate"
    return YouTubeUploadSettings(
        category_id=category_id,
        contains_synthetic_media=bool(csm),
        privacy_status=privacy,
        publish_mode=publish_mode,
    )


def format_new_channel_youtube_upload_reminder(channel_id: str) -> str:
    """Texto de lembrete para colar ao criar um canal novo."""
    return f"""# Lembrete: youtube_upload para o canal {channel_id!r}
# Adicione em config/channels_config_structure/{channel_id}.yaml:

youtube_upload:
  category_id: "28"   # IDs comuns: 2 Automóveis | 24 Entretenimento | 28 Ciência e tecnologia
  contains_synthetic_media: true   # API: status.containsSyntheticMedia (conteúdo alterado/sintético)
  privacy_status: public   # API: status.privacyStatus (public | unlisted | private)
  publish_mode: immediate   # immediate (default) | schedule (define publishAt na próxima hora cheia UTC)

# Valide com: python -m src.youtube.check_upload_config
"""
