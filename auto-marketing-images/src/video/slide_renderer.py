"""Renderiza vídeo a partir de slides de imagens IA com efeitos hyper-edit sorteados."""
from __future__ import annotations

import contextlib
import itertools
import logging
import math
import random
import textwrap
from pathlib import Path
from typing import Any

from src.utils.config import ProjectConfig
from src.video.hyper_edit_effects import EFFECT_CATALOGUE, apply_effect

LOGGER = logging.getLogger(__name__)

_TARGET_W = 1080
_TARGET_H = 1920
_SUBTITLE_STROKE_COLOR = "#000000"


class SlideRenderer:
    def __init__(self, root: Path, config: ProjectConfig) -> None:
        self.root = root
        self.config = config

    def render(
        self,
        image_paths: list[Path],
        audio_path: Path,
        subtitle_path: Path,
        video_path: Path,
        video_profile: dict[str, Any],
    ) -> None:
        """Renderiza MP4 a partir de uma lista de imagens com efeitos hyper-edit."""
        from moviepy.editor import (
            AudioFileClip,
            CompositeVideoClip,
            ImageClip,
            TextClip,
            concatenate_videoclips,
        )
        from src.utils.pillow_compat import apply_pillow_moviepy_compat

        apply_pillow_moviepy_compat()

        slides_cfg = video_profile.get("slides") or {}
        slide_duration = float(slides_cfg.get("duration_seconds", 3.0))
        hyper_cfg = video_profile.get("hyper_edit") or {}
        hyper_enabled = bool(hyper_cfg.get("enabled", True))
        effect_pool: list[str] = list(hyper_cfg.get("effect_pool") or EFFECT_CATALOGUE)
        if not effect_pool:
            effect_pool = list(EFFECT_CATALOGUE)
        avoid_repeat = bool(hyper_cfg.get("avoid_consecutive_repeat", True))

        clips: list = []
        overlays: list = []
        merged = None
        final = None

        try:
            with AudioFileClip(str(audio_path)) as probe:
                audio_duration = float(probe.duration)

            # Garante que temos slides suficientes para cobrir o áudio inteiro.
            # Se faltarem imagens, cicla as disponíveis (cada ciclo recebe um efeito diferente).
            n_needed = max(len(image_paths), math.ceil(audio_duration / slide_duration) + 1)
            effective_paths = list(itertools.islice(itertools.cycle(image_paths), n_needed))
            n = len(effective_paths)

            if n > len(image_paths):
                LOGGER.info(
                    "slide_renderer: %d imagens disponiveis; ciclando para %d slides "
                    "(audio=%.1fs, slide=%.1fs).",
                    len(image_paths),
                    n,
                    audio_duration,
                    slide_duration,
                )

            last_effect: str | None = None

            for i, img_path in enumerate(effective_paths):
                # Último slide recebe o tempo restante; todos os outros têm slide_duration.
                if i == n - 1:
                    remaining = audio_duration - i * slide_duration
                    duration = max(0.5, remaining)
                else:
                    duration = slide_duration

                raw_clip = ImageClip(str(img_path)).set_duration(duration)
                raw_clip = raw_clip.resize((_TARGET_W, _TARGET_H))

                if hyper_enabled:
                    pool = [e for e in effect_pool if not (avoid_repeat and e == last_effect)]
                    if not pool:
                        pool = effect_pool
                    effect_id = random.choice(pool)
                    last_effect = effect_id
                    LOGGER.info("Slide %d: efeito=%s dur=%.2fs img=%s", i, effect_id, duration, img_path.name)
                    raw_clip = apply_effect(raw_clip, effect_id, fps=30)
                else:
                    LOGGER.info("Slide %d: sem efeito (hyper_edit desativado) dur=%.2fs", i, duration)

                clips.append(raw_clip)

            merged = concatenate_videoclips(clips, method="compose").subclip(0, audio_duration)

            with AudioFileClip(str(audio_path)) as narration:
                merged = merged.set_audio(narration)

                subtitle_position = str(video_profile.get("subtitle_position", "center")).lower()
                if subtitle_position not in {"top", "center", "bottom"}:
                    subtitle_position = "center"
                stroke_width = int(video_profile.get("subtitle_stroke_width", 3))
                subtitle_color = self._resolve_subtitle_color(video_profile)
                font_clip = self._resolve_subtitle_font_for_clip(video_profile)

                entries = self._read_srt_entries(subtitle_path)
                for start, end, line in entries:
                    txt = textwrap.fill(line, width=28)
                    clip_kwargs: dict = dict(
                        txt=txt,
                        fontsize=int(video_profile.get("subtitle_font_size", 64)),
                        color=subtitle_color,
                        stroke_color=_SUBTITLE_STROKE_COLOR,
                        stroke_width=stroke_width,
                        method="caption",
                        size=(960, None),
                    )
                    if font_clip:
                        clip_kwargs["font"] = font_clip
                    overlay = (
                        TextClip(**clip_kwargs)
                        .set_position(("center", subtitle_position))
                        .set_start(start)
                        .set_duration(max(0.2, end - start))
                    )
                    overlays.append(overlay)

                final = CompositeVideoClip([merged, *overlays])
                final.write_videofile(
                    str(video_path),
                    fps=30,
                    codec="libx264",
                    audio_codec="aac",
                    ffmpeg_params=["-crf", "23", "-preset", "faster"],
                    verbose=False,
                    logger=None,
                )
            LOGGER.info("SlideRenderer: vídeo renderizado em %s", video_path)
        finally:
            for obj in (final, *reversed(overlays), merged, *reversed(clips)):
                if obj is not None:
                    with contextlib.suppress(Exception):
                        obj.close()

    def _resolve_subtitle_color(self, video_profile: dict[str, Any]) -> str:
        import random as _random

        fallback = str(video_profile.get("subtitle_color", "#FFFF00"))
        rotate = video_profile.get("subtitle_color_rotate", True)
        if not rotate:
            return fallback
        configured = video_profile.get("subtitle_colors")
        if isinstance(configured, list) and configured:
            pool = [str(c).strip() for c in configured if str(c).strip()]
            if pool:
                return _random.choice(pool)
        return f"#{_random.randint(0, 0xFFFFFF):06X}"

    def _resolve_subtitle_font_for_clip(self, video_profile: dict[str, Any]) -> str | None:
        """Resolve font path then map to ImageMagick font name via ShortBuilder helpers."""
        from src.video.short_builder import (
            ShortBuilder,
            _imagemagick_font_for_clip,
        )

        sb = ShortBuilder(root=self.root, config=self.config)
        font_file = sb._resolve_subtitle_font(video_profile)
        if not font_file:
            return None
        return _imagemagick_font_for_clip(font_file) or font_file

    def _parse_srt_time(self, value: str) -> float:
        hh_mm_ss, millis = value.split(",")
        hours, minutes, seconds = hh_mm_ss.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000

    def _read_srt_entries(self, subtitle_path: Path) -> list[tuple[float, float, str]]:
        raw_blocks = subtitle_path.read_text(encoding="utf-8").strip().split("\n\n")
        entries: list[tuple[float, float, str]] = []
        for block in raw_blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if len(lines) < 3 or "-->" not in lines[1]:
                continue
            start_text, end_text = [p.strip() for p in lines[1].split("-->", maxsplit=1)]
            text = " ".join(lines[2:])
            entries.append((self._parse_srt_time(start_text), self._parse_srt_time(end_text), text))
        return entries
