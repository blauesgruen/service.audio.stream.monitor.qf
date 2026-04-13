"""Read access for verified source hints stored by the Kodi bridge."""

from __future__ import annotations

import sqlite3
import time
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

    def get_preferred_source(self, station_key: str, max_age_seconds: int = 0) -> dict | None:
        key = str(station_key or "").strip()
        if not key or not self._db_path:
            return None

        now_ts = int(time.time())
        try:
            conn = sqlite3.connect(self._db_path, timeout=1.0)
            try:
                row = conn.execute(
                    """
                    SELECT source_url, source_url_norm, confidence, last_seen_ts, verified_at_utc, meta_json
                    FROM verified_station_sources
                    WHERE station_key = ?
                    ORDER BY last_seen_ts DESC, confidence DESC
                    LIMIT 1
                    """,
                    (key,),
                ).fetchone()
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
        }

