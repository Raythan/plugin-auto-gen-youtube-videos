from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StateStore:
    path: Path

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"seen_news_ids": [], "runs": []}
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def build_news_id(
        self, title: str, source_url: str, *, channel_id: str | None = None
    ) -> str:
        scope = (channel_id or "").strip()
        if scope:
            raw = f"{scope}::{title}::{source_url}"
        else:
            raw = f"{title}::{source_url}"
        return sha256(raw.encode("utf-8")).hexdigest()

    def has_seen(self, news_id: str) -> bool:
        payload = self._load()
        return news_id in payload.get("seen_news_ids", [])

    def mark_seen(self, news_id: str) -> None:
        payload = self._load()
        seen_ids = payload.setdefault("seen_news_ids", [])
        if news_id not in seen_ids:
            seen_ids.append(news_id)
        self._save(payload)

    def add_run(self, metadata: dict[str, Any]) -> None:
        payload = self._load()
        runs = payload.setdefault("runs", [])
        runs.append(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                **metadata,
            }
        )
        self._save(payload)

    def recent_topics(self, channel_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the last N topics generated for a channel as {date, title, topic_key}.

        Used to build the RECENT_TOPICS block injected into the freeform prompt
        so Gemini avoids repeating tool/angle combinations.
        """
        runs = self.recent_runs(channel_id=channel_id, limit=limit * 2)
        topics: list[dict[str, Any]] = []
        for run in runs:
            title = str(run.get("generated_title") or "").strip()
            topic_key = str(run.get("content_topic") or "").strip()
            date = str(run.get("timestamp_utc", ""))[:10]
            if title or topic_key:
                topics.append({"date": date, "title": title, "topic_key": topic_key})
            if len(topics) >= limit:
                break
        return topics

    def recent_visual_prompts(self, channel_id: str, limit: int = 10) -> list[str]:
        """Return the last N ``visual_prompts_hash`` values for a channel."""
        runs = self.recent_runs(channel_id=channel_id, limit=limit * 2)
        hashes: list[str] = []
        for run in runs:
            h = run.get("visual_prompts_hash")
            if isinstance(h, str) and h:
                hashes.append(h)
            if len(hashes) >= limit:
                break
        return hashes

    def recent_runs(
        self, channel_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return the most recent runs, optionally filtered by channel."""
        payload = self._load()
        runs: list[dict[str, Any]] = payload.get("runs", [])
        if channel_id:
            cid = channel_id.strip()
            runs = [
                r
                for r in runs
                if str(r.get("channel") or "").strip() == cid
                or str(r.get("schedule_profile") or "").strip() == cid
            ]
        sorted_runs = sorted(
            runs,
            key=lambda r: r.get("timestamp_utc", ""),
            reverse=True,
        )
        return sorted_runs[:limit]
