from __future__ import annotations

import logging
from datetime import datetime

# Briefings use a fixed old timestamp so real RSS items (recent dates) sort first.
_BRIEFING_SORT_DATE = datetime(2000, 1, 1, 0, 0, 0)
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import requests
from bs4 import BeautifulSoup

from src.models import NewsItem
from src.utils.config import ProjectConfig
from src.utils.state_store import StateStore

LOGGER = logging.getLogger(__name__)


class NewsCollector:
    def __init__(self, config: ProjectConfig, state_store: StateStore) -> None:
        self.config = config
        self.state_store = state_store

    def collect(self, channel_id: str) -> list[NewsItem]:
        sources = self._resolve_sources_for_channel(channel_id)
        items: list[NewsItem] = []
        for source in sources:
            source_type = source.get("type", "rss")
            if source_type == "rss":
                items.extend(self._collect_from_rss(source))
            elif source_type == "html":
                items.extend(self._collect_from_html(source))
            elif source_type == "briefings":
                items.extend(self._collect_from_briefings(source))
        ranked = sorted(items, key=lambda i: i.published_at, reverse=True)
        return self._deduplicate(ranked, channel_id=channel_id)

    def _resolve_sources_for_channel(self, channel_id: str) -> list[dict[str, Any]]:
        sources = self.config.sources.get("sources", [])
        channel_sources = self.config.source_names_for_channel(channel_id)
        if channel_sources is not None and len(channel_sources) == 0:
            LOGGER.info(
                "Channel %s has an empty source allowlist; skipping RSS/briefings collection.",
                channel_id,
            )
            return []
        if not channel_sources:
            return list(sources)
        filtered = [
            source
            for source in sources
            if str(source.get("name", "")).strip() in channel_sources
        ]
        if not filtered:
            LOGGER.warning(
                "No source names matched channel_sources for channel=%s; using global sources.",
                channel_id,
            )
            return list(sources)
        return filtered

    def _collect_from_briefings(self, source: dict[str, Any]) -> list[NewsItem]:
        result: list[NewsItem] = []
        for entry in source.get("items", []) or []:
            briefing_id = str(entry.get("id") or "").strip()
            title = str(entry.get("title") or "").strip()
            if not briefing_id or not title:
                continue
            summary = str(entry.get("summary") or "").strip()
            stable_url = entry.get("source_url") or f"rpj://briefings/{briefing_id}"
            result.append(
                NewsItem(
                    title=title,
                    summary=summary,
                    source_url=str(stable_url),
                    source_name=source.get("name", "RPJ Tech Group Briefings"),
                    published_at=_BRIEFING_SORT_DATE,
                )
            )
        return result

    def _collect_from_rss(self, source: dict[str, Any]) -> list[NewsItem]:
        feed = feedparser.parse(source["url"])
        result: list[NewsItem] = []
        limit = source.get("max_items", 5)
        for entry in feed.entries[:limit]:
            published_raw = entry.get("published", "")
            if published_raw:
                published = parsedate_to_datetime(published_raw)
            else:
                published = datetime.utcnow()
            result.append(
                NewsItem(
                    title=entry.get("title", "No title"),
                    summary=entry.get("summary", ""),
                    source_url=entry.get("link", source["url"]),
                    source_name=source["name"],
                    published_at=published.replace(tzinfo=None),
                )
            )
        return result

    def _collect_from_html(self, source: dict[str, Any]) -> list[NewsItem]:
        response = requests.get(source["url"], timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        selector = source.get("selector", "a")
        links = soup.select(selector)
        result: list[NewsItem] = []
        limit = source.get("max_items", 5)
        for link in links[:limit]:
            title = (link.get_text() or "").strip()
            href = link.get("href")
            if not title or not href:
                continue
            result.append(
                NewsItem(
                    title=title,
                    summary="",
                    source_url=href,
                    source_name=source["name"],
                    published_at=datetime.utcnow(),
                )
            )
        return result

    def _deduplicate(self, items: list[NewsItem], *, channel_id: str) -> list[NewsItem]:
        seen: set[str] = set()
        unique_items: list[NewsItem] = []
        for item in items:
            key = self.state_store.build_news_id(
                item.title,
                item.source_url,
                channel_id=channel_id,
            )
            if key in seen:
                continue
            seen.add(key)
            unique_items.append(item)
        LOGGER.info("Collected %s unique items", len(unique_items))
        return unique_items
