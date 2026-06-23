from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
from moviepy.audio.AudioClip import AudioArrayClip
from moviepy.editor import AudioFileClip, concatenate_audioclips

LOGGER = logging.getLogger(__name__)


def resolve_ffmpeg_binary() -> str | None:
    raw = os.environ.get("FFMPEG_BINARY")
    if raw and Path(raw).is_file():
        return raw
    return shutil.which("ffmpeg")


def _silence_audio_clip(duration_seconds: float, sample_rate: int = 24000) -> AudioArrayClip:
    samples = max(1, int(duration_seconds * sample_rate))
    return AudioArrayClip(np.zeros((samples, 1), dtype=np.float32), fps=sample_rate)


def concat_narration_clips(part_paths: list[Path], output_path: Path, gap_seconds: float) -> None:
    clips: list = []
    merged = None
    try:
        for index, part in enumerate(part_paths):
            clips.append(AudioFileClip(str(part)))
            if index < len(part_paths) - 1 and gap_seconds > 0:
                clips.append(_silence_audio_clip(gap_seconds))
        merged = concatenate_audioclips(clips)
        merged.write_audiofile(str(output_path), verbose=False, logger=None)
    finally:
        if merged is not None:
            with contextlib.suppress(Exception):
                merged.close()
        for clip in clips:
            with contextlib.suppress(Exception):
                clip.close()


def normalize_narration_audio(audio_path: Path) -> None:
    ffmpeg = resolve_ffmpeg_binary()
    if not ffmpeg:
        LOGGER.warning("ffmpeg not found; skipping tts_audio_normalize.")
        return
    temp_path = audio_path.with_suffix(".norm.mp3")
    try:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(audio_path),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(temp_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        temp_path.replace(audio_path)
        LOGGER.info("TTS audio normalized with ffmpeg loudnorm.")
    except subprocess.CalledProcessError as exc:
        LOGGER.warning("ffmpeg loudnorm failed; keeping raw TTS audio. stderr=%s", exc.stderr)
        if temp_path.exists():
            temp_path.unlink()
