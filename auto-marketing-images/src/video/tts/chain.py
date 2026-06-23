from __future__ import annotations

import logging
from pathlib import Path

from gtts import gTTS

from .audio_io import normalize_narration_audio
from .base import TTSQuotaExceeded, TTSUnavailable, edge_prosody_from_profile, prepare_tts_text
from .edge import generate_edge_tts, pick_edge_tts_voice
from .hf_space import generate_hf_omnivoice, generate_hf_parler

LOGGER = logging.getLogger(__name__)


def _maybe_normalize(audio_path: Path, video_profile: dict) -> None:
    if bool(video_profile.get("tts_audio_normalize", False)):
        normalize_narration_audio(audio_path)


def _generate_edge_with_profile(text: str, audio_path: Path, video_profile: dict) -> None:
    prosody = edge_prosody_from_profile(video_profile)
    chunking = bool(video_profile.get("tts_sentence_chunking", False))
    gap_ms = int(video_profile.get("tts_sentence_gap_ms", 120))
    normalize = bool(video_profile.get("tts_audio_normalize", False))
    rotate = video_profile.get("tts_voice_rotate", True)
    voice = (
        pick_edge_tts_voice(video_profile)
        if rotate
        else str(video_profile.get("tts_voice", "pt-BR-FranciscaNeural"))
    )
    generate_edge_tts(
        text=text,
        audio_path=audio_path,
        voice=voice,
        prosody=prosody,
        sentence_chunking=chunking,
        sentence_gap_ms=gap_ms,
        normalize_audio=normalize,
    )


def generate_narration(text: str, audio_path: Path, video_profile: dict) -> None:
    spoken = prepare_tts_text(text)
    engine = str(video_profile.get("tts_engine", "edge_tts")).lower().strip()

    if engine == "edge_tts":
        try:
            _generate_edge_with_profile(spoken, audio_path, video_profile)
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("edge-tts failed, falling back to gTTS. Error: %s", exc)
            lang = video_profile.get("tts_lang", "pt")
            gTTS(text=spoken or text, lang=lang).save(str(audio_path))
            return

    if engine != "hf_chain":
        LOGGER.warning("Unknown tts_engine=%s; defaulting to edge_tts fallback chain.", engine)

    try:
        generate_hf_omnivoice(spoken, audio_path, video_profile)
        _maybe_normalize(audio_path, video_profile)
        LOGGER.info("TTS provider=omnivoice_space")
        return
    except (TTSQuotaExceeded, TTSUnavailable) as exc:
        LOGGER.warning("TTS fallback from OmniVoice to Parler. reason=%s", exc)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("TTS fallback from OmniVoice to Parler. reason=%s", exc)

    try:
        generate_hf_parler(spoken, audio_path, video_profile)
        _maybe_normalize(audio_path, video_profile)
        LOGGER.info("TTS provider=parler_space")
        return
    except (TTSQuotaExceeded, TTSUnavailable) as exc:
        LOGGER.warning("TTS fallback from Parler to Edge. reason=%s", exc)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("TTS fallback from Parler to Edge. reason=%s", exc)

    try:
        _generate_edge_with_profile(spoken, audio_path, video_profile)
        LOGGER.info("TTS provider=edge_tts")
        return
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("edge-tts failed, falling back to gTTS. Error: %s", exc)

    lang = video_profile.get("tts_lang", "pt")
    gTTS(text=spoken or text, lang=lang).save(str(audio_path))
    LOGGER.info("TTS provider=gtts")
