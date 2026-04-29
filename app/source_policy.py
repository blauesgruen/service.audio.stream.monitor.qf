"""Shared source-policy helpers used by GUI and Kodi bridge."""

from __future__ import annotations

from typing import Callable

from .models import ResolvedStream, StationMatch
from .utils import get_base_domain, is_non_origin_directory_url, is_origin_url


def collect_origin_domains(
    station: StationMatch | None,
    resolved: ResolvedStream | None = None,
) -> set[str]:
    domains = set()

    if resolved:
        base = get_base_domain(resolved.resolved_url)
        if base:
            domains.add(base)

    if not station:
        return domains

    source_type = str(station.raw_record.get("source") or "").strip().lower()
    candidate_urls = [station.stream_url]
    if station.homepage and not is_non_origin_directory_url(station.homepage):
        candidate_urls.append(station.homepage)

    if source_type != "web_directory_fallback":
        for key in ("url", "url_resolved", "homepage", "stream_url"):
            value = station.raw_record.get(key)
            if not isinstance(value, str):
                continue
            if key == "homepage" and is_non_origin_directory_url(value):
                continue
            candidate_urls.append(value)

    for value in candidate_urls:
        base = get_base_domain(value)
        if base:
            domains.add(base)

    return domains


def classify_song_source(
    url: str,
    origin_domains: set[str],
    *,
    origin_only_mode: bool,
    allow_official_chain_sources: bool,
    trusted_candidate_check: Callable[[str], bool] | None = None,
) -> tuple[bool, str]:
    if not url:
        return False, ""
    if not origin_only_mode:
        return True, "unrestricted"
    if is_origin_url(url, origin_domains):
        return True, "origin"
    if (
        allow_official_chain_sources
        and trusted_candidate_check
        and trusted_candidate_check(url)
        and not is_non_origin_directory_url(url)
    ):
        return True, "official_player_chain"
    return False, "blocked_non_allowed"


def is_allowed_song_source(
    url: str,
    origin_domains: set[str],
    *,
    origin_only_mode: bool,
    allow_official_chain_sources: bool,
    trusted_candidate_check: Callable[[str], bool] | None = None,
) -> bool:
    allowed, _ = classify_song_source(
        url,
        origin_domains,
        origin_only_mode=origin_only_mode,
        allow_official_chain_sources=allow_official_chain_sources,
        trusted_candidate_check=trusted_candidate_check,
    )
    return allowed
