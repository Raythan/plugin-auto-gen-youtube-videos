from __future__ import annotations

import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .audio_io import resolve_ffmpeg_binary
from .base import TTSQuotaExceeded, TTSUnavailable, split_script_sentences

LOGGER = logging.getLogger(__name__)

OMNIVOICE_DESIGN_API = "/_design_fn"
OMNIVOICE_DEFAULT_LANGUAGE = "Portuguese"
OMNIVOICE_AGE_POOL = (
    "Teenager / 少年",
    "Young Adult / 青年",
    "Middle-aged / 中年",
)
OMNIVOICE_PITCH_POOL = (
    "Moderate Pitch / 中音调",
    "High Pitch / 高音调",
    "Very High Pitch / 极高音调",
)
OMNIVOICE_DEFAULT_ACCENT = "Portuguese Accent / 葡萄牙口音"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_timeout(video_profile: dict, *, key: str, default: int) -> int:
    value = video_profile.get(key, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _parse_int(video_profile: dict, *, key: str, default: int, minimum: int = 1) -> int:
    value = video_profile.get(key, default)
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _parse_float(video_profile: dict, *, key: str, default: float, minimum: float = 0.0) -> float:
    value = video_profile.get(key, default)
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def _parse_num_range(raw: object) -> tuple[float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        lo = float(raw[0])
        hi = float(raw[1])
    except (TypeError, ValueError):
        return None
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _pick_from_scalar_or_list(raw: object) -> str | None:
    if isinstance(raw, list):
        pool = [str(x).strip() for x in raw if str(x).strip()]
        if not pool:
            return None
        return random.choice(pool)
    if raw is None:
        return None
    candidate = str(raw).strip()
    return candidate or None


def _sample_from_range(video_profile: dict, key: str) -> float | None:
    parsed = _parse_num_range(video_profile.get(key))
    if not parsed:
        return None
    lo, hi = parsed
    return random.uniform(lo, hi)


def _sample_int_from_range(video_profile: dict, key: str) -> int | None:
    parsed = _parse_num_range(video_profile.get(key))
    if not parsed:
        return None
    lo, hi = parsed
    return random.randint(int(lo), int(hi))


def _space_queue_is_quota_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "429" in lowered
        or "quota" in lowered
        or "rate limit" in lowered
        or "too many requests" in lowered
    )


def _space_queue_is_unavailable_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "503" in lowered
        or "timeout" in lowered
        or "timed out" in lowered
        or "queue" in lowered
        or "connection" in lowered
        or "unavailable" in lowered
        or "sleeping" in lowered
    )


def _make_gradio_client(space_id: str, hf_token: str | None) -> Any:
    """gradio_client 2.x uses ``token=``; older builds used ``hf_token=``."""
    from gradio_client import Client

    if hf_token:
        try:
            return Client(space_id, token=hf_token)
        except TypeError:
            return Client(space_id, hf_token=hf_token)
    return Client(space_id)


def _parse_hf_tokens(video_profile: dict) -> list[str | None]:
    tokens: list[str | None] = []

    root = _project_root()
    token_file_candidates: list[Path] = []
    configured_file = _pick_from_scalar_or_list(video_profile.get("tts_hf_token_file"))
    if configured_file:
        token_file_candidates.append(root / configured_file)
    env_file = os.environ.get("HF_TOKEN_FILE", "").strip()
    if env_file:
        token_file_candidates.append(Path(env_file))
    token_file_candidates.extend(
        [
            root / "config" / "secrets" / "hf_keys.txt",
            root / "config" / "secrets" / "hf_tokens.txt",
            root / "config" / "secrets" / "huggingface_tokens.txt",
        ]
    )

    for path in token_file_candidates:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    tokens.append(line)
            break

    profile_tokens = video_profile.get("tts_hf_tokens")
    if isinstance(profile_tokens, list):
        tokens.extend([str(token).strip() for token in profile_tokens if str(token).strip()])

    env_multi = os.environ.get("HF_TOKENS", "")
    if env_multi.strip():
        for raw in env_multi.replace("\n", ",").split(","):
            tok = raw.strip()
            if tok:
                tokens.append(tok)

    env_single = os.environ.get("HF_TOKEN", "").strip()
    if env_single:
        tokens.append(env_single)

    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token and token not in seen:
            deduped.append(token)
            seen.add(token)

    return [*deduped, None] if deduped else [None]


def _sample_omnivoice_design_params(video_profile: dict) -> dict[str, Any]:
    """Sample kwargs for k2-fsa/OmniVoice ``/_design_fn`` (voice design tab)."""
    params: dict[str, Any] = {
        "pp": True,
        "po": True,
        "dn": True,
    }

    params["lang"] = (
        _pick_from_scalar_or_list(video_profile.get("tts_hf_languages"))
        or OMNIVOICE_DEFAULT_LANGUAGE
    )

    steps = _sample_int_from_range(video_profile, "tts_hf_steps_range")
    if steps is not None:
        params["ns"] = max(4, min(64, steps))

    cfg = _sample_from_range(video_profile, "tts_hf_cfg_scale_range")
    if cfg is not None:
        params["gs"] = max(0.0, min(4.0, round(cfg, 3)))

    speed = _sample_from_range(video_profile, "tts_hf_speed_range")
    if speed is not None:
        params["sp"] = max(0.5, min(1.5, round(speed, 3)))

    gender = _pick_from_scalar_or_list(video_profile.get("tts_hf_genders"))
    if gender:
        params["param_9"] = gender

    age = _pick_from_scalar_or_list(video_profile.get("tts_hf_ages")) or random.choice(
        OMNIVOICE_AGE_POOL
    )
    params["param_10"] = age

    pitch = _pick_from_scalar_or_list(video_profile.get("tts_hf_pitches")) or random.choice(
        OMNIVOICE_PITCH_POOL
    )
    params["param_11"] = pitch

    style = _pick_from_scalar_or_list(video_profile.get("tts_hf_styles"))
    if style:
        params["param_12"] = style

    params["param_13"] = (
        _pick_from_scalar_or_list(video_profile.get("tts_hf_accents"))
        or OMNIVOICE_DEFAULT_ACCENT
    )

    dialect = _pick_from_scalar_or_list(video_profile.get("tts_hf_dialects"))
    if dialect:
        params["param_14"] = dialect

    return params


def _omnivoice_duration_seconds(text: str, video_profile: dict) -> float:
    """OmniVoice ``/_design_fn`` requires ``du`` (target duration in seconds)."""
    chars_per_sec = _parse_float(
        video_profile,
        key="tts_hf_duration_chars_per_second",
        default=14.0,
        minimum=4.0,
    )
    headroom = _parse_float(
        video_profile,
        key="tts_hf_duration_headroom",
        default=1.15,
        minimum=1.0,
    )
    min_sec = _parse_float(
        video_profile,
        key="tts_hf_duration_min_seconds",
        default=3.0,
        minimum=1.0,
    )
    max_sec = _parse_float(
        video_profile,
        key="tts_hf_duration_max_seconds",
        default=120.0,
        minimum=min_sec,
    )

    clamp = _parse_num_range(video_profile.get("tts_hf_duration_seconds_range"))
    if clamp:
        lo, hi = clamp
        min_sec = max(min_sec, lo)
        max_sec = min(max_sec, hi)

    stripped = text.strip()
    estimated = (len(stripped) / chars_per_sec) * headroom if stripped else min_sec
    return round(max(min_sec, min(max_sec, estimated)), 2)


def _force_mp3_24k_mono(source_path: Path, output_path: Path) -> None:
    ffmpeg = resolve_ffmpeg_binary()
    if not ffmpeg:
        shutil.copyfile(source_path, output_path)
        return
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-ar",
            "24000",
            "-ac",
            "1",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _extract_space_audio_file(result: Any) -> Path:
    if isinstance(result, str):
        return Path(result)
    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, str) and Path(item).exists():
                return Path(item)
            if isinstance(item, dict):
                candidate = item.get("path") or item.get("name")
                if isinstance(candidate, str) and Path(candidate).exists():
                    return Path(candidate)
    if isinstance(result, dict):
        candidate = result.get("path") or result.get("name")
        if isinstance(candidate, str) and Path(candidate).exists():
            return Path(candidate)
    raise TTSUnavailable(f"Could not resolve audio path from HF Space result: {type(result)!r}")


def _predict_omnivoice_design(
    client: Any,
    *,
    text: str,
    design_params: dict[str, Any],
    timeout_seconds: int,
) -> Path:
    kwargs = {"text": text, **design_params}
    try:
        job = client.submit(api_name=OMNIVOICE_DESIGN_API, **kwargs)
        out = job.result(timeout=timeout_seconds)
        return _extract_space_audio_file(out)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if _space_queue_is_quota_error(msg):
            raise TTSQuotaExceeded(msg) from exc
        if _space_queue_is_unavailable_error(msg):
            raise TTSUnavailable(msg) from exc
        raise TTSUnavailable(msg) from exc


def _space_predict_with_fallback(
    client: Any,
    *,
    text: str,
    instruct: str,
    timeout_seconds: int,
) -> Path:
    """Generic fallback for secondary Spaces (e.g. Parler)."""
    attempts: list[tuple[dict[str, Any], str | None]] = [
        ({"text": text, "description": instruct}, "/predict"),
        ({"prompt": text, "description": instruct}, "/predict"),
        ({"text": text}, "/predict"),
    ]
    last_exc: Exception | None = None
    for kwargs, api_name in attempts:
        try:
            job = client.submit(api_name=api_name, **kwargs)
            out = job.result(timeout=timeout_seconds)
            return _extract_space_audio_file(out)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    if last_exc is None:
        raise TTSUnavailable("HF Space call failed without exception details.")
    msg = str(last_exc)
    if _space_queue_is_quota_error(msg):
        raise TTSQuotaExceeded(msg) from last_exc
    raise TTSUnavailable(msg) from last_exc


def _render_omnivoice_design(
    *,
    space_id: str,
    text: str,
    output_path: Path,
    chunking: bool,
    design_params: dict[str, Any],
    video_profile: dict,
    token_candidates: list[str | None],
    timeout_seconds: int,
    max_attempts: int,
    retry_backoff_seconds: float,
) -> None:
    chunks = split_script_sentences(text) if chunking else [text]
    chunks = [chunk for chunk in chunks if chunk.strip()]
    if not chunks:
        raise TTSUnavailable("HF Space received empty narration text.")

    LOGGER.info(
        "OmniVoice design request: space=%s chunks=%s base_params=%s",
        space_id,
        len(chunks),
        design_params,
    )

    with tempfile.TemporaryDirectory(prefix="hf_tts_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        rendered_parts: list[Path] = []
        for idx, chunk in enumerate(chunks):
            chunk_params = {**design_params, "du": _omnivoice_duration_seconds(chunk, video_profile)}
            LOGGER.info(
                "OmniVoice chunk %s/%s: du=%ss (%s chars)",
                idx + 1,
                len(chunks),
                chunk_params["du"],
                len(chunk.strip()),
            )
            last_exc: Exception | None = None
            raw_audio: Path | None = None
            for token_idx, token in enumerate(token_candidates):
                client = _make_gradio_client(space_id, token)
                for attempt in range(1, max_attempts + 1):
                    try:
                        raw_audio = _predict_omnivoice_design(
                            client,
                            text=chunk,
                            design_params=chunk_params,
                            timeout_seconds=timeout_seconds,
                        )
                        LOGGER.info(
                            "OmniVoice chunk ok (token %s/%s, try %s/%s).",
                            token_idx + 1,
                            len(token_candidates),
                            attempt,
                            max_attempts,
                        )
                        break
                    except TTSQuotaExceeded as exc:
                        last_exc = exc
                        LOGGER.warning(
                            "OmniVoice quota/rate-limit (token %s/%s, try %s/%s).",
                            token_idx + 1,
                            len(token_candidates),
                            attempt,
                            max_attempts,
                        )
                        break
                    except TTSUnavailable as exc:
                        last_exc = exc
                        if attempt < max_attempts:
                            time.sleep(retry_backoff_seconds * attempt)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        if attempt < max_attempts:
                            time.sleep(retry_backoff_seconds * attempt)
                        continue
                if raw_audio is not None:
                    break
            if raw_audio is None:
                if last_exc is None:
                    raise TTSUnavailable("OmniVoice call failed with unknown error.")
                msg = str(last_exc)
                if _space_queue_is_quota_error(msg):
                    raise TTSQuotaExceeded(msg) from last_exc
                raise TTSUnavailable(msg) from last_exc

            part = tmp_root / f"part_{idx:03d}.mp3"
            _force_mp3_24k_mono(raw_audio, part)
            rendered_parts.append(part)

        if len(rendered_parts) == 1:
            shutil.copyfile(rendered_parts[0], output_path)
            return

        ffmpeg = resolve_ffmpeg_binary()
        if not ffmpeg:
            raise TTSUnavailable("ffmpeg is required to merge chunked HF audio.")
        concat_list = tmp_root / "concat.txt"
        concat_list.write_text(
            "\n".join([f"file '{p.as_posix()}'" for p in rendered_parts]),
            encoding="utf-8",
        )
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def _render_with_space(
    *,
    space_id: str,
    text: str,
    instruct: str,
    timeout_seconds: int,
    output_path: Path,
    chunking: bool,
    token_candidates: list[str | None] | None = None,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 2.0,
) -> None:
    tokens = token_candidates or [None]
    chunks = split_script_sentences(text) if chunking else [text]
    chunks = [chunk for chunk in chunks if chunk.strip()]
    if not chunks:
        raise TTSUnavailable("HF Space received empty narration text.")

    with tempfile.TemporaryDirectory(prefix="hf_tts_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        rendered_parts: list[Path] = []
        for idx, chunk in enumerate(chunks):
            last_exc: Exception | None = None
            raw_audio: Path | None = None
            for token_idx, token in enumerate(tokens):
                client = _make_gradio_client(space_id, token)
                for attempt in range(1, max_attempts + 1):
                    try:
                        raw_audio = _space_predict_with_fallback(
                            client,
                            text=chunk,
                            instruct=instruct,
                            timeout_seconds=timeout_seconds,
                        )
                        break
                    except TTSQuotaExceeded as exc:
                        last_exc = exc
                        break
                    except TTSUnavailable as exc:
                        last_exc = exc
                        if attempt < max_attempts:
                            time.sleep(retry_backoff_seconds * attempt)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        if attempt < max_attempts:
                            time.sleep(retry_backoff_seconds * attempt)
                        continue
                if raw_audio is not None:
                    break
            if raw_audio is None:
                if last_exc is None:
                    raise TTSUnavailable("HF Space call failed with unknown error.")
                msg = str(last_exc)
                if _space_queue_is_quota_error(msg):
                    raise TTSQuotaExceeded(msg) from last_exc
                raise TTSUnavailable(msg) from last_exc
            part = tmp_root / f"part_{idx:03d}.mp3"
            _force_mp3_24k_mono(raw_audio, part)
            rendered_parts.append(part)

        if len(rendered_parts) == 1:
            shutil.copyfile(rendered_parts[0], output_path)
            return

        ffmpeg = resolve_ffmpeg_binary()
        if not ffmpeg:
            raise TTSUnavailable("ffmpeg is required to merge chunked HF audio.")
        concat_list = tmp_root / "concat.txt"
        concat_list.write_text(
            "\n".join([f"file '{p.as_posix()}'" for p in rendered_parts]),
            encoding="utf-8",
        )
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def generate_hf_omnivoice(
    text: str,
    audio_path: Path,
    video_profile: dict,
) -> None:
    timeout = _parse_timeout(video_profile, key="tts_hf_timeout_seconds", default=180)
    max_attempts = _parse_int(video_profile, key="tts_hf_max_attempts", default=3)
    retry_backoff = _parse_float(
        video_profile,
        key="tts_hf_retry_backoff_seconds",
        default=2.0,
        minimum=0.2,
    )
    tokens = _parse_hf_tokens(video_profile)
    space_id = str(video_profile.get("tts_hf_primary_space", "k2-fsa/OmniVoice")).strip()
    chunking = bool(video_profile.get("tts_sentence_chunking", False))
    design_params = _sample_omnivoice_design_params(video_profile)
    try:
        _render_omnivoice_design(
            space_id=space_id,
            text=text,
            output_path=audio_path,
            chunking=chunking,
            design_params=design_params,
            video_profile=video_profile,
            token_candidates=tokens,
            timeout_seconds=timeout,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff,
        )
    except TTSQuotaExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if _space_queue_is_quota_error(msg):
            raise TTSQuotaExceeded(msg) from exc
        raise TTSUnavailable(msg) from exc


def generate_hf_parler(
    text: str,
    audio_path: Path,
    video_profile: dict,
) -> None:
    timeout = _parse_timeout(video_profile, key="tts_hf_secondary_timeout_seconds", default=90)
    max_attempts = _parse_int(video_profile, key="tts_hf_secondary_max_attempts", default=2)
    retry_backoff = _parse_float(
        video_profile,
        key="tts_hf_secondary_retry_backoff_seconds",
        default=1.5,
        minimum=0.2,
    )
    tokens = _parse_hf_tokens(video_profile)
    space_id = str(video_profile.get("tts_hf_secondary_space", "parler-tts/parler_tts")).strip()
    instruct = _pick_from_scalar_or_list(video_profile.get("tts_parler_descriptions")) or str(
        video_profile.get(
            "tts_parler_description",
            "A Brazilian Portuguese speaker with warm, natural cadence and clear diction.",
        )
    ).strip()
    chunking = bool(video_profile.get("tts_sentence_chunking", False))
    try:
        _render_with_space(
            space_id=space_id,
            text=text,
            instruct=instruct,
            timeout_seconds=timeout,
            output_path=audio_path,
            chunking=chunking,
            token_candidates=tokens,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff,
        )
    except TTSQuotaExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if _space_queue_is_quota_error(msg):
            raise TTSQuotaExceeded(msg) from exc
        raise TTSUnavailable(msg) from exc
