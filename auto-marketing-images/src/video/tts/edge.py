from __future__ import annotations

import asyncio
import logging
import random
import shutil
import uuid
from pathlib import Path

import edge_tts

from .audio_io import concat_narration_clips, normalize_narration_audio
from .base import EdgeProsody, split_script_sentences

LOGGER = logging.getLogger(__name__)

_EDGE_PT_BR_VOICE_CACHE: list[str] | None = None
_EDGE_VOICE_LIST_API_OK = False
_EDGE_VOICE_LIST_RETRIES = 3
_EDGE_VOICE_LIST_RETRY_DELAY_SEC = 2.0


def edge_voice_list_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if status is not None:
        return int(status) in (429, 500, 502, 503, 504)
    message = str(exc).lower()
    return "503" in message or "502" in message or "504" in message or "429" in message


def configured_edge_voice_pool(video_profile: dict) -> list[str]:
    configured = video_profile.get("tts_voices")
    if isinstance(configured, list) and configured:
        names = [str(x).strip() for x in configured if str(x).strip()]
        if names:
            return names
    default = str(video_profile.get("tts_voice", "pt-BR-FranciscaNeural")).strip()
    return [default] if default else []


async def fetch_pt_br_edge_voice_names() -> list[str]:
    last_exc: BaseException | None = None
    for attempt in range(1, _EDGE_VOICE_LIST_RETRIES + 1):
        try:
            voices = await edge_tts.list_voices()
            return sorted(
                {
                    str(v["ShortName"])
                    for v in voices
                    if str(v.get("Locale", "")).lower() == "pt-br"
                }
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _EDGE_VOICE_LIST_RETRIES and edge_voice_list_retryable(exc):
                delay = _EDGE_VOICE_LIST_RETRY_DELAY_SEC * attempt
                LOGGER.warning(
                    "Edge TTS list_voices failed (attempt %s/%s): %s; retry in %ss.",
                    attempt,
                    _EDGE_VOICE_LIST_RETRIES,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return []


def load_edge_pt_br_voice_cache() -> None:
    global _EDGE_PT_BR_VOICE_CACHE, _EDGE_VOICE_LIST_API_OK
    if _EDGE_PT_BR_VOICE_CACHE is not None:
        return
    try:
        _EDGE_PT_BR_VOICE_CACHE = asyncio.run(fetch_pt_br_edge_voice_names())
        _EDGE_VOICE_LIST_API_OK = True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Edge TTS list_voices unavailable (%s); using configured tts_voices/tts_voice.",
            exc,
        )
        _EDGE_PT_BR_VOICE_CACHE = []
        _EDGE_VOICE_LIST_API_OK = False


def edge_pt_br_voice_pool(video_profile: dict) -> list[str]:
    global _EDGE_PT_BR_VOICE_CACHE
    load_edge_pt_br_voice_cache()
    if not _EDGE_VOICE_LIST_API_OK:
        return configured_edge_voice_pool(video_profile)

    api_names = set(_EDGE_PT_BR_VOICE_CACHE or [])
    configured = video_profile.get("tts_voices")
    if isinstance(configured, list) and configured:
        wanted = [str(x).strip() for x in configured if str(x).strip()]
        filtered = [v for v in wanted if v in api_names]
        if filtered:
            return filtered
        LOGGER.warning(
            "tts_voices has no entries supported by Edge TTS for pt-BR; using the full API list.",
        )
    return list(_EDGE_PT_BR_VOICE_CACHE or [])


def pick_edge_tts_voice(video_profile: dict) -> str:
    default = str(video_profile.get("tts_voice", "pt-BR-FranciscaNeural"))
    pool = edge_pt_br_voice_pool(video_profile)
    if not pool:
        LOGGER.warning("No pt-BR Edge TTS voices returned; using tts_voice fallback.")
        return default
    choice = random.choice(pool)
    LOGGER.info("TTS Edge voice (pt-BR pool=%s): %s", len(pool), choice)
    return choice


async def edge_tts_save(
    text: str,
    audio_path: Path,
    *,
    voice: str,
    prosody: EdgeProsody,
) -> None:
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=prosody.rate,
        pitch=prosody.pitch,
        volume=prosody.volume,
    )
    await communicate.save(str(audio_path))


def generate_edge_tts(
    text: str,
    audio_path: Path,
    *,
    voice: str,
    prosody: EdgeProsody,
    sentence_chunking: bool,
    sentence_gap_ms: int,
    normalize_audio: bool,
) -> None:
    sentences = split_script_sentences(text) if sentence_chunking else [text]
    if sentence_chunking and len(sentences) > 1:
        gap_seconds = max(0.0, sentence_gap_ms / 1000.0)
        parts_dir = audio_path.parent / f".tts_parts_{uuid.uuid4().hex[:8]}"
        parts_dir.mkdir(parents=True, exist_ok=True)
        part_paths: list[Path] = []
        try:
            for index, sentence in enumerate(sentences):
                part_path = parts_dir / f"{index:03d}.mp3"
                asyncio.run(
                    edge_tts_save(
                        sentence,
                        part_path,
                        voice=voice,
                        prosody=prosody,
                    )
                )
                part_paths.append(part_path)
            LOGGER.info(
                "TTS Edge chunked narration: sentences=%s gap_ms=%s voice=%s rate=%s pitch=%s",
                len(part_paths),
                sentence_gap_ms,
                voice,
                prosody.rate,
                prosody.pitch,
            )
            concat_narration_clips(part_paths, audio_path, gap_seconds)
        finally:
            shutil.rmtree(parts_dir, ignore_errors=True)
    else:
        asyncio.run(
            edge_tts_save(
                text,
                audio_path,
                voice=voice,
                prosody=prosody,
            )
        )

    if normalize_audio:
        normalize_narration_audio(audio_path)
