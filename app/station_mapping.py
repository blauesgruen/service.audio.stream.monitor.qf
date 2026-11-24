"""Persistent source->station mapping with trust metadata (pure sqlite, no Kodi).

Replaces the blind name->uuid map. Legacy rows from `station_uuid_map` are copied once
as `legacy_unverified` and never treated as confirmed. The old table is kept.
"""

from __future__ import annotations

import os
import sqlite3
import time

CONFIRM_USER = "user"
CONFIRM_TECHNICAL = "technical"
CONFIRM_LEGACY = "legacy_unverified"

STATUS_CONFIRMED = "confirmed"
STATUS_UNVERIFIED = "unverified"

TRUSTED_SOURCES = frozenset({CONFIRM_USER, CONFIRM_TECHNICAL})

_SCHEMA_VERSION = 1

_COLUMNS = (
    "source_type",
    "source_identifier",
    "station_name",
    "stationuuid",
    "identity_status",
    "confirmation_source",
    "save_preference",
    "created_ts",
    "updated_ts",
)


def _connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=2.0)
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS station_identity_map ("
        "source_type TEXT NOT NULL, "
        "source_identifier TEXT NOT NULL, "
        "station_name TEXT NOT NULL DEFAULT '', "
        "stationuuid TEXT NOT NULL, "
        "identity_status TEXT NOT NULL DEFAULT 'unverified', "
        "confirmation_source TEXT NOT NULL DEFAULT 'legacy_unverified', "
        "save_preference INTEGER NOT NULL DEFAULT 1, "
        "created_ts INTEGER NOT NULL DEFAULT 0, "
        "updated_ts INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (source_type, source_identifier))"
    )
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
        return  # already migrated: no write on regular requests
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='station_uuid_map'"
    ).fetchone()
    if legacy:
        # One-time copy; rows deleted later by the user must not come back.
        conn.execute(
            "INSERT OR IGNORE INTO station_identity_map "
            "(source_type, source_identifier, station_name, stationuuid, identity_status, "
            " confirmation_source, save_preference, created_ts, updated_ts) "
            "SELECT 'name', station_key, '', stationuuid, 'unverified', 'legacy_unverified', 1, "
            "updated_ts, updated_ts FROM station_uuid_map"
        )
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


def _row_to_dict(row) -> dict:
    return dict(zip(_COLUMNS, row))


def load_mapping(db_path: str, source_type: str, source_identifier: str) -> dict | None:
    if not source_type or not source_identifier:
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM station_identity_map "
            "WHERE source_type=? AND source_identifier=?",
            (source_type, source_identifier),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def is_trusted(mapping: dict | None) -> bool:
    return bool(mapping) and mapping.get("confirmation_source") in TRUSTED_SOURCES


def save_mapping(
    db_path: str,
    source_type: str,
    source_identifier: str,
    stationuuid: str,
    *,
    station_name: str = "",
    confirmation_source: str = CONFIRM_USER,
    save_preference: bool = True,
) -> bool:
    """Persist a mapping. Only user/technical confirmations may be stored."""
    if confirmation_source not in TRUSTED_SOURCES:
        raise ValueError("only user/technical confirmations may be persisted")
    uuid = str(stationuuid or "").strip()
    if not source_type or not source_identifier or not uuid:
        return False
    now = int(time.time())
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO station_identity_map "
            "(source_type, source_identifier, station_name, stationuuid, identity_status, "
            " confirmation_source, save_preference, created_ts, updated_ts) "
            "VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?, ?) "
            "ON CONFLICT(source_type, source_identifier) DO UPDATE SET "
            "station_name=excluded.station_name, stationuuid=excluded.stationuuid, "
            "identity_status='confirmed', confirmation_source=excluded.confirmation_source, "
            "save_preference=excluded.save_preference, updated_ts=excluded.updated_ts",
            (source_type, source_identifier, station_name, uuid, confirmation_source,
             int(bool(save_preference)), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def delete_mapping(db_path: str, source_type: str, source_identifier: str) -> bool:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM station_identity_map WHERE source_type=? AND source_identifier=?",
            (source_type, source_identifier),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_mappings(db_path: str) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM station_identity_map "
            "ORDER BY updated_ts DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]
