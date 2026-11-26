"""SQLite persistence for seen YouTube videos, channel ID cache, and podcast episodes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable


@dataclass(slots=True)
class EpisodeRecord:
    guid: str
    title: str
    summary: str
    audio_filename: str
    audio_url: str
    byte_size: int
    duration_seconds: int
    published_at: datetime
    video_ids: list[str]
    source_links: list[dict[str, str]]


class StateStore:
    """Manages SQLite state for deduplication, channel resolution caching, and episode history."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_videos (
                    video_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_cache (
                    channel_input TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    guid TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    audio_filename TEXT NOT NULL,
                    audio_url TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    published_at TEXT NOT NULL,
                    video_ids_json TEXT NOT NULL,
                    source_links_json TEXT NOT NULL
                );
                """
            )

    def is_video_seen(self, video_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return row is not None

    def mark_videos_seen(self, videos: Iterable[dict[str, str]]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                v["video_id"],
                v.get("title", ""),
                v.get("channel_name", ""),
                v.get("url", ""),
                v.get("published_at", now_iso),
                now_iso,
            )
            for v in videos
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO seen_videos
                    (video_id, title, channel_name, url, published_at, processed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_cached_channel(self, channel_input: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT channel_id, channel_name FROM channel_cache WHERE channel_input = ?",
            (channel_input,),
        ).fetchone()
        if row is None:
            return None
        return str(row["channel_id"]), str(row["channel_name"])

    def cache_channel(
        self, channel_input: str, channel_id: str, channel_name: str = ""
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO channel_cache (channel_input, channel_id, channel_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_input) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    channel_name = excluded.channel_name,
                    updated_at = excluded.updated_at
                """,
                (channel_input, channel_id, channel_name, now_iso),
            )

    def record_episode(self, episode: EpisodeRecord) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO episodes (
                    guid, title, summary, audio_filename, audio_url,
                    byte_size, duration_seconds, published_at,
                    video_ids_json, source_links_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guid) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    audio_filename = excluded.audio_filename,
                    audio_url = excluded.audio_url,
                    byte_size = excluded.byte_size,
                    duration_seconds = excluded.duration_seconds,
                    published_at = excluded.published_at,
                    video_ids_json = excluded.video_ids_json,
                    source_links_json = excluded.source_links_json
                """,
                (
                    episode.guid,
                    episode.title,
                    episode.summary,
                    episode.audio_filename,
                    episode.audio_url,
                    episode.byte_size,
                    episode.duration_seconds,
                    episode.published_at.astimezone(timezone.utc).isoformat(),
                    json.dumps(episode.video_ids, ensure_ascii=False),
                    json.dumps(episode.source_links, ensure_ascii=False),
                ),
            )

    def list_episodes(self, limit: int | None = None) -> list[EpisodeRecord]:
        sql = "SELECT * FROM episodes ORDER BY published_at DESC"
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self._conn.execute(sql, params).fetchall()
        results: list[EpisodeRecord] = []
        for row in rows:
            published_at = datetime.fromisoformat(row["published_at"])
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            results.append(
                EpisodeRecord(
                    guid=row["guid"],
                    title=row["title"],
                    summary=row["summary"],
                    audio_filename=row["audio_filename"],
                    audio_url=row["audio_url"],
                    byte_size=int(row["byte_size"]),
                    duration_seconds=int(row["duration_seconds"]),
                    published_at=published_at,
                    video_ids=json.loads(row["video_ids_json"]),
                    source_links=json.loads(row["source_links_json"]),
                )
            )
        return results
