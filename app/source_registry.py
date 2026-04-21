"""Read access for verified source hints stored by the Kodi bridge."""

from __future__ import annotations

import sqlite3
import time
import json
from typing import Callable


class VerifiedSourceRepository:
    def __init__(
        self,
        db_path: str,
        normalize_url: Callable[[str], str] | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._db_path = str(db_path or "").strip()
        self._normalize_url = normalize_url or (lambda value: str(value or "").strip().lower())
        self._log = log

    def _trace(self, event: str, detail: str = "") -> None:
        if not self._log:
            return
        self._log(event, detail)

    def get_preferred_source(
        self,
        station_key: str,
        max_age_seconds: int = 0,
        *,
        allow_name_fallback: bool = False,
        min_name_tokens: int = 2,
        max_name_candidates: int = 6,
    ) -> dict | None:
        key = str(station_key or "").strip()
        if not key or not self._db_path:
            return None

        now_ts = int(time.time())
        row = None
        try:
            conn = sqlite3.connect(self._db_path, timeout=1.0)
            try:
                row = conn.execute(
                    """
                    SELECT source_url, source_url_norm, confidence, last_seen_ts, verified_at_utc, meta_json
                    FROM verified_station_sources
                    WHERE station_key = ?
                    ORDER BY confidence DESC, last_seen_ts DESC
                    LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                if not row and allow_name_fallback:
                    row = self._lookup_name_fallback(
                        conn=conn,
                        station_key=key,
                        min_name_tokens=min_name_tokens,
                        max_name_candidates=max_name_candidates,
                    )
            finally:
                conn.close()
        except Exception as err:
            self._trace("verified_source_lookup_failed", str(err))
            return None

        if not row:
            self._trace("verified_source_lookup_miss", key)
            return None

        source_url = str(row[0] or "").strip()
        source_url_norm = self._normalize_url(source_url)
        if not source_url or not source_url_norm:
            return None

        last_seen_ts = int(row[3] or 0)
        if max_age_seconds > 0 and last_seen_ts > 0:
            age_seconds = max(0, now_ts - last_seen_ts)
            if age_seconds > int(max_age_seconds):
                self._trace("verified_source_lookup_stale", f"{key}:{age_seconds}")
                return None

        return {
            "station_key": key,
            "source_url": source_url,
            "source_url_norm": source_url_norm,
            "confidence": float(row[2] or 0.0),
            "last_seen_ts": last_seen_ts,
            "verified_at_utc": str(row[4] or ""),
            "meta_json": str(row[5] or ""),
            "meta": self._parse_meta_json(row[5]),
        }

    def _extract_name_key_tokens(self, station_key: str) -> list[str]:
        key = str(station_key or "").strip().lower()
        if not key.startswith("name:"):
            return []
        value = key[len("name:") :].strip()
        if not value:
            return []
        return [token for token in value.split() if token]

    def _lookup_name_fallback(
        self,
        *,
        conn: sqlite3.Connection,
        station_key: str,
        min_name_tokens: int,
        max_name_candidates: int,
    ):
        tokens = self._extract_name_key_tokens(station_key)
        if len(tokens) < max(1, int(min_name_tokens or 1)):
            return None

        # Strict compatibility: key-prefix match in both directions.
        like_prefix = f"{station_key} %"
        rows = conn.execute(
            """
            SELECT source_url, source_url_norm, confidence, last_seen_ts, verified_at_utc, meta_json
            FROM verified_station_sources
            WHERE station_key LIKE ? OR ? LIKE station_key || ' %'
            ORDER BY confidence DESC, last_seen_ts DESC
            LIMIT ?
            """,
            (like_prefix, station_key, max(1, int(max_name_candidates or 1))),
        ).fetchall()
        if not rows:
            self._trace("verified_source_lookup_name_fallback_miss", station_key)
            return None
        self._trace("verified_source_lookup_name_fallback_hit", station_key)
        return rows[0]

    def _parse_meta_json(self, raw_value: str) -> dict:
        text = str(raw_value or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            self._trace("verified_source_meta_parse_failed", text[:120])
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

