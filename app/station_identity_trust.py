"""Generic stream-variant grouping and identity trust tiers (pure logic, no Kodi/network).

Tiers (see plan "Sichere Senderidentifikation"):
  A - confirmed/unambiguous: exactly one plausible program.
  B - probable: a best candidate exists, but other distinct programs are plausible.
  C - undetermined: no safe decision possible.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse

from .utils import get_base_domain

TIER_CONFIRMED = "A"
TIER_PROBABLE = "B"
TIER_UNDETERMINED = "C"

# Technical (format/transport) tokens only. Deliberately NOT included: "live",
# "main", "hq" etc. may be real channel identifiers (bob-live vs. bob-national).
_FORMAT_TOKENS = frozenset(
    {
        "mp3", "mp2", "aac", "aacp", "aacplus", "heaac", "ogg", "oga", "opus", "flac",
        "m4a", "mp4", "hls", "stream", "streams", "icecast", "shoutcast", "listen",
        "audio", "play", "player", "webradio", "playlist", "index", "master", "chunklist",
        "kbps", "kbit", "k",
    }
)
_STREAM_EXTENSIONS = (
    ".mp3", ".aac", ".ogg", ".opus", ".flac", ".m4a", ".mp4",
    ".m3u", ".m3u8", ".pls", ".xspf", ".asx", ".php", ".html", ".htm",
)
# Only well-known audio bitrates; numbers like "40" in "top-40" are channel identity.
_BITRATE_RE = re.compile(r"^(?:24|32|48|56|64|80|96|112|128|160|192|224|256|320)k?$|^\d{2,3}k$")


def is_format_token(token: str) -> bool:
    """Format/bitrate words (mp3, aac, 128k, hd...) carry no program identity."""
    t = str(token or "").lower()
    return t in _FORMAT_TOKENS or t in {"hd", "hq", "lq"} or bool(_BITRATE_RE.match(t))


def _strip_extension(segment: str) -> str:
    lowered = segment.lower()
    for ext in _STREAM_EXTENSIONS:
        if lowered.endswith(ext):
            return segment[: -len(ext)]
    return segment


# Server/CDN-style path segments such as "sw282p3" or "sw331ch" (letters-digits-letters or
# several digit groups). Plain "swr3", "wdr2", "1live", "top40" stay channel identity.
_SERVER_SEGMENT_RE = re.compile(r"^(?:[a-z]+\d+[a-z]+\d*|[a-z]+\d+[a-z]+\d+|[a-z]*\d+[a-z]+\d+[a-z0-9]*)$")


def _segment_channel_tokens(segment: str) -> list[str]:
    if _SERVER_SEGMENT_RE.match(_strip_extension(unquote(segment)).lower()):
        return []
    tokens = []
    for token in re.split(r"[-_.+\s]+", _strip_extension(unquote(segment)).lower()):
        if not token or token in _FORMAT_TOKENS or _BITRATE_RE.match(token):
            continue
        tokens.append(token)
    return tokens


def _is_format_segment(segment: str) -> bool:
    """True for pure format/bitrate segments such as "mp3-192", "aac-64", "stream.mp3"."""
    raw = _strip_extension(unquote(segment)).lower()
    parts = [t for t in re.split(r"[-_.+\s]+", raw) if t]
    if not parts:
        return False
    if _segment_channel_tokens(segment):
        return False
    # "webradio" is noise (dropped from the key) but must not end the channel part of the path.
    return any((t in _FORMAT_TOKENS and t != "webradio") or _BITRATE_RE.match(t) for t in parts)


def stream_channel_key(url: str) -> str:
    """Return the channel identity encoded in a stream URL path, or "" if none.

    The channel is the path before the first pure format segment (mp3-192, aac-64, ...);
    everything after it is treated as player/tracking parameters (e.g. /homepage/,
    /mediaplayer/). Without a format segment all non-format segments count. Query
    strings are ignored.
    """
    path = urlparse(str(url or "").strip()).path or ""
    parts: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        if _is_format_segment(segment):
            break
        tokens = _segment_channel_tokens(segment)
        if tokens:
            parts.append("-".join(tokens))
    return "/".join(parts)


def _host_path(url: str) -> tuple[str, str]:
    parsed = urlparse(str(url or "").strip())
    return (parsed.hostname or "").lower(), (parsed.path or "").rstrip("/")


def same_program_stream(url_a: str, url_b: str) -> bool:
    """True only with sufficient technical evidence that two streams are one program."""
    base_a = get_base_domain(url_a)
    base_b = get_base_domain(url_b)
    if not base_a or base_a != base_b:
        return False
    key_a = stream_channel_key(url_a)
    key_b = stream_channel_key(url_b)
    if key_a or key_b:
        return key_a == key_b
    # No channel identity in either path: only identical host+path counts.
    return _host_path(url_a) == _host_path(url_b)


def group_stream_variants(stations: Iterable) -> list[list]:
    """Group stations into programs. Unclear cases stay separate (safety rule)."""
    groups: list[list] = []
    for station in stations:
        for group in groups:
            if same_program_stream(group[0].stream_url, station.stream_url):
                group.append(station)
                break
        else:
            groups.append([station])
    return groups


def pick_group_representative(group: list):
    """Among streams already known to be the same program, health/votes/bitrate may decide."""
    return max(
        group,
        key=lambda s: (int(s.lastcheckok or 0), int(s.votes or 0), int(s.bitrate or 0)),
    )


def redact_url(url: str) -> str:
    """Log-safe URL: scheme://host/path, no userinfo, query, fragment."""
    parsed = urlparse(str(url or "").strip())
    if not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}"


def candidate_summary(station, group_size: int = 1) -> dict:
    return {
        "stationuuid": station.stationuuid,
        "name": station.name,
        "homepage": station.homepage,
        "country": station.country,
        "stream_url": station.stream_url,
        "channel_key": stream_channel_key(station.stream_url),
        "variants": group_size,
        "bitrate": station.bitrate,
        "votes": station.votes,
        "lastcheckok": station.lastcheckok,
    }


def assess_identity(
    selected,
    candidates: Iterable,
    is_plausible: Callable[[object], bool],
    *,
    max_candidates: int = 8,
) -> dict:
    """Rate the identity of `selected` against all plausible competing programs.

    Returns {"tier", "reasons", "candidates"}. `candidates` lists one representative
    per distinct plausible program (selected program first). Never trusts votes,
    bitrate or health to choose between *different* programs.
    """
    pool = [selected, *[c for c in candidates if c is not selected]]
    plausible = [c for c in pool if c is selected or is_plausible(c)]
    groups = group_stream_variants(plausible)
    selected_group = next((g for g in groups if any(m is selected for m in g)), [selected])
    ordered = [selected_group] + [g for g in groups if g is not selected_group]
    summaries = [
        candidate_summary(selected if g is selected_group else pick_group_representative(g), len(g))
        for g in ordered[:max_candidates]
    ]
    if len(groups) <= 1:
        return {"tier": TIER_CONFIRMED, "reasons": ["single_plausible_program"], "candidates": summaries}
    return {
        "tier": TIER_PROBABLE,
        "reasons": [f"other_plausible_programs:{len(groups) - 1}"],
        "candidates": summaries,
    }
