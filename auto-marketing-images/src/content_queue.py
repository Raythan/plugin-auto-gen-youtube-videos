from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.content.script_contract import validate_script_result
from src.models import ScriptResult, VisualScene

LOGGER = logging.getLogger(__name__)


def script_from_content_manifest(script_data: dict[str, Any]) -> ScriptResult:
    scenes_raw = script_data.get("visual_scenes") or []
    visual_scenes: list[VisualScene] = []
    if isinstance(scenes_raw, list):
        for item in scenes_raw:
            if not isinstance(item, dict):
                continue
            prompt_en = str(item.get("prompt_en") or item.get("promptEn") or "").strip()
            if not prompt_en:
                continue
            keywords_pt = str(item.get("keywords_pt") or item.get("keywordsPt") or "").strip()
            visual_scenes.append(VisualScene(prompt_en=prompt_en, keywords_pt=keywords_pt))
    return ScriptResult(
        title=str(script_data.get("title") or "").strip(),
        script_text=str(script_data.get("script_text") or script_data.get("scriptText") or "").strip(),
        tags=[str(t).strip() for t in (script_data.get("tags") or []) if str(t).strip()],
        youtube_body=script_data.get("youtube_body") or script_data.get("youtubeBody"),
        visual_scenes=visual_scenes,
        topic_key=str(script_data.get("topic_key") or script_data.get("topicKey") or "").strip(),
    )


class ContentQueue:
    """FIFO queue of content packages from plugin-auto-gen."""

    def __init__(self, inbox_dir: Path, processed_dir: Path) -> None:
        self.inbox_dir = inbox_dir
        self.processed_dir = processed_dir

    def _list_package_dirs(self) -> list[Path]:
        if not self.inbox_dir.is_dir():
            return []
        dirs = [p for p in self.inbox_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
        return sorted(dirs, key=lambda p: p.name)

    def pending_count(self, channel_id: str | None = None) -> int:
        count = 0
        for package_dir in self._list_package_dirs():
            manifest = self._read_manifest(package_dir)
            if manifest is None:
                continue
            if channel_id and manifest.get("channel_id") != channel_id:
                continue
            count += 1
        return count

    def package_exists(self, package_id: str) -> bool:
        target = self.inbox_dir / package_id
        if target.is_dir():
            return True
        processed = self.processed_dir / package_id
        return processed.is_dir()

    def _read_manifest(self, package_dir: Path) -> dict[str, Any] | None:
        manifest_path = package_dir / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data["_package_dir"] = str(package_dir)
                return data
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Invalid manifest in %s: %s", package_dir, exc)
        return None

    def peek_oldest(self, channel_id: str | None = None) -> dict[str, Any] | None:
        for package_dir in self._list_package_dirs():
            manifest = self._read_manifest(package_dir)
            if manifest is None:
                continue
            if channel_id and manifest.get("channel_id") != channel_id:
                continue
            image_paths = self._image_paths(package_dir)
            manifest["_package_dir"] = str(package_dir)
            manifest["_image_paths"] = [str(p) for p in image_paths]
            return manifest
        return None

    def _image_paths(self, package_dir: Path) -> list[Path]:
        images_dir = package_dir / "images"
        if not images_dir.is_dir():
            return []
        paths = sorted(images_dir.glob("*.png"))
        return paths

    def validate_package(
        self,
        manifest: dict[str, Any],
        image_paths: list[Path],
        *,
        script_target: dict[str, int] | None = None,
    ) -> ScriptResult:
        script_data = manifest.get("script")
        if not isinstance(script_data, dict):
            raise ValueError("manifest.script ausente ou invalido.")
        script = script_from_content_manifest(script_data)
        validate_script_result(
            script,
            script_target,
            require_topic_key=bool(script.topic_key),
            require_visual_scenes=True,
        )
        if len(image_paths) < len(script.visual_scenes):
            raise ValueError(
                f"Pacote tem {len(image_paths)} imagens mas {len(script.visual_scenes)} cenas."
            )
        if not image_paths:
            raise ValueError("Pacote sem imagens em images/.")
        return script

    def ingest_multipart(
        self,
        manifest_data: dict[str, Any],
        images: list[tuple[str, bytes]],
        *,
        max_payload_bytes: int = 52_428_800,
    ) -> Path:
        """Atomically write a new package from bridge POST."""
        total_size = sum(len(blob) for _, blob in images)
        if total_size > max_payload_bytes:
            raise ValueError(f"Payload excede limite de {max_payload_bytes} bytes.")

        channel_id = str(manifest_data.get("channel_id") or "").strip()
        if not channel_id:
            raise ValueError("channel_id obrigatorio no manifest.")

        script_data = manifest_data.get("script")
        if not isinstance(script_data, dict):
            raise ValueError("script obrigatorio no manifest.")

        script = script_from_content_manifest(script_data)
        validate_script_result(script, require_topic_key=False, require_visual_scenes=True)

        if len(images) < len(script.visual_scenes):
            raise ValueError(
                f"Enviadas {len(images)} imagens mas o roteiro tem {len(script.visual_scenes)} cenas."
            )

        requested_id = str(manifest_data.get("id") or "").strip()
        created_at = datetime.now(timezone.utc)
        package_id = requested_id or f"{created_at.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"

        if self.package_exists(package_id):
            raise FileExistsError(f"Pacote {package_id} ja existe.")

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = self.inbox_dir / f".tmp_{package_id}"
        final_dir = self.inbox_dir / package_id
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

        try:
            tmp_dir.mkdir(parents=True)
            images_dir = tmp_dir / "images"
            images_dir.mkdir()

            for idx, (_, blob) in enumerate(images, start=1):
                (images_dir / f"{idx:02d}.png").write_bytes(blob)

            full_manifest = {
                "id": package_id,
                "created_at": created_at.isoformat(),
                "channel_id": channel_id,
                "source": str(manifest_data.get("source") or "plugin-auto-gen"),
                "script": asdict(script),
            }
            (tmp_dir / "manifest.json").write_text(
                json.dumps(full_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_dir.rename(final_dir)
            LOGGER.info("Content package ingested: %s", final_dir)
            return final_dir
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    def mark_processed(self, package_dir: Path) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        source = Path(package_dir)
        if not source.is_dir():
            raise FileNotFoundError(f"Pacote nao encontrado: {package_dir}")
        target = self.processed_dir / source.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(source), str(target))
        LOGGER.info("Content package archived: %s", target)
        return target


def resolve_content_source(config_pipeline: dict[str, Any], channel_raw: dict[str, Any]) -> str:
    content = channel_raw.get("content") or {}
    if isinstance(content, dict) and content.get("source"):
        return str(content["source"]).strip().lower()
    default = config_pipeline.get("content_source") or "gemini"
    return str(default).strip().lower()
