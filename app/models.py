"""Domain models shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any



@dataclass
class ResolvedStream:
    input_url: str
    resolved_url: str
    delivery_url: str
    content_type: str
    was_playlist: bool
    station_name: str = ""


@dataclass
class SongInfo:
    stream_title: str
    raw_metadata: str
    artist: str = ""
    title: str = ""
    age_minutes: int | None = None
    source_kind: str = "stream_icy"
    source_url: str = ""
    source_approval: str = ""
    source_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class StationMatch:
    stationuuid: str
    name: str
    stream_url: str
    homepage: str
    country: str
    language: str
    codec: str
    bitrate: int
    votes: int
    lastcheckok: int
    raw_record: dict[str, Any] = field(default_factory=dict)


@dataclass
class EpgInfo:
    available: bool
    source_url: str
    summary: str
    raw_xml: str = ""
    error: str = ""
