from __future__ import annotations

import contextlib
import logging
import os
import random
import re
import shutil
import subprocess
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from src.utils.pillow_compat import apply_pillow_moviepy_compat

apply_pillow_moviepy_compat()

import moviepy.video.fx.all as vfx
from moviepy.config import change_settings
from moviepy.editor import (
    AudioFileClip,
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
)
from src.video.tts import generate_narration

from src.models import ScriptResult, VideoResult
from src.utils.config import ProjectConfig
from src.video.slide_renderer import SlideRenderer

LOGGER = logging.getLogger(__name__)

_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov"})
_TARGET_W = 1080
_TARGET_H = 1920
_SUBTITLE_STROKE_COLOR = "#000000"


def _random_subtitle_hex_color() -> str:
    return f"#{random.randint(0, 0xFFFFFF):06X}"


@dataclass(frozen=True)
class _RenderJitterSettings:
    enabled: bool
    zoom_range: tuple[float, float]
    pan_range: tuple[float, float]
    brightness_range: tuple[float, float]
    contrast_range: tuple[float, float]
    saturation_range: tuple[float, float]
    rotate_deg_range: tuple[float, float]
    flip_x_probability: float
    trim_start_range: tuple[float, float]
    per_clip_zoom_jitter: float
    fps_choices: tuple[int, ...]
    x264_crf_range: tuple[int, int]
    x264_preset_choices: tuple[str, ...]
    x264_gop_choices: tuple[int, ...]


@dataclass(frozen=True)
class _RenderJitterDraw:
    zoom: float
    pan_x: float
    pan_y: float
    brightness: float
    contrast: float
    saturation: float
    rotate_deg: float
    flip_x: bool
    fps: int
    crf: int
    preset: str
    gop: int


def _parse_float_range(raw: object, default: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return default
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return default
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _parse_int_range(raw: object, default: tuple[int, int]) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return default
    try:
        lo, hi = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return default
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _parse_int_choices(raw: object, default: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        return default
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(out) if out else default


def _parse_str_choices(raw: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        return default
    out = [str(x).strip() for x in raw if str(x).strip()]
    return tuple(out) if out else default


def _parse_render_jitter_settings(video_profile: dict) -> _RenderJitterSettings | None:
    cfg = video_profile.get("render_jitter")
    if not isinstance(cfg, dict):
        return None
    return _RenderJitterSettings(
        enabled=bool(cfg.get("enabled", True)),
        zoom_range=_parse_float_range(cfg.get("zoom_range"), (1.0, 1.035)),
        pan_range=_parse_float_range(cfg.get("pan_range"), (-0.04, 0.04)),
        brightness_range=_parse_float_range(cfg.get("brightness_range"), (0.97, 1.03)),
        contrast_range=_parse_float_range(cfg.get("contrast_range"), (0.98, 1.04)),
        saturation_range=_parse_float_range(cfg.get("saturation_range"), (0.94, 1.06)),
        rotate_deg_range=_parse_float_range(cfg.get("rotate_deg_range"), (-0.6, 0.6)),
        flip_x_probability=max(0.0, min(1.0, float(cfg.get("flip_x_probability", 0.12)))),
        trim_start_range=_parse_float_range(cfg.get("trim_start_seconds_range"), (0.0, 0.35)),
        per_clip_zoom_jitter=max(0.0, float(cfg.get("per_clip_zoom_jitter", 0.012))),
        fps_choices=_parse_int_choices(cfg.get("fps_choices"), (29, 30, 31)),
        x264_crf_range=_parse_int_range(cfg.get("x264_crf_range"), (22, 24)),
        x264_preset_choices=_parse_str_choices(cfg.get("x264_preset_choices"), ("medium", "faster")),
        x264_gop_choices=_parse_int_choices(cfg.get("x264_gop_choices"), (48, 60, 72)),
    )


def _sample_render_jitter(settings: _RenderJitterSettings) -> _RenderJitterDraw:
    pan_lo, pan_hi = settings.pan_range
    return _RenderJitterDraw(
        zoom=random.uniform(*settings.zoom_range),
        pan_x=max(0.0, min(1.0, 0.5 + random.uniform(pan_lo, pan_hi))),
        pan_y=max(0.0, min(1.0, 0.5 + random.uniform(pan_lo, pan_hi))),
        brightness=random.uniform(*settings.brightness_range),
        contrast=random.uniform(*settings.contrast_range),
        saturation=random.uniform(*settings.saturation_range),
        rotate_deg=random.uniform(*settings.rotate_deg_range),
        flip_x=random.random() < settings.flip_x_probability,
        fps=random.choice(settings.fps_choices),
        crf=random.randint(*settings.x264_crf_range),
        preset=random.choice(settings.x264_preset_choices),
        gop=random.choice(settings.x264_gop_choices),
    )


def _resize_cover_crop(
    clip,
    target_w: int,
    target_h: int,
    *,
    zoom: float = 1.0,
    pan_x: float = 0.5,
    pan_y: float = 0.5,
):
    w, h = clip.size
    if not w or not h:
        return clip.resize((target_w, target_h))
    scale = max(target_w / w, target_h / h) * max(1.0, zoom)
    new_w = max(target_w, int(round(w * scale)))
    new_h = max(target_h, int(round(h * scale)))
    resized = clip.resize((new_w, new_h))
    rw, rh = resized.size
    max_x = max(0, rw - target_w)
    max_y = max(0, rh - target_h)
    x1 = int(max_x * max(0.0, min(1.0, pan_x)))
    y1 = int(max_y * max(0.0, min(1.0, pan_y)))
    if rw == target_w and rh == target_h:
        return resized
    return resized.crop(x1=x1, y1=y1, x2=x1 + target_w, y2=y1 + target_h)


def _apply_saturation(clip, factor: float):
    """Scale chroma around per-pixel grey (1.0 = unchanged)."""
    if abs(factor - 1.0) <= 0.005:
        return clip

    def _adjust(frame: np.ndarray) -> np.ndarray:
        img = frame.astype(np.float32)
        if img.ndim < 3 or img.shape[2] < 3:
            return frame
        rgb = img[:, :, :3]
        grey = np.mean(rgb, axis=2, keepdims=True)
        adjusted = grey + factor * (rgb - grey)
        if img.shape[2] > 3:
            adjusted = np.concatenate([adjusted, img[:, :, 3:]], axis=2)
        return np.clip(adjusted, 0, 255).astype(np.uint8)

    return clip.fl_image(_adjust)


def _prepare_b_roll_clip(
    source: Path,
    *,
    jitter: _RenderJitterSettings | None,
    draw: _RenderJitterDraw | None,
) -> VideoFileClip:
    clip = VideoFileClip(str(source))
    if not jitter or not jitter.enabled or draw is None:
        return clip.resize((_TARGET_W, _TARGET_H))

    trim_lo, trim_hi = jitter.trim_start_range
    trim = random.uniform(trim_lo, trim_hi) if trim_hi > 0 else 0.0
    duration = float(clip.duration or 0.0)
    if trim > 0 and duration > trim + 0.5:
        clip = clip.subclip(trim)

    zoom = draw.zoom
    if jitter.per_clip_zoom_jitter > 0:
        zoom += random.uniform(-jitter.per_clip_zoom_jitter, jitter.per_clip_zoom_jitter)
    zoom = max(1.0, min(zoom, 1.08))

    pan_x = max(0.0, min(1.0, draw.pan_x + random.uniform(-0.02, 0.02)))
    pan_y = max(0.0, min(1.0, draw.pan_y + random.uniform(-0.02, 0.02)))
    clip = _resize_cover_crop(clip, _TARGET_W, _TARGET_H, zoom=zoom, pan_x=pan_x, pan_y=pan_y)

    if abs(draw.rotate_deg) >= 0.05:
        clip = clip.rotate(draw.rotate_deg, resample="bicubic")
        clip = _resize_cover_crop(clip, _TARGET_W, _TARGET_H)

    if abs(draw.brightness - 1.0) > 0.005:
        clip = clip.fx(vfx.colorx, draw.brightness)
    if abs(draw.contrast - 1.0) > 0.005:
        clip = clip.fx(vfx.lum_contrast, 0, draw.contrast, 128)
    clip = _apply_saturation(clip, draw.saturation)
    if draw.flip_x:
        clip = clip.fx(vfx.mirror_x)
    return clip


def _active_b_roll_files(media_folder: Path) -> list[Path]:
    """Top-level .mp4/.mov in ``media_folder`` only (not ``old/`` and not recursive)."""
    if not media_folder.is_dir():
        return []
    return sorted(
        p
        for p in media_folder.iterdir()
        if p.is_file() and p.suffix.lower() in _VIDEO_EXTENSIONS
    )


def _ffprobe_duration_seconds(media_path: Path, ffprobe_exe: str) -> float | None:
    args = [
        ffprobe_exe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 120,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(args, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("ffprobe failed for %s: %s", media_path, exc)
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    raw = result.stdout.strip()
    try:
        return float(raw)
    except ValueError:
        return None


def _total_pool_duration_seconds(paths: list[Path]) -> float | None:
    """Sum of durations via ffprobe, or ``None`` if ffprobe is unavailable."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    total = 0.0
    for p in paths:
        d = _ffprobe_duration_seconds(p, exe)
        if d is not None and d > 0:
            total += d
    if total <= 0 and paths:
        LOGGER.warning(
            "ffprobe returned no usable durations for b-roll under %s; skipping pool preflight.",
            paths[0].parent,
        )
        return None
    return total


_B_ROLL_AFTER_USE_MODES = frozenset({"move_to_old", "delete", "keep"})


def _parse_b_roll_after_use(video_profile: dict) -> str:
    raw = str(video_profile.get("b_roll_after_use", "move_to_old")).strip().lower()
    if raw in _B_ROLL_AFTER_USE_MODES:
        return raw
    LOGGER.warning("Unknown b_roll_after_use=%r; using move_to_old.", raw)
    return "move_to_old"


def _move_used_b_roll_to_old(src: Path, old_dir: Path) -> None:
    old_dir.mkdir(parents=True, exist_ok=True)
    dest = old_dir / src.name
    if dest.exists():
        dest = old_dir / f"{src.stem}_{uuid.uuid4().hex[:10]}{src.suffix}"
    shutil.move(str(src), str(dest))
    LOGGER.info("Moved used b-roll asset to %s", dest)


def _delete_used_b_roll(src: Path) -> None:
    try:
        src.unlink()
        LOGGER.info("Deleted used b-roll asset: %s", src)
    except OSError as exc:
        LOGGER.warning("Could not delete used b-roll %s: %s", src, exc)


def _dispose_used_b_roll(src: Path, *, old_dir: Path, mode: str) -> None:
    if mode == "keep":
        LOGGER.debug("Keeping used b-roll asset (b_roll_after_use=keep): %s", src)
        return
    if mode == "delete":
        _delete_used_b_roll(src)
        return
    _move_used_b_roll_to_old(src, old_dir)


# Human-readable Latin sans-serif faces (Windows Fonts). Excludes symbol/UI-icon fonts.
_WINDOWS_SUBTITLE_SANS_ROTATION: tuple[str, ...] = (
    "ariblk.ttf",
    "arialbd.ttf",
    "arial.ttf",
    "calibrib.ttf",
    "calibri.ttf",
    "segoeuib.ttf",
    "segoeui.ttf",
    "verdanab.ttf",
    "verdana.ttf",
    "tahomabd.ttf",
    "tahoma.ttf",
    "trebucbd.ttf",
    "trebuc.ttf",
    "corbelb.ttf",
    "corbel.ttf",
    "candarab.ttf",
    "candara.ttf",
    "framd.ttf",
    "impact.ttf",
    "micross.ttf",
)

# Fallback when ``magick -list font`` is unavailable (paths are still resolved at runtime).
_WINDOWS_TTF_TO_IMAGEMAGICK_FONT: dict[str, str] = {
    "ariblk.ttf": "Arial-Black",
    "arialbd.ttf": "Arial-Bold",
    "arial.ttf": "Arial",
    "calibrib.ttf": "Calibri-Bold",
    "calibri.ttf": "Calibri",
    "segoeuib.ttf": "Segoe-UI-Bold",
    "segoeui.ttf": "Segoe-UI",
    "verdanab.ttf": "Verdana-Bold",
    "verdana.ttf": "Verdana",
    "tahomabd.ttf": "Tahoma-Bold",
    "tahoma.ttf": "Tahoma",
    "trebucbd.ttf": "Trebuchet-MS-Bold",
    "trebuc.ttf": "Trebuchet-MS",
    "corbelb.ttf": "Corbel-Bold",
    "corbel.ttf": "Corbel",
    "candarab.ttf": "Candara-Bold",
    "candara.ttf": "Candara",
    "framd.ttf": "Franklin-Gothic-Medium",
    "impact.ttf": "Impact",
    "micross.ttf": "Microsoft-Sans-Serif",
}

_LINUX_SUBTITLE_SANS_ROTATION: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
)


def _gather_builtin_subtitle_sans_fonts() -> list[str]:
    found: list[str] = []
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    fonts_dir = windir / "Fonts"
    for name in _WINDOWS_SUBTITLE_SANS_ROTATION:
        path = fonts_dir / name
        if path.is_file():
            found.append(str(path))
    for raw in _LINUX_SUBTITLE_SANS_ROTATION:
        path = Path(raw)
        if path.is_file():
            found.append(str(path))
    return found


_IMAGEMAGICK_GLYPH_TO_FONT: dict[str, str] | None = None


def _glyph_lookup_key(path: str) -> str:
    return Path(path).as_posix().lower()


def _parse_imagemagick_font_list(stdout: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    current_name: str | None = None
    for line in stdout.splitlines():
        font_match = re.match(r"\s*Font:\s*(.+)", line)
        if font_match:
            current_name = font_match.group(1).strip()
            continue
        glyph_match = re.match(r"\s*glyphs:\s*(.+)", line, re.IGNORECASE)
        if glyph_match and current_name:
            mapping[_glyph_lookup_key(glyph_match.group(1).strip())] = current_name
    return mapping


def _imagemagick_glyph_font_map() -> dict[str, str]:
    global _IMAGEMAGICK_GLYPH_TO_FONT
    if _IMAGEMAGICK_GLYPH_TO_FONT is not None:
        return _IMAGEMAGICK_GLYPH_TO_FONT

    mapping: dict[str, str] = {}
    try:
        from moviepy.config import get_setting

        binary = get_setting("IMAGEMAGICK_BINARY")
        if binary and Path(binary).is_file():
            popen_kwargs: dict = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "check": False,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run([binary, "-list", "font"], **popen_kwargs)
            if result.returncode == 0 and result.stdout:
                mapping = _parse_imagemagick_font_list(result.stdout)
    except OSError as exc:
        LOGGER.warning("Could not list ImageMagick fonts: %s", exc)

    _IMAGEMAGICK_GLYPH_TO_FONT = mapping
    return mapping


def _imagemagick_font_for_clip(font: str | None) -> str | None:
    """Return a font spec that ImageMagick/TextClip will honor.

    MoviePy forwards ``font`` to ``magick -font``. On Windows, bare ``.ttf`` paths
    are ignored and every caption falls back to the same default face. Map files to
    the registered ImageMagick font name (from ``-list font``) instead.
    """
    if not font:
        return None

    candidate = Path(font)
    if not candidate.is_file():
        return font

    keyed = _glyph_lookup_key(str(candidate.resolve()))
    mapped = _imagemagick_glyph_font_map().get(keyed)
    if mapped:
        return mapped

    fallback = _WINDOWS_TTF_TO_IMAGEMAGICK_FONT.get(candidate.name.lower())
    if fallback:
        return fallback

    LOGGER.warning(
        "No ImageMagick font name for %s; using forward-slash path fallback.",
        font,
    )
    return candidate.as_posix()


def _resolve_imagemagick_binary() -> Path | None:
    """Locate ImageMagick CLI for MoviePy TextClip (Windows + Linux/macOS)."""
    env_binary = os.environ.get("IMAGEMAGICK_BINARY", "").strip()
    if env_binary:
        path = Path(env_binary)
        if path.is_file():
            if path.name.lower() == "convert.exe":
                magick = path.parent / "magick.exe"
                if magick.is_file():
                    return magick
            return path

    for name in ("magick", "convert"):
        located = shutil.which(name)
        if located and Path(located).is_file():
            return Path(located)

    for candidate in (
        Path("/usr/bin/magick"),
        Path("/usr/bin/convert"),
        Path("/usr/local/bin/magick"),
        Path("/usr/local/bin/convert"),
    ):
        if candidate.is_file():
            return candidate

    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        for magick in sorted(program_files.glob("ImageMagick*/magick.exe")):
            return magick
        windows_default = Path(r"C:\Program Files\ImageMagick-7.1.2-Q16-HDRI\magick.exe")
        if windows_default.is_file():
            return windows_default

    return None


class ShortBuilder:
    def __init__(self, root: Path, config: ProjectConfig) -> None:
        self.root = root
        self.config = config
        self._configure_imagemagick()

    def _configure_imagemagick(self) -> None:
        resolved = _resolve_imagemagick_binary()
        if resolved:
            change_settings({"IMAGEMAGICK_BINARY": str(resolved)})
            LOGGER.info("ImageMagick binary: %s", resolved)
            return

        LOGGER.warning(
            "ImageMagick binary not found. On Linux: sudo apt install imagemagick. "
            "Or set IMAGEMAGICK_BINARY (e.g. /usr/bin/convert or magick.exe on Windows)."
        )

    def _subtitle_font_rotation_pool(self, video_profile: dict) -> list[str]:
        configured = video_profile.get("subtitle_fonts")
        if isinstance(configured, list) and configured:
            resolved: list[str] = []
            windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
            fonts_dir = windir / "Fonts"
            for item in configured:
                raw = str(item).strip()
                if not raw:
                    continue
                path = Path(raw)
                if not path.is_absolute():
                    candidate = self.root / path
                    if candidate.is_file():
                        resolved.append(str(candidate))
                        continue
                    path = fonts_dir / path.name
                if path.is_file():
                    resolved.append(str(path))
            if resolved:
                return resolved
            LOGGER.warning(
                "subtitle_fonts had no existing files; using built-in sans-serif rotation pool.",
            )
        return _gather_builtin_subtitle_sans_fonts()

    def _resolve_fixed_subtitle_font(self, video_profile: dict) -> str | None:
        configured = video_profile.get("subtitle_font")
        if configured:
            path = Path(configured)
            if not path.is_absolute():
                path = self.root / path
            if path.is_file():
                return str(path)
            LOGGER.warning("subtitle_font not found: %s", path)

        windir = os.environ.get("WINDIR", r"C:\Windows")
        arial_black = Path(windir) / "Fonts" / "ariblk.ttf"
        if arial_black.is_file():
            return str(arial_black)

        arial_bold = Path(windir) / "Fonts" / "arialbd.ttf"
        if arial_bold.is_file():
            return str(arial_bold)

        dejavu = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        if dejavu.is_file():
            return str(dejavu)

        LOGGER.warning("No bold subtitle font found; captions may look thin.")
        return None

    def _resolve_subtitle_font(self, video_profile: dict) -> str | None:
        """TTF/OTF path for captions (ImageMagick/TextClip)."""
        rotate = video_profile.get("subtitle_font_rotate", True)
        if rotate:
            pool = self._subtitle_font_rotation_pool(video_profile)
            if pool:
                choice = random.choice(pool)
                LOGGER.info("Subtitle font (pool=%s): %s", len(pool), choice)
                return choice
            LOGGER.warning("Subtitle font rotation pool empty; using fixed subtitle_font fallback.")
        return self._resolve_fixed_subtitle_font(video_profile)

    def _resolve_subtitle_color(self, video_profile: dict) -> str:
        fallback = str(video_profile.get("subtitle_color", "#FFFF00"))
        rotate = video_profile.get("subtitle_color_rotate", True)
        if not rotate:
            return fallback
        configured = video_profile.get("subtitle_colors")
        if isinstance(configured, list) and configured:
            pool = [str(c).strip() for c in configured if str(c).strip()]
            if pool:
                choice = random.choice(pool)
                LOGGER.info("Subtitle color (pool=%s): %s", len(pool), choice)
                return choice
        choice = _random_subtitle_hex_color()
        LOGGER.info("Subtitle color (random hex): %s", choice)
        return choice

    def build(
        self,
        script: ScriptResult,
        video_profile: dict,
        image_paths: list[Path] | None = None,
    ) -> VideoResult:
        output_dir = self.root / "output" / "videos"
        output_dir.mkdir(parents=True, exist_ok=True)
        run_slug = self._slugify(script.title)

        audio_path = output_dir / f"{run_slug}.mp3"
        subtitle_path = output_dir / f"{run_slug}.srt"
        video_path = output_dir / f"{run_slug}.mp4"

        visual_mode = str(video_profile.get("visual_mode", "ai_slides")).lower()
        if visual_mode != "ai_slides":
            raise RuntimeError(
                f"Perfil de video com visual_mode={visual_mode!r} nao suportado. "
                "Use rpjtechgroup_default (visual_mode: ai_slides)."
            )
        if not image_paths:
            raise RuntimeError(
                "Nenhuma imagem gerada para o video. Verifique image_gen e as chaves de API em .env / config/secrets/."
            )

        try:
            self._generate_tts(script.script_text, audio_path, video_profile)
            with AudioFileClip(str(audio_path)) as audio:
                audio_duration = float(audio.duration)
            self._generate_srt(script.script_text, subtitle_path, audio_duration, video_profile)

            SlideRenderer(self.root, self.config).render(
                image_paths,
                audio_path,
                subtitle_path,
                video_path,
                video_profile,
            )
        except Exception:
            for path in (audio_path, subtitle_path, video_path):
                path.unlink(missing_ok=True)
            raise

        with AudioFileClip(str(audio_path)) as audio:
            duration = int(audio.duration)
        return VideoResult(
            video_path=video_path,
            subtitle_path=subtitle_path,
            duration_seconds=duration,
        )

    def _generate_tts(self, text: str, audio_path: Path, video_profile: dict) -> None:
        generate_narration(text=text, audio_path=audio_path, video_profile=video_profile)

    def _generate_srt(
        self,
        script_text: str,
        subtitle_path: Path,
        total_duration: float,
        video_profile: dict,
    ) -> None:
        words = script_text.split()
        chunk_size = int(video_profile.get("subtitle_words_per_chunk", 7))
        chunks = [" ".join(words[i : i + chunk_size]) for i in range(0, len(words), chunk_size)]
        if not chunks:
            subtitle_path.write_text("", encoding="utf-8")
            return
        seconds_per_chunk = total_duration / len(chunks)
        lines: list[str] = []
        for index, chunk in enumerate(chunks):
            start_s = index * seconds_per_chunk
            end_s = total_duration if index == len(chunks) - 1 else (index + 1) * seconds_per_chunk
            lines.extend(
                [
                    str(index + 1),
                    f"{self._format_srt_time(start_s)} --> {self._format_srt_time(end_s)}",
                    chunk,
                    "",
                ]
            )
        subtitle_path.write_text("\n".join(lines), encoding="utf-8")

    def _format_srt_time(self, total_seconds: float) -> str:
        safe_seconds = max(0.0, total_seconds)
        hours = int(safe_seconds // 3600)
        minutes = int((safe_seconds % 3600) // 60)
        seconds = int(safe_seconds % 60)
        milliseconds = int(round((safe_seconds - int(safe_seconds)) * 1000))
        if milliseconds == 1000:
            seconds += 1
            milliseconds = 0
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

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
            start_text, end_text = [part.strip() for part in lines[1].split("-->", maxsplit=1)]
            text = " ".join(lines[2:])
            entries.append(
                (self._parse_srt_time(start_text), self._parse_srt_time(end_text), text)
            )
        return entries

    def _render_video(
        self,
        audio_path: Path,
        subtitle_path: Path,
        video_path: Path,
        video_profile: dict,
        *,
        target_duration: float,
    ) -> None:
        media_folder = self.root / video_profile.get("media_folder", "output/videos/b_roll")
        b_roll_after_use = _parse_b_roll_after_use(video_profile)
        old_dir = media_folder / "old"
        if b_roll_after_use == "move_to_old":
            old_dir.mkdir(parents=True, exist_ok=True)

        media_paths = _active_b_roll_files(media_folder)
        if not media_paths:
            raise RuntimeError(
                f"No b-roll video files (.mp4/.mov) available in {media_folder} "
                "(files in old/ are not reused until moved back)."
            )

        pool_order = list(media_paths)
        random.shuffle(pool_order)
        pool_sum = _total_pool_duration_seconds(pool_order)
        if pool_sum is not None and pool_sum + 1e-3 < target_duration:
            raise RuntimeError(
                f"Insufficient b-roll total duration ({pool_sum:.1f}s) for audio ({target_duration:.1f}s) "
                f"in {media_folder}."
            )

        jitter_settings = _parse_render_jitter_settings(video_profile)
        jitter_draw = (
            _sample_render_jitter(jitter_settings)
            if jitter_settings and jitter_settings.enabled
            else None
        )
        if jitter_draw:
            LOGGER.info(
                "Render jitter: zoom=%.3f pan=(%.2f,%.2f) bright=%.3f contrast=%.3f "
                "sat=%.3f rotate=%.2f° flip_x=%s fps=%s crf=%s preset=%s gop=%s",
                jitter_draw.zoom,
                jitter_draw.pan_x,
                jitter_draw.pan_y,
                jitter_draw.brightness,
                jitter_draw.contrast,
                jitter_draw.saturation,
                jitter_draw.rotate_deg,
                jitter_draw.flip_x,
                jitter_draw.fps,
                jitter_draw.crf,
                jitter_draw.preset,
                jitter_draw.gop,
            )

        with AudioFileClip(str(audio_path)) as narration:
            clips: list = []
            overlays: list = []
            merged = None
            final = None
            selected_sources: list[Path] = []
            render_ok = False
            try:
                acc = 0.0
                for source in pool_order:
                    clip = _prepare_b_roll_clip(
                        source,
                        jitter=jitter_settings,
                        draw=jitter_draw,
                    )
                    clips.append(clip)
                    selected_sources.append(source)
                    acc += float(clip.duration or 0.0)
                    if acc + 1e-3 >= target_duration:
                        break
                else:
                    raise RuntimeError(
                        f"Insufficient b-roll to cover audio ({target_duration:.1f}s) "
                        f"in {media_folder} (MoviePy duration sum {acc:.1f}s)."
                    )

                merged = concatenate_videoclips(clips).subclip(0, target_duration)
                merged = merged.set_audio(narration)

                entries = self._read_srt_entries(subtitle_path)
                subtitle_position = str(video_profile.get("subtitle_position", "center")).lower()
                if subtitle_position not in {"top", "center", "bottom"}:
                    subtitle_position = "center"
                stroke_width = int(video_profile.get("subtitle_stroke_width", 6))
                subtitle_color = self._resolve_subtitle_color(video_profile)
                font_file = self._resolve_subtitle_font(video_profile)
                font_clip = _imagemagick_font_for_clip(font_file)
                if font_file and font_clip and font_clip != font_file:
                    LOGGER.info("Subtitle TextClip font: %s (from %s)", font_clip, font_file)
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
                encode_fps = jitter_draw.fps if jitter_draw else 30
                ffmpeg_params: list[str] = []
                if jitter_draw:
                    ffmpeg_params = [
                        "-crf",
                        str(jitter_draw.crf),
                        "-preset",
                        jitter_draw.preset,
                        "-g",
                        str(jitter_draw.gop),
                    ]
                final.write_videofile(
                    str(video_path),
                    fps=encode_fps,
                    codec="libx264",
                    audio_codec="aac",
                    ffmpeg_params=ffmpeg_params or None,
                    verbose=False,
                    logger=None,
                )
                render_ok = True
                LOGGER.info("Rendered video at %s", video_path)
            finally:
                for clip in (final, *reversed(overlays), merged, *reversed(clips)):
                    if clip is not None:
                        with contextlib.suppress(Exception):
                            clip.close()

            if render_ok:
                for src in dict.fromkeys(selected_sources):
                    _dispose_used_b_roll(src, old_dir=old_dir, mode=b_roll_after_use)

    def _slugify(self, text: str) -> str:
        normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
        while "--" in normalized:
            normalized = normalized.replace("--", "-")
        return normalized.strip("-")[:80] or "rpjtechgroup-short"
