from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class NewsItem:
    title: str
    summary: str
    source_url: str
    source_name: str
    published_at: datetime


@dataclass(slots=True)
class VisualScene:
    prompt_en: str
    keywords_pt: str = ""


@dataclass(slots=True)
class ScriptResult:
    title: str
    script_text: str
    tags: list[str]
    youtube_body: str | None = None
    visual_scenes: list[VisualScene] = field(default_factory=list)
    topic_key: str = ""


@dataclass(slots=True)
class VideoResult:
    video_path: Path
    subtitle_path: Path
    duration_seconds: int
