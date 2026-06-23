from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.content.gemini_rewriter import GeminiRewriter
from src.content.script_contract import validate_script_result
from src.content_queue import ContentQueue, resolve_content_source
from src.models import NewsItem
from src.scrapers.news_sources import NewsCollector
from src.upload_queue import (
    UploadQueue,
    script_from_manifest,
    video_from_manifest,
)
from src.utils.config import (
    CHANNELS_DIR_NAME,
    Channel,
    ProjectConfig,
    load_channel_yaml_by_stem,
    load_dotenv_files,
    load_project_config,
    merge_brand_from_raw,
    merge_channel_brand,
    merge_video_profile_with_channel,
    resolve_script_target,
)
from src.utils.oauth_helpers import (
    merge_oauth_from_yaml_dict,
    try_merge_oauth_from_convention_files,
)
from src.utils.logging_utils import setup_logging
from src.utils.state_store import StateStore
from src.video.short_builder import ShortBuilder
from src.youtube.upload_settings import youtube_upload_settings_from_channel
from src.youtube.uploader import YouTubeUploader

LOGGER = logging.getLogger("pipeline")


def _resolve_channel_id_alias(config: ProjectConfig, channel_key: str) -> str:
    aliases = config.pipeline.get("channel_id_aliases") or {}
    if not isinstance(aliases, dict):
        return channel_key
    mapped = aliases.get(channel_key)
    if mapped is None:
        return channel_key
    return str(mapped).strip() or channel_key


# Per-channel template rotation state for pick=rotate. Process-wide is fine
# because the console runs as a single long-lived process.
_ROTATION_CYCLES: dict[str, itertools.cycle] = {}


def _validate_channel_template_isolation(config: ProjectConfig, channel: Channel) -> None:
    """Ensure rpjtechgroup uses only rpjtechgroup_* video profiles."""
    if channel.id != "rpjtechgroup":
        return
    invalid = [
        name
        for name in channel.video_templates
        if not str(name).startswith("rpjtechgroup_")
    ]
    if invalid:
        raise ValueError(
            f"Channel {channel.id!r} must use templates prefixed with 'rpjtechgroup_'; "
            f"invalid: {invalid!r}."
        )


def reset_channel_caches(*, root: Path | None = None, wipe_tokens: bool = False) -> dict[str, Any]:
    """Clear processing state and queues so channels can restart from zero."""
    root = root or Path(__file__).resolve().parents[1]
    targets = [
        root / "data" / "processed" / "state.json",
        root / "data" / "pending_uploads",
        root / "data" / "uploaded",
    ]
    removed_files: list[str] = []
    for target in targets:
        if target.is_file():
            target.unlink(missing_ok=True)
            removed_files.append(str(target))
            continue
        if target.is_dir():
            for child in target.glob("*.json"):
                child.unlink(missing_ok=True)
                removed_files.append(str(child))
    removed_tokens: list[str] = []
    if wipe_tokens:
        for secrets_dir in (root / "config" / "secrets", root / "secrets"):
            if secrets_dir.is_dir():
                for token in secrets_dir.glob("*_token.json"):
                    token.unlink(missing_ok=True)
                    removed_tokens.append(str(token))
    for cache_name in ("audio", "temp"):
        cache_dir = root / "data" / cache_name
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            removed_files.append(str(cache_dir))
    LOGGER.info("Cache reset done. Removed %s entries.", len(removed_files) + len(removed_tokens))
    return {
        "removed_entries": removed_files,
        "removed_tokens": removed_tokens,
        "wipe_tokens": wipe_tokens,
    }


def _coerce_bool(value: Any, default: bool, source: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    LOGGER.warning("Invalid boolean for %s=%r. Using default=%s.", source, value, default)
    return default


def _runtime_flag(config: ProjectConfig, key: str, env_name: str, default: bool) -> bool:
    """Resolve a runtime flag honoring env override, then config/pipeline.yaml runtime, then default."""
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return _coerce_bool(env_value, default, env_name)
    return _coerce_bool(config.runtime.get(key), default, f"runtime.{key}")


def _is_freeform_channel(channel: Channel) -> bool:
    """True when the channel's content block uses gemini_freeform (no RSS)."""
    content = channel.raw.get("content") or {}
    if not isinstance(content, dict):
        return False
    return str(content.get("mode", "")).strip().lower() == "gemini_freeform"


def _freeform_seed_news(channel: Channel) -> NewsItem:
    """Synthetic NewsItem that tells Gemini to invent topic + script (no RSS text)."""
    content = channel.raw.get("content") or {}
    editorial = content.get("editorial") or {}
    focus = str(editorial.get("focus") or "ferramentas de IA para negocios").strip()
    categories = content.get("tool_categories") or []
    cats_text = "; ".join(str(c) for c in categories) if categories else "ferramentas de IA"
    return NewsItem(
        title="[gemini_freeform] ferramentas de IA para negocios",
        summary=(
            "Gatilho interno: invente o tema, roteiro e cenas visuais do zero. "
            f"Foco: {focus} "
            f"Categorias de ferramentas disponíveis: {cats_text}"
        ),
        source_url=f"{channel.id}://gemini-freeform",
        source_name="gemini_freeform",
        published_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _select_news(
    collector: NewsCollector,
    state_store: StateStore,
    max_candidates: int,
    channel: Channel,
) -> list[NewsItem]:
    collected = collector.collect(channel_id=channel.id)
    selected: list[NewsItem] = []
    for item in collected:
        news_id = state_store.build_news_id(
            item.title,
            item.source_url,
            channel_id=channel.id,
        )
        legacy_news_id = state_store.build_news_id(item.title, item.source_url)
        if state_store.has_seen(news_id) or state_store.has_seen(legacy_news_id):
            continue
        selected.append(item)
        state_store.mark_seen(news_id)
        if len(selected) >= max_candidates:
            break
    return selected


def _bootstrap(root: Path) -> ProjectConfig:
    load_dotenv_files(root)
    config = load_project_config(root)
    setup_logging(root / "logs")
    return config


def _resolve_queue(root: Path, config: ProjectConfig) -> UploadQueue:
    console_cfg = config.console
    queue_dir = root / console_cfg.get("queue_dir", "data/pending_uploads")
    uploaded_dir = root / console_cfg.get("uploaded_dir", "data/uploaded")
    return UploadQueue(queue_dir=queue_dir, uploaded_dir=uploaded_dir)


def _resolve_content_queue(root: Path, config: ProjectConfig) -> ContentQueue:
    bridge_cfg = config.pipeline.get("content_bridge") or {}
    inbox_dir = root / str(bridge_cfg.get("inbox_dir") or "data/pending_content")
    processed_dir = root / str(bridge_cfg.get("processed_dir") or "data/processed_content")
    return ContentQueue(inbox_dir=inbox_dir, processed_dir=processed_dir)


def _pick_template(channel: Channel) -> str:
    if not channel.video_templates:
        raise ValueError(f"Channel {channel.id!r} has no video_templates configured.")
    if len(channel.video_templates) == 1 or channel.template_pick == "random":
        return random.choice(channel.video_templates)
    cycle = _ROTATION_CYCLES.get(channel.id)
    if cycle is None:
        cycle = itertools.cycle(channel.video_templates)
        _ROTATION_CYCLES[channel.id] = cycle
    return next(cycle)


def _resolve_channel(config: ProjectConfig, channel_id: str | None) -> Channel:
    if channel_id:
        return config.channel(channel_id)
    enabled = config.enabled_channels
    if not enabled:
        raise RuntimeError(
            "No channels configured under config/channels_config_structure/."
        )
    return enabled[0]


def _delete_published_artifacts(payload: dict[str, Any], archive_manifest: Path) -> None:
    """Remove the rendered media and the archived manifest after a successful upload."""
    targets: list[Path] = []
    for key in ("video_path", "subtitle_path"):
        value = payload.get(key)
        if value:
            targets.append(Path(value))

    video_value = payload.get("video_path")
    if video_value:
        video_path = Path(video_value)
        for sibling_suffix in (".mp3", ".wav"):
            candidate = video_path.with_suffix(sibling_suffix)
            if candidate.exists():
                targets.append(candidate)

    targets.append(archive_manifest)

    for target in targets:
        try:
            if target.is_file():
                target.unlink()
                LOGGER.info("Deleted published artifact: %s", target)
        except OSError as exc:
            LOGGER.warning("Could not delete %s: %s", target, exc)


def run_generate_only(
    channel_id: str | None = None, *, root: Path | None = None
) -> dict | None:
    """Generate a single short video for a channel and enqueue it for upload."""

    root = root or Path(__file__).resolve().parents[1]
    config = _bootstrap(root)
    state_store = StateStore(root / "data" / "processed" / "state.json")

    channel = _resolve_channel(config, channel_id)
    _validate_channel_template_isolation(config, channel)
    template_name = _pick_template(channel)
    profile = merge_video_profile_with_channel(
        config.video_profile(template_name),
        channel.raw,
    )
    youtube_account_name = channel.youtube_account
    # Validate the account exists up-front so we fail before rendering a video.
    config.youtube_account(youtube_account_name)

    LOGGER.info(
        "Generation start | channel=%s | template=%s | youtube_account=%s",
        channel.id,
        template_name,
        youtube_account_name,
    )

    freeform = _is_freeform_channel(channel)
    if freeform:
        LOGGER.info(
            "Channel %s usa mode=gemini_freeform; pulando NewsCollector.",
            channel.id,
        )
        news_batch = [_freeform_seed_news(channel)]
    else:
        collector = NewsCollector(config=config, state_store=state_store)
        news_batch = _select_news(
            collector=collector,
            state_store=state_store,
            max_candidates=config.pipeline.get("news_items_per_video", 1),
            channel=channel,
        )
        if not news_batch:
            LOGGER.info("No new briefings/news available for channel %s.", channel.id)
            return None

    brand_for_channel = merge_channel_brand(config.brand, channel)
    script_target = resolve_script_target(config.pipeline, channel.raw)
    rewriter = GeminiRewriter(
        root=root,
        config=config,
        brand=brand_for_channel,
        channel_id=channel.id,
        script_target=script_target,
        channel_content=channel.raw.get("content"),
    )
    gemini_enabled = _runtime_flag(config, "use_gemini", "PIPELINE_USE_GEMINI", True)
    if gemini_enabled:
        script = rewriter.generate_script(news_batch)
    else:
        LOGGER.info("PIPELINE_USE_GEMINI is false, using local fallback script.")
        script = rewriter.generate_fallback_script(news_batch)

    # Anti-repetição: registrar topic_key no state após geração bem-sucedida (freeform)
    if freeform and script.topic_key:
        topic_id = state_store.build_news_id(
            script.topic_key, f"{channel.id}://topic", channel_id=channel.id
        )
        state_store.mark_seen(topic_id)

    image_paths: list[Path] = []
    run_img_dir: Path | None = None
    if profile.get("visual_mode") == "ai_slides" and script.visual_scenes:
        from src.media.image_generator import ImageGenerator

        img_gen = ImageGenerator(root=root, config=config)
        slug_seed = script.topic_key or news_batch[0].title
        run_slug = state_store.build_news_id(
            slug_seed, news_batch[0].source_url, channel_id=channel.id
        )[:12]
        run_img_dir = root / "output" / "images" / run_slug
        run_img_dir.mkdir(parents=True, exist_ok=True)
        image_paths = img_gen.generate_for_scenes(
            script.visual_scenes, run_img_dir, news_id=run_slug
        )
        if not image_paths:
            raise RuntimeError(
                f"Nenhuma imagem gerada para o canal {channel.id}. "
                "Configure chaves em .env ou config/secrets/*_keys.txt (ver config/env.example)."
            )

    short_builder = ShortBuilder(root=root, config=config)
    video = short_builder.build(script=script, video_profile=profile, image_paths=image_paths or None)

    if image_paths and run_img_dir:
        after_use = (
            profile.get("image_gen", {}).get("after_use")
            or config.pipeline.get("image_gen", {}).get("after_use")
        )
        if after_use == "delete":
            try:
                shutil.rmtree(run_img_dir, ignore_errors=True)
                LOGGER.info("Imagens temporarias removidas: %s", run_img_dir)
            except OSError as exc:
                LOGGER.warning("Nao foi possivel remover imagens em %s: %s", run_img_dir, exc)

    queue = _resolve_queue(root, config)
    manifest_path = queue.enqueue(
        script=script,
        video=video,
        schedule_profile=channel.id,
        youtube_account=youtube_account_name,
        video_template=template_name,
    )

    run_data = {
        "phase": "generate",
        "channel": channel.id,
        "video_template": template_name,
        "news_urls": [item.source_url for item in news_batch],
        "generated_title": script.title,
        "content_topic": script.topic_key or "",
        "video_path": str(video.video_path),
        "subtitle_path": str(video.subtitle_path),
        "manifest_path": str(manifest_path),
        "account": youtube_account_name,
        "gemini_enabled": gemini_enabled,
        "pending_upload": True,
    }
    state_store.add_run(run_data)
    with (root / "logs" / "last_run.json").open("w", encoding="utf-8") as handle:
        json.dump(run_data, handle, ensure_ascii=False, indent=2)
    LOGGER.info(
        "Generation finished for channel %s: %s queued for upload.",
        channel.id,
        manifest_path.name,
    )
    return run_data


def run_render_from_content(
    channel_id: str | None = None, *, root: Path | None = None
) -> dict | None:
    """Render a video from the oldest plugin content package for a channel."""

    root = root or Path(__file__).resolve().parents[1]
    config = _bootstrap(root)
    state_store = StateStore(root / "data" / "processed" / "state.json")

    channel = _resolve_channel(config, channel_id)
    source = resolve_content_source(config.pipeline, channel.raw)
    if source != "plugin":
        LOGGER.warning(
            "Channel %s usa content.source=%r; render-content requer 'plugin'.",
            channel.id,
            source,
        )
        return None

    _validate_channel_template_isolation(config, channel)
    template_name = _pick_template(channel)
    profile = merge_video_profile_with_channel(
        config.video_profile(template_name),
        channel.raw,
    )
    youtube_account_name = channel.youtube_account
    config.youtube_account(youtube_account_name)

    content_queue = _resolve_content_queue(root, config)
    manifest = content_queue.peek_oldest(channel.id)
    if manifest is None:
        LOGGER.info("Nenhum pacote de conteudo pendente para o canal %s.", channel.id)
        return None

    package_dir = Path(manifest["_package_dir"])
    image_paths = [Path(p) for p in manifest.get("_image_paths", [])]
    script_target = resolve_script_target(config.pipeline, channel.raw)
    script = content_queue.validate_package(
        manifest, image_paths, script_target=script_target
    )
    validate_script_result(
        script,
        script_target,
        require_topic_key=bool(script.topic_key),
        require_visual_scenes=True,
    )

    package_id = str(manifest.get("id") or package_dir.name)
    LOGGER.info(
        "Render from content | channel=%s | package=%s | images=%d",
        channel.id,
        package_id,
        len(image_paths),
    )

    short_builder = ShortBuilder(root=root, config=config)
    video = short_builder.build(
        script=script, video_profile=profile, image_paths=image_paths
    )

    upload_queue = _resolve_queue(root, config)
    manifest_path = upload_queue.enqueue(
        script=script,
        video=video,
        schedule_profile=channel.id,
        youtube_account=youtube_account_name,
        video_template=template_name,
    )

    content_queue.mark_processed(package_dir)

    if script.topic_key:
        topic_id = state_store.build_news_id(
            script.topic_key, f"{channel.id}://topic", channel_id=channel.id
        )
        state_store.mark_seen(topic_id)

    run_data = {
        "phase": "render_content",
        "channel": channel.id,
        "content_package_id": package_id,
        "video_template": template_name,
        "generated_title": script.title,
        "content_topic": script.topic_key or "",
        "video_path": str(video.video_path),
        "subtitle_path": str(video.subtitle_path),
        "manifest_path": str(manifest_path),
        "account": youtube_account_name,
        "pending_upload": True,
    }
    state_store.add_run(run_data)
    with (root / "logs" / "last_run.json").open("w", encoding="utf-8") as handle:
        json.dump(run_data, handle, ensure_ascii=False, indent=2)
    LOGGER.info(
        "Render finished for channel %s from package %s: %s queued.",
        channel.id,
        package_id,
        manifest_path.name,
    )
    return run_data


def run_publish_next(*, root: Path | None = None) -> dict | None:
    """Pop the oldest pending video, upload it to YouTube, and clean up files on success."""

    root = root or Path(__file__).resolve().parents[1]
    config = _bootstrap(root)
    state_store = StateStore(root / "data" / "processed" / "state.json")

    upload_enabled = _runtime_flag(config, "upload_youtube", "PIPELINE_UPLOAD_YOUTUBE", True)
    if not upload_enabled:
        LOGGER.info("PIPELINE_UPLOAD_YOUTUBE is false, skipping publish.")
        return None

    queue = _resolve_queue(root, config)
    payload = queue.peek_oldest()
    if payload is None:
        return None

    manifest_path = Path(payload["_manifest_path"])
    video_path = Path(payload["video_path"])

    if not video_path.is_file():
        LOGGER.error(
            "Stale queue entry: video file is missing (%s). Removing manifest %s "
            "so the next pending item can publish. Regenerate the video if you still need it.",
            video_path,
            manifest_path.name,
        )
        manifest_path.unlink(missing_ok=True)
        return None

    youtube_account_name = payload["youtube_account"]
    youtube_account = dict(config.youtube_account(youtube_account_name))

    script = script_from_manifest(payload)
    video = video_from_manifest(payload)

    channel_key = str(payload.get("schedule_profile") or "").strip()
    resolved_key = _resolve_channel_id_alias(config, channel_key) if channel_key else ""

    publish_channel: Channel | None = None
    raw_fallback: dict[str, Any] = {}

    if resolved_key:
        try:
            publish_channel = config.channel(resolved_key)
        except ValueError:
            publish_channel = None

    if publish_channel is not None:
        merge_oauth_from_yaml_dict(
            youtube_account,
            publish_channel.raw,
            log_as=publish_channel.id,
            logger=LOGGER,
        )
        yo = publish_channel.raw.get("youtube_oauth")
        yo_complete = (
            isinstance(yo, dict)
            and str(yo.get("client_id") or "").strip()
            and str(yo.get("client_secret") or "").strip()
        )
        if not yo_complete and not youtube_account.get("oauth_client_id"):
            LOGGER.info(
                "Channel %s: no complete youtube_oauth block in channel YAML; "
                "will try root/installed keys in the same file, then convention *_oauth.yaml.",
                publish_channel.id,
            )
        if channel_key and channel_key != resolved_key:
            LOGGER.info(
                "Resolved schedule_profile %r to channel id %r via channel_id_aliases.",
                channel_key,
                resolved_key,
            )
    elif channel_key:
        for stem, log_label in ((resolved_key, resolved_key), (channel_key, channel_key)):
            if not stem:
                continue
            candidate = load_channel_yaml_by_stem(root, stem)
            if candidate:
                raw_fallback = candidate
                merge_oauth_from_yaml_dict(
                    youtube_account,
                    raw_fallback,
                    log_as=log_label,
                    logger=LOGGER,
                )
                LOGGER.info(
                    "Loaded youtube_oauth from channels_config_structure/%s.yaml "
                    "(no loaded Channel for schedule_profile=%r).",
                    stem,
                    channel_key,
                )
                break

    channel_raw: dict[str, Any] = {}
    if publish_channel is not None:
        brand_for_upload = merge_channel_brand(config.brand, publish_channel)
        channel_raw = publish_channel.raw
    elif raw_fallback:
        brand_for_upload = merge_brand_from_raw(config.brand, raw_fallback)
        channel_raw = raw_fallback
    else:
        brand_for_upload = dict(config.brand)

    upload_settings = youtube_upload_settings_from_channel(channel_raw)
    if upload_settings is None:
        channel_hint = resolved_key or channel_key or youtube_account_name
        raise ValueError(
            f"Canal {channel_hint!r}: falta bloco youtube_upload com category_id e "
            "contains_synthetic_media em config/channels_config_structure/<id>.yaml. "
            "Rode: python -m src.youtube.check_upload_config"
        )

    try_merge_oauth_from_convention_files(
        root,
        youtube_account,
        youtube_account_name,
        logger=LOGGER,
    )
    if (
        not youtube_account.get("oauth_client_id")
        and not youtube_account.get("client_secret_file")
    ):
        LOGGER.info(
            "YouTube account %r: still no OAuth client credentials. Options: "
            "youtube_oauth in config/%s/<channel>.yaml, client_secret_file in youtube_accounts.yaml, "
            "or convention files config/%s/%s_oauth.yaml or config/secrets/%s_oauth.yaml "
            "(legacy: secrets/%s_oauth.yaml).",
            youtube_account_name,
            CHANNELS_DIR_NAME,
            CHANNELS_DIR_NAME,
            youtube_account_name,
            youtube_account_name,
            youtube_account_name,
        )

    uploader = YouTubeUploader(
        root=root,
        account_config=youtube_account,
        brand=brand_for_upload,
        upload_settings=upload_settings,
        account_label=youtube_account_name,
    )
    upload_result = uploader.upload(video=video, script=script)
    archive_manifest = queue.mark_uploaded(manifest_path, upload_result)

    _delete_published_artifacts(payload, archive_manifest)

    run_data = {
        "phase": "publish",
        "channel": payload.get("schedule_profile"),
        "video_template": payload.get("video_template"),
        "manifest_path": str(manifest_path),
        "video_path": payload["video_path"],
        "youtube_video_id": upload_result.get("id"),
        "account": youtube_account_name,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    state_store.add_run(run_data)
    with (root / "logs" / "last_run.json").open("w", encoding="utf-8") as handle:
        json.dump(run_data, handle, ensure_ascii=False, indent=2)
    LOGGER.info(
        "Publish finished for channel %s: video id=%s",
        payload.get("schedule_profile"),
        upload_result.get("id"),
    )
    return run_data


def main() -> None:
    parser = argparse.ArgumentParser(description="RPJ Tech Group YouTube pipeline")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "console", help="Start the long-running console (recommended; runs forever)."
    )

    generate_parser = sub.add_parser(
        "generate", help="Generate one video for a channel and enqueue it (debug)."
    )
    generate_parser.add_argument(
        "--channel",
        default=None,
        help="Channel id from config/channels_config_structure/ (e.g. rpjtechgroup). Defaults to the first enabled channel.",
    )

    render_parser = sub.add_parser(
        "render-content",
        help="Render the oldest plugin content package for a channel and enqueue upload.",
    )
    render_parser.add_argument(
        "--channel",
        default=None,
        help="Channel id (e.g. rpjtechgroup). Defaults to the first enabled channel.",
    )

    sub.add_parser(
        "publish", help="Publish the oldest pending video and clean up files (debug)."
    )
    reset_parser = sub.add_parser(
        "reset-cache",
        help="Clear state/queue/upload cache (optional token cleanup).",
    )
    reset_parser.add_argument(
        "--wipe-tokens",
        action="store_true",
        help="Also remove *_token.json under config/secrets/ (and legacy secrets/) to force OAuth re-auth.",
    )

    sub.add_parser(
        "check-oauth",
        help="Check YouTube OAuth token health per enabled channel (no browser).",
    )

    reauth_parser = sub.add_parser(
        "oauth-reauth",
        help="Interactive OAuth re-auth for one channel (opens browser).",
    )
    reauth_parser.add_argument(
        "--channel",
        required=True,
        help="Channel id from config/channels_config_structure/ (e.g. rpjtechgroup).",
    )

    args = parser.parse_args()
    command = args.command

    if command == "console":
        from src.console_runner import run_console_loop

        run_console_loop()
        return
    if command == "generate":
        run_generate_only(args.channel)
        return
    if command == "render-content":
        run_render_from_content(args.channel)
        return
    if command == "publish":
        run_publish_next()
        return
    if command == "reset-cache":
        reset_channel_caches(wipe_tokens=bool(args.wipe_tokens))
        return
    if command == "check-oauth":
        from src.youtube.check_oauth_tokens import main as check_oauth_main

        raise SystemExit(check_oauth_main([]))
    if command == "oauth-reauth":
        root = Path(__file__).resolve().parents[1]
        load_dotenv_files(root)
        config = load_project_config(root)
        setup_logging(root / "logs")
        from src.youtube.oauth_status import run_channel_oauth_reauth

        token_path = run_channel_oauth_reauth(root, config, args.channel)
        LOGGER.info("OAuth reauth concluído para canal %s. Token em %s", args.channel, token_path)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
