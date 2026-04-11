"""SQLite storage for verified sources."""

from __future__ import annotations

import sqlite3
from datetime import datetime
import json
from pathlib import Path

from .models import EpgInfo, ResolvedStream, SongInfo


class SourceDatabase:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verified_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_url TEXT NOT NULL,
                    station_name TEXT,
                    resolved_url TEXT NOT NULL,
                    delivery_url TEXT,
                    content_type TEXT,
                    was_playlist INTEGER NOT NULL,
                    last_stream_title TEXT,
                    last_artist TEXT,
                    last_title TEXT,
                    song_source_kind TEXT,
                    song_source_url TEXT,
                    raw_metadata TEXT,
                    source_headers TEXT,
                    epg_available INTEGER NOT NULL DEFAULT 0,
                    epg_source_url TEXT,
                    epg_summary TEXT,
                    verified_at TEXT NOT NULL,
                    UNIQUE(input_url, resolved_url)
                )
                """
            )
            self._ensure_column(conn, "verified_sources", "station_name", "TEXT")
            self._ensure_column(conn, "verified_sources", "delivery_url", "TEXT")
            self._ensure_column(conn, "verified_sources", "source_headers", "TEXT")
            self._ensure_column(conn, "verified_sources", "last_artist", "TEXT")
            self._ensure_column(conn, "verified_sources", "last_title", "TEXT")
            self._ensure_column(conn, "verified_sources", "song_source_kind", "TEXT")
            self._ensure_column(conn, "verified_sources", "song_source_url", "TEXT")
            self._ensure_column(conn, "verified_sources", "epg_available", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "verified_sources", "epg_source_url", "TEXT")
            self._ensure_column(conn, "verified_sources", "epg_summary", "TEXT")
            conn.commit()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing_columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_verified_source(self, resolved: ResolvedStream, song: SongInfo, epg: EpgInfo | None = None) -> None:
        verified_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        headers_json = json.dumps(song.source_headers, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO verified_sources (
                    input_url,
                    station_name,
                    resolved_url,
                    delivery_url,
                    content_type,
                    was_playlist,
                    last_stream_title,
                    last_artist,
                    last_title,
                    song_source_kind,
                    song_source_url,
                    raw_metadata,
                    source_headers,
                    epg_available,
                    epg_source_url,
                    epg_summary,
                    verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(input_url, resolved_url) DO UPDATE SET
                    station_name = excluded.station_name,
                    delivery_url = excluded.delivery_url,
                    content_type = excluded.content_type,
                    was_playlist = excluded.was_playlist,
                    last_stream_title = excluded.last_stream_title,
                    last_artist = excluded.last_artist,
                    last_title = excluded.last_title,
                    song_source_kind = excluded.song_source_kind,
                    song_source_url = excluded.song_source_url,
                    raw_metadata = excluded.raw_metadata,
                    source_headers = excluded.source_headers,
                    epg_available = excluded.epg_available,
                    epg_source_url = excluded.epg_source_url,
                    epg_summary = excluded.epg_summary,
                    verified_at = excluded.verified_at
                """,
                (
                    resolved.input_url,
                    resolved.station_name or None,
                    resolved.resolved_url,
                    resolved.delivery_url or None,
                    resolved.content_type,
                    1 if resolved.was_playlist else 0,
                    song.stream_title,
                    song.artist or None,
                    song.title or None,
                    song.source_kind or None,
                    song.source_url or None,
                    song.raw_metadata,
                    headers_json,
                    1 if epg and epg.available else 0,
                    epg.source_url if epg else None,
                    epg.summary if epg else None,
                    verified_at,
                ),
            )
            conn.commit()
