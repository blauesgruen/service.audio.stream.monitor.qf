"""Shared song probing and source selection used by GUI and Kodi bridge."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Tuple

from .models import ResolvedStream, SongInfo, StationMatch
from .source_policy import classify_song_source, is_allowed_song_source


PairValidator = Callable[[str, str], Tuple[str, str, str]]
PairPredicate = Callable[[str, str], bool]


@dataclass
class SongProbeConfig:
    origin_only_mode: bool
    allow_official_chain_sources: bool
    strict_webplayer_source: bool = False
    stale_without_stream_track_max_age_minutes: int = 0
    feed_retry_attempts: int = 1
    feed_retry_delay_seconds: float = 0.0
    quickpass_enabled: bool = False
    quickpass_max_candidates: int = 0
    quickpass_max_seconds: float = 0.0


@dataclass
class SongProbeResult:
    stream_song: SongInfo | None = None
    feed_song: SongInfo | None = None
    chosen_song: SongInfo | None = None
    stream_pair_state: str = "no_candidate"
    feed_pair_state: str = "no_candidate"
    stream_error: str = ""
    stream_song_is_valid: bool = False
    feed_song_is_valid: bool = False
    stream_song_is_allowed: bool = False
    feed_song_is_allowed: bool = False
    stream_song_approval: str = ""
    feed_song_approval: str = ""
    stream_title_missing_in_cycle: bool = False
    strict_webplayer_mode: bool = False
    rejected_non_origin_source: bool = False
    reported_stream_deferred: bool = False
    feed_candidates: list[str] = field(default_factory=list)
    official_html_feed_candidates: list[str] = field(default_factory=list)
    linked_domains: list[str] = field(default_factory=list)
    preferred_feed_url: str = ""


class SongProbeSession:
    def __init__(
        self,
        *,
        resolved: ResolvedStream,
        station: StationMatch | None,
        origin_domains: set[str],
        fetcher,
        discovery,
        config: SongProbeConfig,
        pair_is_valid: PairPredicate,
        pair_validator: PairValidator | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.resolved = resolved
        self.station = station
        self.origin_domains = set(origin_domains or [])
        self.fetcher = fetcher
        self.discovery = discovery
        self.config = config
        self.pair_is_valid = pair_is_valid
        self.pair_validator = pair_validator
        self._log = log or (lambda message: None)
        self.feed_candidates: list[str] = []
        self.official_html_feed_candidates: list[str] = []
        self.preferred_feed_url = ""
        self._reported_stream_deferred = False

    def _station_name(self) -> str:
        return self.station.name if self.station else ""

    def _validate_pair(self, song: SongInfo | None) -> tuple[bool, str]:
        if not song:
            return False, "no_candidate"
        if self.pair_validator:
            artist, title, state = self.pair_validator(song.artist, song.title)
            if state == "ok":
                song.artist = artist
                song.title = title
                return True, "ok"
            return False, state
        is_valid = self.pair_is_valid(song.artist, song.title)
        if is_valid:
            return True, "ok"
        if not song.artist or not song.title:
            return False, "missing_field"
        return False, "invalid"

    def _classify_source(self, url: str) -> tuple[bool, str]:
        return classify_song_source(
            url,
            self.origin_domains,
            origin_only_mode=self.config.origin_only_mode,
            allow_official_chain_sources=self.config.allow_official_chain_sources,
            trusted_candidate_check=self.discovery.is_trusted_candidate,
        )

    def _ensure_feed_candidates(self, stream_headers: dict[str, str]) -> tuple[list[str], list[str], list[str], bool]:
        rejected_non_origin_source = False
        if self.feed_candidates:
            linked_domains = sorted(self.discovery.get_linked_domains() - self.origin_domains)
            return (
                list(self.feed_candidates),
                list(self.official_html_feed_candidates),
                linked_domains,
                rejected_non_origin_source,
            )

        discovered = self.discovery.discover_candidate_urls(
            resolved=self.resolved,
            station=self.station,
            stream_headers=stream_headers,
        )
        filtered_candidates = []
        for url in discovered:
            if is_allowed_song_source(
                url,
                self.origin_domains,
                origin_only_mode=self.config.origin_only_mode,
                allow_official_chain_sources=self.config.allow_official_chain_sources,
                trusted_candidate_check=self.discovery.is_trusted_candidate,
            ):
                filtered_candidates.append(url)
            else:
                rejected_non_origin_source = True
                self._log(f"Feed-Kandidat verworfen (nicht erlaubt): {url}")

        self.feed_candidates = filtered_candidates
        self.official_html_feed_candidates = self.discovery.filter_official_html_candidates(
            self.feed_candidates,
            self.station,
        )
        linked_domains = sorted(self.discovery.get_linked_domains() - self.origin_domains)
        return (
            list(self.feed_candidates),
            list(self.official_html_feed_candidates),
            linked_domains,
            rejected_non_origin_source,
        )

    def _fetch_feed_song(self, probe_candidates: list[str]) -> tuple[SongInfo | None, str]:
        attempts = max(1, int(self.config.feed_retry_attempts or 1))
        delay = max(0.0, float(self.config.feed_retry_delay_seconds or 0.0))
        feed_song = None
        feed_pair_state = "no_candidate"

        if self.config.quickpass_enabled and probe_candidates:
            feed_song = self.discovery.fetch_now_playing(
                probe_candidates,
                station_name=self._station_name(),
                max_candidates=max(0, int(self.config.quickpass_max_candidates or 0)),
                max_elapsed_seconds=max(0.0, float(self.config.quickpass_max_seconds or 0.0)),
            )
            is_valid, feed_pair_state = self._validate_pair(feed_song)
            if is_valid:
                return feed_song, "ok"

        for attempt in range(1, attempts + 1):
            feed_song = self.discovery.fetch_now_playing(
                probe_candidates,
                station_name=self._station_name(),
            )
            is_valid, feed_pair_state = self._validate_pair(feed_song)
            if is_valid:
                return feed_song, "ok"
            if attempt < attempts and delay > 0.0:
                time.sleep(delay)

        return feed_song, feed_pair_state

    def probe_once(self) -> SongProbeResult:
        result = SongProbeResult(preferred_feed_url=self.preferred_feed_url)

        try:
            result.stream_song = self.fetcher.fetch(self.resolved.resolved_url)
        except Exception as err:
            result.stream_error = str(err)
            result.stream_title_missing_in_cycle = "kein StreamTitle gefunden" in result.stream_error

        if result.stream_song:
            result.stream_song_is_valid, result.stream_pair_state = self._validate_pair(result.stream_song)
            result.stream_song_is_allowed, result.stream_song_approval = self._classify_source(
                result.stream_song.source_url,
            )
            if result.stream_song_is_valid and not result.stream_song_is_allowed:
                result.rejected_non_origin_source = True
                self._log(
                    f"Stream-Metadaten verworfen (nicht erlaubt): {result.stream_song.source_url}"
                )

        (
            result.feed_candidates,
            result.official_html_feed_candidates,
            result.linked_domains,
            rejected_feed_candidates,
        ) = self._ensure_feed_candidates(result.stream_song.source_headers if result.stream_song else {})
        result.rejected_non_origin_source = result.rejected_non_origin_source or rejected_feed_candidates

        result.strict_webplayer_mode = bool(
            self.config.strict_webplayer_source and result.official_html_feed_candidates
        )

        if result.stream_song_is_valid and result.stream_song_is_allowed:
            if result.strict_webplayer_mode:
                if not self._reported_stream_deferred:
                    self._log("ICY-Treffer zurueckgestellt: offizieller HTML-Webplayer-Feed verfuegbar.")
                    self._reported_stream_deferred = True
                result.reported_stream_deferred = True
            else:
                result.chosen_song = result.stream_song
                result.chosen_song.source_approval = result.stream_song_approval
                self._reported_stream_deferred = False
        elif not result.strict_webplayer_mode:
            self._reported_stream_deferred = False

        if result.feed_candidates:
            probe_candidates = (
                self.discovery.prioritize_feed_candidates(result.feed_candidates, self.station)
                if result.strict_webplayer_mode
                else list(result.feed_candidates)
            )
            if self.preferred_feed_url and self.preferred_feed_url not in probe_candidates:
                self.preferred_feed_url = ""
            probe_list = [self.preferred_feed_url] if self.preferred_feed_url else probe_candidates
            result.feed_song, result.feed_pair_state = self._fetch_feed_song(probe_list)
            result.feed_song_is_valid = result.feed_pair_state == "ok"

            if (
                result.feed_song
                and result.feed_song_is_valid
                and result.strict_webplayer_mode
                and result.stream_song
                and not result.stream_song_is_valid
                and result.feed_song.age_minutes is not None
                and result.feed_song.age_minutes >= self.config.stale_without_stream_track_max_age_minutes
            ):
                self._log(
                    "Feed-Treffer verworfen (veraltet ohne ICY-Track-Signal): "
                    f"{result.feed_song.stream_title} ({result.feed_song.age_minutes} min alt)"
                )
                result.feed_song = None
                result.feed_song_is_valid = False
                result.feed_pair_state = "stale_without_stream_track"

            if result.feed_song and result.feed_song_is_valid:
                if result.stream_song and result.stream_song.source_headers:
                    result.feed_song.source_headers = result.stream_song.source_headers
                result.feed_song_is_allowed, result.feed_song_approval = self._classify_source(
                    result.feed_song.source_url,
                )
                if result.feed_song_is_allowed:
                    result.feed_song.source_approval = result.feed_song_approval
                    result.chosen_song = result.feed_song
                    self.preferred_feed_url = result.feed_song.source_url or self.preferred_feed_url
                    result.preferred_feed_url = self.preferred_feed_url
                else:
                    result.rejected_non_origin_source = True
                    self._log(f"Feed-Treffer verworfen (nicht erlaubt): {result.feed_song.source_url}")

        if (
            result.strict_webplayer_mode
            and not result.chosen_song
            and result.stream_song_is_valid
            and result.stream_song_is_allowed
        ):
            self._log("Kein gueltiger HTML-Feed-Treffer; falle auf ICY-Treffer zurueck.")
            result.chosen_song = result.stream_song
            result.chosen_song.source_approval = result.stream_song_approval
            result.reported_stream_deferred = False
            self._reported_stream_deferred = False

        return result
