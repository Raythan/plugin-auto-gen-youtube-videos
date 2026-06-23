"""Long-running console for the RPJ Tech Group pipeline.

Three async loops cooperate forever in a single process:

* ``_generate_loop`` checks every channel each tick and fires
  ``run_generate_only(channel_id=...)`` when ``HH:MM`` matches one of the
  channel's ``generate_times``. A global ``asyncio.Lock`` keeps generation
  sequential, so Gemini calls never overlap and the rewriter's per-process
  spacing/cooldown stays meaningful.
* ``_publish_loop`` polls the queue at a short interval and uploads the
  oldest pending video; on success the rendered files are deleted by the
  pipeline.
* ``_keep_alive_loop`` performs a tiny activity (HTTP GET or local ping)
  every ``interval_seconds`` so the OS doesn't decide to sleep.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.content_bridge import build_bridge_from_config
from src.content_queue import resolve_content_source
from src.pipeline import run_generate_only, run_publish_next, run_render_from_content
from src.utils.config import (
    Channel,
    ProjectConfig,
    load_dotenv_files,
    load_project_config,
)
from src.utils.logging_utils import setup_logging

LOGGER = logging.getLogger("console")

_DEFAULT_KEEP_ALIVE_URL = "https://www.gstatic.com/generate_204"
# Retry a missed generate slot if the job failed shortly after the scheduled minute.
_GENERATE_SLOT_GRACE_MINUTES = 20


class _ConsoleState:
    def __init__(self) -> None:
        self.fired_generate: set[tuple[str, str, str]] = set()
        self.stop = asyncio.Event()
        self.generation_lock = asyncio.Lock()


def _prune_fired_generate(state: _ConsoleState, today: str) -> None:
    for marker in list(state.fired_generate):
        if marker[0] != today:
            state.fired_generate.discard(marker)


def _minutes_since_slot(now: datetime, slot_hhmm: str) -> int:
    hour_str, minute_str = slot_hhmm.split(":", 1)
    slot_dt = now.replace(
        hour=int(hour_str),
        minute=int(minute_str),
        second=0,
        microsecond=0,
    )
    return int((now - slot_dt).total_seconds() // 60)


def _due_generate_slot(
    state: _ConsoleState, channel_id: str, now: datetime, slots: list[str]
) -> str | None:
    today = now.strftime("%Y-%m-%d")
    _prune_fired_generate(state, today)
    for slot in slots:
        elapsed = _minutes_since_slot(now, slot)
        if elapsed < 0 or elapsed > _GENERATE_SLOT_GRACE_MINUTES:
            continue
        key = (today, channel_id, slot)
        if key in state.fired_generate:
            continue
        return slot
    return None


async def _sleep_until_stop(state: _ConsoleState, seconds: float) -> None:
    try:
        await asyncio.wait_for(state.stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _generate_loop(
    state: _ConsoleState,
    tz: ZoneInfo,
    channels: list[Channel],
    interval: int,
    config: ProjectConfig,
) -> None:
    if not channels:
        LOGGER.warning("No enabled channels found; generate loop will idle.")
    else:
        LOGGER.info(
            "Generate loop ready for channels: %s",
            ", ".join(f"{c.id}({len(c.generate_times)} slots)" for c in channels),
        )
    while not state.stop.is_set():
        now = datetime.now(tz)
        for channel in channels:
            if not channel.generate_times:
                continue
            slot = _due_generate_slot(state, channel.id, now, channel.generate_times)
            if slot is None:
                continue
            today = now.strftime("%Y-%m-%d")
            key = (today, channel.id, slot)
            LOGGER.info("Generate slot triggered: channel=%s slot=%s", channel.id, slot)
            async with state.generation_lock:
                try:
                    source = resolve_content_source(config.pipeline, channel.raw)
                    if source == "plugin":
                        result = await asyncio.to_thread(
                            run_render_from_content, channel.id
                        )
                        if result is None:
                            LOGGER.warning(
                                "Slot %s: fila de conteudo vazia para canal %s, pulando.",
                                slot,
                                channel.id,
                            )
                    else:
                        await asyncio.to_thread(run_generate_only, channel.id)
                except Exception:  # noqa: BLE001
                    LOGGER.exception(
                        "Generate job failed for channel %s at %s.", channel.id, slot
                    )
                else:
                    state.fired_generate.add(key)
        await _sleep_until_stop(state, interval)


async def _publish_loop(
    state: _ConsoleState,
    poll_interval: int,
) -> None:
    LOGGER.info("Publish loop ready (poll every %ss).", poll_interval)
    while not state.stop.is_set():
        try:
            await asyncio.to_thread(run_publish_next)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Publish job failed.")
        await _sleep_until_stop(state, poll_interval)


def _do_keep_alive(mode: str, target: str, timeout: int) -> None:
    if mode == "http_get":
        request = urllib.request.Request(
            target, headers={"User-Agent": "rpj-auto-marketing-keep-alive/1.0"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            response.read(64)
        return
    if mode == "process":
        cmd = target.split() if target else (
            ["ping", "-n", "1", "1.1.1.1"] if os.name == "nt" else ["ping", "-c", "1", "1.1.1.1"]
        )
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        return
    raise ValueError(f"Unknown keep_alive mode: {mode!r}")


async def _keep_alive_loop(state: _ConsoleState, cfg: dict[str, Any]) -> None:
    interval = max(60, int(cfg.get("interval_seconds", 3600)))
    mode = str(cfg.get("mode", "http_get")).strip().lower()
    target = str(cfg.get("target", _DEFAULT_KEEP_ALIVE_URL if mode == "http_get" else "")).strip()
    timeout = max(1, int(cfg.get("timeout_seconds", 10)))
    LOGGER.info(
        "Keep-alive loop ready (mode=%s target=%s every=%ss).",
        mode,
        target or "<default>",
        interval,
    )
    # Wait one interval before the first ping so logs aren't noisy at startup.
    await _sleep_until_stop(state, interval)
    while not state.stop.is_set():
        try:
            await asyncio.to_thread(_do_keep_alive, mode, target, timeout)
            LOGGER.debug("Keep-alive ping ok (mode=%s).", mode)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Keep-alive ping failed: %s", exc)
        await _sleep_until_stop(state, interval)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, state: _ConsoleState) -> None:
    def _handler() -> None:
        LOGGER.info("Stop signal received, shutting down console loops.")
        state.stop.set()

    if os.name == "nt":
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(state.stop.set))
        try:
            signal.signal(
                signal.SIGTERM,
                lambda *_: loop.call_soon_threadsafe(state.stop.set),
            )
        except (AttributeError, ValueError):
            pass
        return

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            pass


async def _main(config: ProjectConfig) -> int:
    console_cfg = config.console
    if not console_cfg.get("enabled", True):
        LOGGER.error("console.enabled=false in config/pipeline.yaml. Aborting.")
        return 2

    tz_name = console_cfg.get("timezone") or "America/Sao_Paulo"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        LOGGER.warning("Invalid timezone %r; falling back to UTC.", tz_name)
        tz = ZoneInfo("UTC")

    interval = max(5, int(console_cfg.get("check_interval_seconds", 30)))
    publish_cfg = console_cfg.get("publish") or {}
    publish_poll = max(10, int(publish_cfg.get("poll_interval_seconds", 60)))
    keep_alive_cfg = console_cfg.get("keep_alive") or {}

    channels = config.enabled_channels
    LOGGER.info(
        "Console started | tz=%s | tick=%ss | publish_poll=%ss | channels=%d",
        tz_name,
        interval,
        publish_poll,
        len(channels),
    )
    LOGGER.info("Press Ctrl+C to stop.")

    state = _ConsoleState()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, state)

    bridge = build_bridge_from_config(config.root, config.pipeline)
    if bridge:
        bridge.start()
    else:
        bridge = None

    tasks = [
        asyncio.create_task(_generate_loop(state, tz, channels, interval, config)),
        asyncio.create_task(_publish_loop(state, publish_poll)),
        asyncio.create_task(_keep_alive_loop(state, keep_alive_cfg)),
    ]
    try:
        await state.stop.wait()
    finally:
        if bridge:
            bridge.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    LOGGER.info("Console stopped.")
    return 0


def run_console_loop() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv_files(root)
    config = load_project_config(root)
    setup_logging(root / "logs")
    from src.utils.gemini_keys import resolve_gemini_keys

    if not resolve_gemini_keys(root, config):
        LOGGER.warning(
            "No Gemini API key found; generation will use the local fallback script."
        )
    if not config.enabled_channels:
        LOGGER.error(
            "No enabled channels in config/channels_config_structure/. Add at least one YAML."
        )
        return 2
    return asyncio.run(_main(config))


if __name__ == "__main__":
    raise SystemExit(run_console_loop())
