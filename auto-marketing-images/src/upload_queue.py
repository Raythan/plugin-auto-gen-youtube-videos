from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.models import ScriptResult, VideoResult

LOGGER = logging.getLogger(__name__)


class UploadQueue:
    """FIFO queue of generated videos waiting for YouTube upload.

    Each entry is a JSON manifest under ``queue_dir`` with the absolute paths
    of the rendered video and subtitle files plus the script payload that
    feeds the YouTube description.
    """

    def __init__(self, queue_dir: Path, uploaded_dir: Path) -> None:
        self.queue_dir = queue_dir
        self.uploaded_dir = uploaded_dir

    def enqueue(
        self,
        *,
        script: ScriptResult,
        video: VideoResult,
        schedule_profile: str,
        youtube_account: str,
        video_template: str | None = None,
    ) -> Path:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc)
        item_id = f"{created_at.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        manifest_path = self.queue_dir / f"{item_id}.json"
        payload: dict[str, Any] = {
            "id": item_id,
            "created_at": created_at.isoformat(),
            "schedule_profile": schedule_profile,
            "youtube_account": youtube_account,
            "video_template": video_template,
            "video_path": str(video.video_path),
            "subtitle_path": str(video.subtitle_path),
            "duration_seconds": video.duration_seconds,
            "script": asdict(script),
        }
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        LOGGER.info("Enqueued upload manifest %s", manifest_path)
        return manifest_path

    def peek_oldest(self) -> dict[str, Any] | None:
        if not self.queue_dir.exists():
            return None
        manifests = sorted(self.queue_dir.glob("*.json"))
        if not manifests:
            return None
        manifest_path = manifests[0]
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["_manifest_path"] = str(manifest_path)
        return payload

    def mark_uploaded(self, manifest_path: Path, upload_result: dict[str, Any]) -> Path:
        """Move the manifest to the uploaded archive and return the new path."""
        self.uploaded_dir.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["uploaded_at"] = datetime.now(timezone.utc).isoformat()
        payload["youtube_video_id"] = upload_result.get("id")
        target = self.uploaded_dir / manifest_path.name
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest_path.unlink(missing_ok=True)
        LOGGER.info("Moved manifest to uploaded archive: %s", target)
        return target

    def pending_count(self) -> int:
        if not self.queue_dir.exists():
            return 0
        return sum(1 for _ in self.queue_dir.glob("*.json"))


def script_from_manifest(payload: dict[str, Any]) -> ScriptResult:
    script_data = payload["script"]
    youtube_body = script_data.get("youtube_body")
    return ScriptResult(
        title=str(script_data["title"]),
        script_text=str(script_data["script_text"]),
        tags=[str(tag) for tag in script_data.get("tags", [])],
        youtube_body=str(youtube_body) if youtube_body else None,
    )


def video_from_manifest(payload: dict[str, Any]) -> VideoResult:
    return VideoResult(
        video_path=Path(payload["video_path"]),
        subtitle_path=Path(payload["subtitle_path"]),
        duration_seconds=int(payload.get("duration_seconds", 0)),
    )
