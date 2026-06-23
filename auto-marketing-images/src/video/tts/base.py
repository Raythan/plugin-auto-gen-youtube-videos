from __future__ import annotations

import re
from dataclasses import dataclass

_TTS_WS_PATTERN = re.compile(r"\s+")
_TTS_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_TTS_LONG_CLAUSE_SPLIT = re.compile(r"(?<=[,;])\s+")
_TTS_MAX_CHARS_PER_CHUNK = 220


class TTSUnavailable(RuntimeError):
    """Generic transient failure from a TTS provider."""


class TTSQuotaExceeded(TTSUnavailable):
    """Quota/rate-limit failure from a TTS provider."""


@dataclass(frozen=True)
class EdgeProsody:
    rate: str
    pitch: str
    volume: str


def prepare_tts_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("…", ".").replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"\.{3,}", ".", cleaned)
    return _TTS_WS_PATTERN.sub(" ", cleaned).strip()


def split_script_sentences(text: str) -> list[str]:
    prepared = prepare_tts_text(text)
    if not prepared:
        return []
    sentences: list[str] = []
    for part in _TTS_SENTENCE_SPLIT.split(prepared):
        chunk = part.strip()
        if not chunk:
            continue
        if len(chunk) <= _TTS_MAX_CHARS_PER_CHUNK:
            sentences.append(chunk)
            continue
        for sub in _TTS_LONG_CLAUSE_SPLIT.split(chunk):
            sub = sub.strip()
            if sub:
                sentences.append(sub)
    return sentences or [prepared]


def edge_prosody_from_profile(video_profile: dict) -> EdgeProsody:
    return EdgeProsody(
        rate=str(video_profile.get("tts_rate", "+0%")),
        pitch=str(video_profile.get("tts_pitch", "+0Hz")),
        volume=str(video_profile.get("tts_volume", "+0%")),
    )
