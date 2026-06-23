from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CHANNELS_DIR_NAME = "channels_config_structure"

SCRIPT_TARGET_DEFAULTS: dict[str, int] = {
    "min_seconds": 20,
    "max_seconds": 35,
    "min_words": 55,
    "max_words": 95,
}


@dataclass(slots=True)
class Channel:
    id: str
    enabled: bool
    youtube_account: str
    video_templates: list[str]
    generate_times: list[str]
    template_pick: str = "random"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProjectConfig:
    root: Path
    pipeline: dict[str, Any]
    sources: dict[str, Any]
    video_profiles: dict[str, Any]
    youtube_accounts: dict[str, Any]
    channels: list[Channel]

    def video_profile(self, name: str) -> dict[str, Any]:
        profiles = self.video_profiles.get("profiles", {})
        if name not in profiles:
            raise ValueError(f"Unknown video profile: {name}")
        profile = dict(profiles[name])
        defaults = self.video_profiles.get("render_jitter_defaults")
        if isinstance(defaults, dict) and defaults:
            overlay = profile.get("render_jitter")
            if isinstance(overlay, dict):
                profile["render_jitter"] = _deep_merge(dict(defaults), overlay)
            else:
                profile["render_jitter"] = dict(defaults)
        return profile

    def youtube_account(self, name: str) -> dict[str, Any]:
        accounts = self.youtube_accounts.get("accounts", {})
        if name not in accounts:
            raise ValueError(f"Unknown youtube account profile: {name}")
        return accounts[name]

    def channel(self, channel_id: str) -> Channel:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        raise ValueError(f"Unknown channel: {channel_id}")

    @property
    def enabled_channels(self) -> list[Channel]:
        return [channel for channel in self.channels if channel.enabled]

    @property
    def console(self) -> dict[str, Any]:
        return self.pipeline.get("console", {}) or {}

    @property
    def brand(self) -> dict[str, Any]:
        return self.pipeline.get("brand", {}) or {}

    @property
    def runtime(self) -> dict[str, Any]:
        return self.pipeline.get("runtime", {}) or {}

    def source_names_for_channel(self, channel_id: str) -> set[str] | None:
        """Return the configured source-name allowlist for a channel."""
        # Preferred place: pipeline.yaml -> channel_sources.
        configured = self.pipeline.get("channel_sources")
        if not configured:
            # Backward compatible fallback in sources.yaml.
            configured = self.sources.get("channel_sources")
        if not isinstance(configured, dict):
            return None
        names = configured.get(channel_id)
        if names is None:
            return None
        if isinstance(names, str):
            names = [names]
        if isinstance(names, list) and len(names) == 0:
            return set()
        allowlist = {str(item).strip() for item in names if str(item).strip()}
        return allowlist or None


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def merge_video_profile_with_channel(
    profile: dict[str, Any], channel_raw: dict[str, Any]
) -> dict[str, Any]:
    """Merge optional per-channel video overrides into a video profile dict."""
    merged = dict(profile)
    block = channel_raw.get("video_profile")
    if isinstance(block, dict):
        merged.update(block)
    if "b_roll_after_use" in channel_raw:
        merged["b_roll_after_use"] = channel_raw["b_roll_after_use"]
    return merged


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = _deep_merge(base_value, overlay_value)
        else:
            result[key] = overlay_value
    return result


def _normalize_time(value: Any) -> str | None:
    text = str(value).strip()
    if len(text) == 4 and ":" not in text and text.isdigit():
        text = f"{text[:2]}:{text[2:]}"
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hh, mm = int(text[:2]), int(text[3:])
    except ValueError:
        return None
    if 0 <= hh < 24 and 0 <= mm < 60:
        return f"{hh:02d}:{mm:02d}"
    return None


def _parse_channel(channel_id: str, data: dict[str, Any]) -> Channel:
    youtube_account = str(data.get("youtube_account") or "").strip()
    if not youtube_account:
        raise ValueError(f"Channel {channel_id!r}: missing 'youtube_account'.")

    raw_templates = data.get("video_templates") or []
    if isinstance(raw_templates, str):
        raw_templates = [raw_templates]
    templates = [str(item).strip() for item in raw_templates if str(item).strip()]
    if not templates:
        raise ValueError(f"Channel {channel_id!r}: 'video_templates' must list at least one profile.")

    raw_times = data.get("generate_times") or []
    times: list[str] = []
    for entry in raw_times:
        normalized = _normalize_time(entry)
        if normalized:
            times.append(normalized)
    times = sorted(set(times))

    pick = str(data.get("template_pick", "random")).strip().lower()
    if pick not in {"random", "rotate"}:
        pick = "random"

    return Channel(
        id=channel_id,
        enabled=bool(data.get("enabled", True)),
        youtube_account=youtube_account,
        video_templates=templates,
        generate_times=times,
        template_pick=pick,
        raw=data,
    )


def _coerce_script_target_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_script_target(
    pipeline: dict[str, Any],
    channel_raw: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Merge ``script_target`` from pipeline.yaml and optional channel YAML overrides."""
    merged: dict[str, int] = dict(SCRIPT_TARGET_DEFAULTS)
    for source in (pipeline.get("script_target"), (channel_raw or {}).get("script_target")):
        if not isinstance(source, dict):
            continue
        for key in SCRIPT_TARGET_DEFAULTS:
            if key in source:
                merged[key] = _coerce_script_target_int(source[key], merged[key])
    if merged["min_seconds"] > merged["max_seconds"]:
        merged["min_seconds"], merged["max_seconds"] = (
            merged["max_seconds"],
            merged["min_seconds"],
        )
    if merged["min_words"] > merged["max_words"]:
        merged["min_words"], merged["max_words"] = merged["max_words"], merged["min_words"]
    return merged


def merge_channel_brand(pipeline_brand: dict[str, Any], channel: Channel) -> dict[str, Any]:
    """Merge optional ``brand`` block from channel YAML over global pipeline brand."""
    overlay = channel.raw.get("brand")
    if isinstance(overlay, dict) and overlay:
        return _deep_merge(dict(pipeline_brand), overlay)
    return dict(pipeline_brand)


def merge_brand_from_raw(pipeline_brand: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Merge ``brand`` from a channel YAML dict (used when Channel is not loaded)."""
    overlay = raw.get("brand")
    if isinstance(overlay, dict) and overlay:
        return _deep_merge(dict(pipeline_brand), overlay)
    return dict(pipeline_brand)


def load_channel_yaml_by_stem(project_root: Path, stem: str) -> dict[str, Any]:
    """Load ``config/channels_config_structure/<stem>.yaml`` if it exists (local, often gitignored)."""
    if not stem:
        return {}
    path = project_root / "config" / CHANNELS_DIR_NAME / f"{stem}.yaml"
    return _read_yaml(path)


def _load_channels(config_dir: Path) -> list[Channel]:
    channels_dir = config_dir / CHANNELS_DIR_NAME
    if not channels_dir.is_dir():
        return []
    channels: list[Channel] = []
    for path in sorted(channels_dir.glob("*.yaml")):
        # Templates versionados (*example.yaml) nao sao canais ativos.
        if path.name.endswith(".example.yaml"):
            continue
        data = _read_yaml(path)
        if not data:
            continue
        channel_id = str(data.get("id") or path.stem).strip()
        channels.append(_parse_channel(channel_id, data))
    return channels


def load_project_config(project_root: Path) -> ProjectConfig:
    config_dir = project_root / "config"
    base = {
        "pipeline": _read_yaml(config_dir / "pipeline.yaml"),
        "sources": _read_yaml(config_dir / "sources.yaml"),
        "video_profiles": _read_yaml(config_dir / "video_profiles.yaml"),
        "youtube_accounts": _read_yaml(config_dir / "youtube_accounts.yaml"),
    }
    local = _read_yaml(config_dir / "local.yaml")
    merged = _deep_merge(base, local) if local else base
    for filename, key in (
        ("sources.local.yaml", "sources"),
        ("youtube_accounts.local.yaml", "youtube_accounts"),
        ("video_profiles.local.yaml", "video_profiles"),
    ):
        extra = _read_yaml(config_dir / filename)
        if extra:
            merged[key] = _deep_merge(dict(merged.get(key, {})), extra)
    channels = _load_channels(config_dir)
    return ProjectConfig(root=project_root, channels=channels, **merged)


def load_dotenv_files(project_root: Path) -> None:
    """Load environment from `.env` then `config/.env`, without overriding existing vars.

    The root `.env` is loaded first for backwards compatibility, then `config/.env`
    fills in any keys still missing. Neither overrides values already present in
    the process environment, so CI/service overrides keep priority.
    """
    from dotenv import load_dotenv

    for candidate in (project_root / ".env", project_root / "config" / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
