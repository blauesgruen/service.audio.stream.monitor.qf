"""Generic discovery and parsing of web now-playing feeds (XML/JSON)."""

from __future__ import annotations

import html
import json
import re
import ssl
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .config import (
    DISCOVERY_CRAWL_MAX_WORKERS,
    DISCOVERY_CTRL_API_FUTURE_GRACE_SECONDS,
    DISCOVERY_CTRL_API_START_DELAY_SECONDS,
    DISCOVERY_OFFICIAL_PLAYER_ENTRY_LIMIT,
    DISCOVERY_OFFICIAL_PLAYER_FOLLOWUP_BUDGET,
    DISCOVERY_MAX_CANDIDATES,
    DISCOVERY_PAGE_FETCH_BUDGET,
    DISCOVERY_PLAYERBAR_MAX_CONTAINERS,
    DISCOVERY_PLAYERBAR_MAX_WORKERS,
    DISCOVERY_READ_BYTES,
    DISCOVERY_REQUEST_TIMEOUT_SECONDS,
    DISCOVERY_SCRIPT_FETCH_BUDGET,
    MAX_NOWPLAYING_AGE_MINUTES,
    NOWPLAYING_PARALLEL_BATCH_SIZE,
    NOWPLAYING_PARALLEL_MAX_WORKERS,
    NOWPLAYING_PARALLEL_PROBING_ENABLED,
    NOWPLAYING_DURATION_GRACE_SECONDS,
    NOWPLAYING_CANDIDATE_KEYWORDS,
    NOWPLAYING_HTML_EDITORIAL_TOKENS,
    NOWPLAYING_QUERY_CONTEXT_IGNORE_TOKENS,
    PROVIDER_BCS_BASE_DOMAINS,
    PROVIDER_BCS_GENERIC_NAME_TOKENS,
    PROVIDER_BCS_IFRAME_HOST,
    PROVIDER_BCS_WEBRADIO_HOST,
    PROVIDER_BR_BASE_DOMAINS,
    PROVIDER_NDR_BASE_DOMAINS,
    STATION_LOOKUP_OPTIONAL_PREFIX_TOKENS,
    USER_AGENT,
)
from .models import ResolvedStream, SongInfo, StationMatch
from .song_validation import build_station_hints, compact_station_compare_text, is_valid_song_candidate
from .utils import (
    decode_text_bytes,
    get_base_domain,
    is_mixed_alnum_token,
    is_probable_url,
    repair_mojibake_text,
    split_search_tokens,
)

TITLE_KEYS = {
    "title",
    "song",
    "track",
    "tracktitle",
    "songtitle",
    "songname",
    "trackname",
    "name",
    "song_title",
    "song_now_title",
}
ARTIST_KEYS = {
    "artist",
    "artistcredits",
    "artist_credit",
    "author",
    "interpret",
    "performer",
    "band",
    "artistname",
    "song_interpret",
    "song_now_interpret",
}
STATUS_KEYS = {"status", "state", "playstate", "onair", "current", "isplaying"}
TIME_KEYS = {
    "starttime",
    "start",
    "timestamp",
    "ts",
    "time",
    "date",
    "datetime",
    "lastupdated",
    "updated",
    "updatedat",
    "updated_at",
}
DURATION_KEYS = {"duration", "length", "duration_sec", "duration_seconds", "runtime"}
HTML_TITLE_CLASS_KEYS = ("js_title", "songtitle", "tracktitle", "title", "titel", "track", "song", "songname", "trackname")
HTML_ARTIST_CLASS_KEYS = ("js_artist", "interpret", "artist", "artistname", "performer", "band", "author")
JSONP_WRAPPER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.]*\((?P<payload>.*)\)\s*;?\s*$", flags=re.DOTALL)
GRAPHQL_ENDPOINT_RE = re.compile(r"https?://[^\"'`\s<>()]+/graphql\b", flags=re.IGNORECASE)
GRAPHQL_STREAMS_QUERY = """
query StreamQuery {
  taxonomyTermList(bundles: ["streams"]) {
    items {
      ... on TaxonomyTermStreams {
        id
        label
        fieldLink {
          url {
            path
          }
        }
      }
    }
  }
}
""".strip()
GRAPHQL_TRACKS_QUERY = """
query TracksQuery($id: ID!) {
  streamById(id: $id) {
    name
    streamValue {
      date
      track {
        artist
        duration
        start_time
        title
      }
    }
  }
}
""".strip()
GRAPHQL_TRACKS_MODE_PARAM = "_qf_np"
GRAPHQL_TRACKS_MODE_VALUE = "graphql_stream_tracks"
GRAPHQL_TRACKS_ID_PARAM = "stream_id"
BCS_CURRENT_MODE_PARAM = "_qf_bcs"
BCS_CURRENT_MODE_VALUE = "current_station"
BCS_CURRENT_STATION_PARAM = "station"
RADIOPLAYER_EVENT_TITLE_KEYS = {"name", "title", "song", "track"}
RADIOPLAYER_EVENT_ARTIST_KEYS = {"artistname", "artist_name", "artist"}


@dataclass
class OfficialPlayerEntry:
    score: int
    feed_urls: list[str] = field(default_factory=list)
    follow_urls: list[str] = field(default_factory=list)


class NowPlayingDiscoveryService:
    def __init__(self, log) -> None:
        self._log = log
        self._trusted_candidates: set[str] = set()
        self._linked_domains: set[str] = set()
        self._crawl_max_workers = max(1, int(DISCOVERY_CRAWL_MAX_WORKERS))
        self._playerbar_max_containers = max(1, int(DISCOVERY_PLAYERBAR_MAX_CONTAINERS))
        self._playerbar_max_workers = max(1, int(DISCOVERY_PLAYERBAR_MAX_WORKERS))
        self._parallel_prob_enabled = bool(NOWPLAYING_PARALLEL_PROBING_ENABLED)
        self._parallel_max_workers = max(1, int(NOWPLAYING_PARALLEL_MAX_WORKERS))
        self._parallel_batch_size = max(1, int(NOWPLAYING_PARALLEL_BATCH_SIZE))
        self._graphql_stream_catalog_cache: dict[str, list[dict[str, str]]] = {}

    def is_trusted_candidate(self, url: str) -> bool:
        return url in self._trusted_candidates

    def get_linked_domains(self) -> set[str]:
        return set(self._linked_domains)

    def filter_official_html_candidates(
        self,
        candidate_urls: list[str],
        station: StationMatch | None,
    ) -> list[str]:
        station_base = get_base_domain(station.homepage) if station and station.homepage else ""
        station_domain_matches = []
        generic_html_matches = []
        seen = set()
        for url in candidate_urls:
            if not self._looks_like_html_nowplaying_endpoint(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            candidate_base = get_base_domain(url)
            generic_html_matches.append(url)
            if station_base and candidate_base == station_base:
                station_domain_matches.append(url)

        if station_domain_matches:
            merged = list(station_domain_matches)
            for url in generic_html_matches:
                if url in station_domain_matches:
                    continue
                merged.append(url)
            return merged
        return generic_html_matches

    def prioritize_feed_candidates(
        self,
        candidate_urls: list[str],
        station: StationMatch | None,
    ) -> list[str]:
        ordered = []
        seen = set()

        def _append(url: str):
            if not url or url in seen:
                return
            seen.add(url)
            ordered.append(url)

        html_priority = self.filter_official_html_candidates(candidate_urls, station)
        for url in html_priority:
            _append(url)

        for url in list(candidate_urls or []):
            if url in seen:
                continue
            if self._is_strong_nowplaying_feed_url(url):
                _append(url)

        for url in list(candidate_urls or []):
            _append(url)

        return ordered

    def discover_candidate_urls(
        self,
        resolved: ResolvedStream,
        station: StationMatch | None,
        stream_headers: dict[str, str],
    ) -> list[str]:
        total_started = time.perf_counter()
        self._trusted_candidates = set()
        self._linked_domains = set()
        phase_timings = {
            "seed_fetch": 0.0,
            "discovery_pages": 0.0,
            "avcustom": 0.0,
            "scripts": 0.0,
            "nested_scripts": 0.0,
            "generated": 0.0,
            "official_player": 0.0,
            "playerbar": 0.0,
            "graphql": 0.0,
            "bcs": 0.0,
            "loverad": 0.0,
            "ranking": 0.0,
        }

        seeds = self._build_seed_urls(resolved, station, stream_headers)
        candidates = set()
        visited_pages = set()
        discovery_page_budget = max(0, int(DISCOVERY_PAGE_FETCH_BUDGET))
        script_fetch_budget = max(0, int(DISCOVERY_SCRIPT_FETCH_BUDGET))
        seed_documents: list[tuple[str, str]] = []
        discovery_page_urls: list[str] = []
        avcustom_urls: list[str] = []
        script_urls: list[str] = []

        def _remember_candidate(url: str) -> None:
            if self._looks_like_feed_url(url):
                candidates.add(url)
                self._mark_trusted_candidate(url)

        for seed in seeds:
            visited_pages.add(seed)
            _remember_candidate(seed)

        seed_fetch_urls = [seed for seed in seeds if not self._looks_like_stream_endpoint(seed)]
        phase_started = time.perf_counter()
        seed_documents_batch = self._fetch_documents_parallel(seed_fetch_urls)
        phase_timings["seed_fetch"] = time.perf_counter() - phase_started
        for seed, text in seed_documents_batch:
            seed_documents.append((seed, text))
            extracted_urls = self._extract_urls_from_document(text, seed)
            for extracted in extracted_urls:
                _remember_candidate(extracted)
                if (
                    self._looks_like_discovery_page(extracted)
                    and get_base_domain(extracted) == get_base_domain(seed)
                ):
                    discovery_page_urls.append(extracted)
                if "avcustom" in extracted.lower() and get_base_domain(extracted) == get_base_domain(seed):
                    avcustom_urls.append(extracted)
            script_urls.extend(self._prioritize_script_asset_urls(extracted_urls, seed))

        discovery_fetch_urls, discovery_page_budget = self._take_budgeted_urls(
            discovery_page_urls,
            visited_pages,
            discovery_page_budget,
        )
        phase_started = time.perf_counter()
        discovery_documents = self._fetch_documents_parallel(discovery_fetch_urls)
        phase_timings["discovery_pages"] = time.perf_counter() - phase_started
        for page_url, page_text in discovery_documents:
            seed_documents.append((page_url, page_text))
            for nested in self._extract_urls_from_document(page_text, page_url):
                _remember_candidate(nested)

        avcustom_fetch_urls, _ = self._take_budgeted_urls(
            avcustom_urls,
            visited_pages,
            max(0, len(avcustom_urls)),
        )
        phase_started = time.perf_counter()
        avcustom_documents = self._fetch_documents_parallel(avcustom_fetch_urls)
        phase_timings["avcustom"] = time.perf_counter() - phase_started
        for doc_url, doc_text in avcustom_documents:
            seed_documents.append((doc_url, doc_text))
            for nested in self._extract_urls_from_document(doc_text, doc_url):
                _remember_candidate(nested)

        script_fetch_urls, script_fetch_budget = self._take_budgeted_urls(
            script_urls,
            visited_pages,
            script_fetch_budget,
        )
        nested_script_urls: list[str] = []
        phase_started = time.perf_counter()
        script_documents = self._fetch_documents_parallel(script_fetch_urls)
        phase_timings["scripts"] = time.perf_counter() - phase_started
        for script_url, script_text in script_documents:
            seed_documents.append((script_url, script_text))
            nested_urls = self._extract_urls_from_document(script_text, script_url)
            for nested in nested_urls:
                _remember_candidate(nested)
            nested_script_urls.extend(self._prioritize_script_asset_urls(nested_urls, script_url))

        nested_script_fetch_urls, script_fetch_budget = self._take_budgeted_urls(
            nested_script_urls,
            visited_pages,
            script_fetch_budget,
        )
        phase_started = time.perf_counter()
        nested_script_documents = self._fetch_documents_parallel(nested_script_fetch_urls)
        phase_timings["nested_scripts"] = time.perf_counter() - phase_started
        for nested_script_url, nested_script_text in nested_script_documents:
            seed_documents.append((nested_script_url, nested_script_text))
            for extracted_nested_feed in self._extract_urls_from_document(nested_script_text, nested_script_url):
                _remember_candidate(extracted_nested_feed)

        phase_started = time.perf_counter()
        document_index = self._build_document_index(seed_documents)
        generated = self._build_generated_candidates(document_index, candidates, resolved, station)
        phase_timings["generated"] = time.perf_counter() - phase_started
        for generated_url in generated:
            candidates.add(generated_url)
            self._mark_trusted_candidate(generated_url)

        phase_started = time.perf_counter()
        official_player_feeds = self._discover_official_player_feed_urls(document_index, resolved, station)
        phase_timings["official_player"] = time.perf_counter() - phase_started
        for feed_url in official_player_feeds:
            candidates.add(feed_url)
            self._mark_trusted_candidate(feed_url)

        phase_started = time.perf_counter()
        playerbar_playlist_feeds = self._discover_playerbar_playlist_urls(document_index, resolved, station)
        phase_timings["playerbar"] = time.perf_counter() - phase_started
        for feed_url in playerbar_playlist_feeds:
            candidates.add(feed_url)
            self._mark_trusted_candidate(feed_url)

        phase_started = time.perf_counter()
        graphql_track_feeds = self._discover_graphql_track_feed_urls(document_index, resolved, station)
        phase_timings["graphql"] = time.perf_counter() - phase_started
        for feed_url in graphql_track_feeds:
            candidates.add(feed_url)
            self._mark_trusted_candidate(feed_url)

        phase_started = time.perf_counter()
        bcs_station_feeds = self._discover_bcs_station_feed_urls(document_index, resolved, station)
        phase_timings["bcs"] = time.perf_counter() - phase_started
        for feed_url in bcs_station_feeds:
            candidates.add(feed_url)
            self._mark_trusted_candidate(feed_url)

        derived_html_candidates = set()
        for candidate in list(candidates):
            for derived in self._generate_html_nowplaying_variants(candidate):
                if self._looks_like_feed_url(derived):
                    derived_html_candidates.add(derived)
        for derived in derived_html_candidates:
            candidates.add(derived)
            self._mark_trusted_candidate(derived)

        phase_started = time.perf_counter()
        loverad_flow_urls = self._discover_loverad_flow_urls(candidates, resolved, station)
        phase_timings["loverad"] = time.perf_counter() - phase_started
        for flow_url in loverad_flow_urls:
            candidates.add(flow_url)
            self._mark_trusted_candidate(flow_url)

        phase_started = time.perf_counter()
        normalized_candidates = self._dedupe_url_variants(candidates)
        normalized_candidates = self._prefer_ctrl_api_timestamped_candidates(normalized_candidates)
        context_filtered_candidates = {
            url
            for url in normalized_candidates
            if self._candidate_matches_input_context(url, resolved, station)
        }
        if context_filtered_candidates:
            normalized_candidates = context_filtered_candidates
            normalized_candidates = self._prefer_ctrl_api_timestamped_candidates(normalized_candidates)
        ranked = sorted(
            normalized_candidates,
            key=lambda url: self._candidate_score(url) + self._candidate_domain_preference(url, resolved, station),
            reverse=True,
        )
        limited = ranked[:DISCOVERY_MAX_CANDIDATES]
        self._trusted_candidates = {url for url in self._trusted_candidates if url in limited}
        self._linked_domains = {
            base
            for base in (get_base_domain(url) for url in self._trusted_candidates)
            if base
        }
        phase_timings["ranking"] = time.perf_counter() - phase_started
        if limited:
            self._log(f"Now-Playing Kandidaten gefunden: {len(limited)}")
        else:
            self._log("Keine Now-Playing Kandidaten gefunden")
        total_elapsed = time.perf_counter() - total_started
        self._log(
            "Discovery-Timing: "
            f"seeds={phase_timings['seed_fetch']:.2f}s "
            f"pages={phase_timings['discovery_pages']:.2f}s "
            f"avcustom={phase_timings['avcustom']:.2f}s "
            f"scripts={phase_timings['scripts']:.2f}s "
            f"nested_scripts={phase_timings['nested_scripts']:.2f}s "
            f"generated={phase_timings['generated']:.2f}s "
            f"official_player={phase_timings['official_player']:.2f}s "
            f"playerbar={phase_timings['playerbar']:.2f}s "
            f"graphql={phase_timings['graphql']:.2f}s "
            f"bcs={phase_timings['bcs']:.2f}s "
            f"loverad={phase_timings['loverad']:.2f}s "
            f"ranking={phase_timings['ranking']:.2f}s "
            f"total={total_elapsed:.2f}s "
            f"candidates={len(limited)}"
        )
        return limited

    def fetch_now_playing(
        self,
        candidate_urls: list[str],
        station_name: str = "",
        max_candidates: int = 0,
        max_elapsed_seconds: float = 0.0,
    ) -> SongInfo | None:
        candidate_list = list(candidate_urls or [])
        limit = DISCOVERY_MAX_CANDIDATES
        if int(max_candidates or 0) > 0:
            limit = min(limit, int(max_candidates))
        candidate_list = candidate_list[:limit]
        if not candidate_list:
            return None

        started_at = time.time()
        if not self._parallel_prob_enabled or len(candidate_list) <= 1:
            return self._fetch_now_playing_serial(candidate_list, station_name, started_at, max_elapsed_seconds)
        return self._fetch_now_playing_parallel(candidate_list, station_name, started_at, max_elapsed_seconds)

    def _probe_feed_candidate(self, url: str) -> SongInfo | None:
        if self._is_graphql_tracks_candidate(url):
            return self._probe_graphql_tracks_candidate(url)
        if self._is_bcs_current_candidate(url):
            return self._probe_bcs_current_candidate(url)
        request_url = url if self._looks_like_html_nowplaying_endpoint(url) else self._cache_bust_url(url)
        text, content_type = self._fetch_text(request_url)
        if not text:
            return None

        song = None
        if self._is_json_candidate(url, content_type, text):
            song = self._parse_json_payload(text, url)
        if not song:
            song = self._parse_xml_payload(text, url)
        if not song and self._looks_like_html_nowplaying_endpoint(url):
            song = self._parse_html_payload(text, url)
        return song

    def _is_elapsed_limit_reached(self, started_at: float, max_elapsed_seconds: float) -> bool:
        return bool(max_elapsed_seconds and (time.time() - started_at) > float(max_elapsed_seconds))

    def _fetch_now_playing_serial(
        self,
        candidate_urls: list[str],
        station_name: str,
        started_at: float,
        max_elapsed_seconds: float,
    ) -> SongInfo | None:
        partial_match: SongInfo | None = None
        for url in candidate_urls:
            if self._is_elapsed_limit_reached(started_at, max_elapsed_seconds):
                break
            song = self._probe_feed_candidate(url)
            if not song:
                continue
            if is_valid_song_candidate(song.artist, song.title, station_name=station_name):
                self._log(f"Now-Playing Treffer aus Feed: {url}")
                return song
            if not partial_match and (song.artist or song.title or song.stream_title):
                partial_match = song

        if partial_match:
            self._log(f"Now-Playing Fallback ohne vollstaendigen Artist+Title: {partial_match.source_url}")
        return partial_match

    def _fetch_now_playing_parallel(
        self,
        candidate_urls: list[str],
        station_name: str,
        started_at: float,
        max_elapsed_seconds: float,
    ) -> SongInfo | None:
        partial_match: SongInfo | None = None
        batch_size = max(1, min(self._parallel_batch_size, self._parallel_max_workers, len(candidate_urls)))

        for batch_start in range(0, len(candidate_urls), batch_size):
            if self._is_elapsed_limit_reached(started_at, max_elapsed_seconds):
                break

            batch = candidate_urls[batch_start : batch_start + batch_size]
            worker_count = max(1, min(self._parallel_max_workers, len(batch)))
            batch_results: dict[int, SongInfo | None] = {}
            next_idx = 0
            pool = ThreadPoolExecutor(max_workers=worker_count)
            shutdown_without_wait = False
            try:
                future_map = {
                    pool.submit(self._probe_feed_candidate, url): idx for idx, url in enumerate(batch)
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        batch_results[idx] = future.result()
                    except Exception:
                        batch_results[idx] = None

                    while next_idx in batch_results:
                        url = batch[next_idx]
                        song = batch_results.pop(next_idx)
                        next_idx += 1
                        if not song:
                            continue
                        if is_valid_song_candidate(song.artist, song.title, station_name=station_name):
                            self._log(f"Now-Playing Treffer aus Feed: {url}")
                            shutdown_without_wait = True
                            for pending in future_map:
                                if not pending.done():
                                    pending.cancel()
                            pool.shutdown(wait=False)
                            return song
                        if not partial_match and (song.artist or song.title or song.stream_title):
                            partial_match = song
            finally:
                if not shutdown_without_wait:
                    pool.shutdown(wait=True)

        if partial_match:
            self._log(f"Now-Playing Fallback ohne vollstaendigen Artist+Title: {partial_match.source_url}")
        return partial_match

    def _build_seed_urls(
        self,
        resolved: ResolvedStream,
        station: StationMatch | None,
        stream_headers: dict[str, str],
    ) -> list[str]:
        seeds = []

        if station and station.homepage:
            seeds.append(self._normalize_seed(station.homepage))
            radio_directory_slug = self._extract_radio_directory_slug(station.homepage)
            if radio_directory_slug:
                seeds.append(f"https://api.radio.de/stations/now-playing?stationIds={radio_directory_slug}")

        icy_url = (stream_headers.get("icy-url") or "").strip()
        if icy_url:
            normalized_icy = self._normalize_seed(icy_url)
            if normalized_icy:
                parsed_icy = urlparse(normalized_icy)
                icy_has_path = parsed_icy.path not in {"", "/"}
                icy_has_hint = any(keyword in normalized_icy.lower() for keyword in NOWPLAYING_CANDIDATE_KEYWORDS)
                if icy_has_path or icy_has_hint:
                    seeds.append(normalized_icy)

        if is_probable_url(resolved.input_url):
            seeds.append(self._normalize_seed(resolved.input_url))

        seeds.append(self._normalize_seed(resolved.resolved_url))
        parsed = urlparse(resolved.resolved_url)
        if parsed.scheme and parsed.netloc:
            stream_root = f"{parsed.scheme}://{parsed.netloc}/"
            seeds.append(stream_root)
            # Common Icecast/Shoutcast status endpoints that often expose current track info.
            seeds.append(urljoin(stream_root, "status-json.xsl"))
            seeds.append(urljoin(stream_root, "status.xsl"))
            seeds.append(urljoin(stream_root, "stats"))

        # Fallback for stations that expose metadata on the root/base domain.
        base_domains = set()
        if station and station.homepage:
            base = get_base_domain(station.homepage)
            if base:
                base_domains.add(base)
        for url_value in (resolved.input_url, resolved.resolved_url, resolved.delivery_url):
            base = get_base_domain(url_value)
            if base:
                base_domains.add(base)
        for base in sorted(base_domains):
            seeds.append(f"https://www.{base}/")
            seeds.append(f"https://{base}/")
            seeds.append(f"https://www.{base}/streams.json")
            seeds.append(f"https://{base}/streams.json")

        if self._is_br_context(base_domains):
            for br_slug in self._build_br_station_slugs(station, resolved):
                seeds.append(self._build_br_radio_graphql_url(br_slug))

        if self._is_ndr_context(base_domains):
            for ndr_slug in self._build_ndr_station_slugs(station, resolved):
                seeds.append(f"https://www.ndr.de/public/radioplaylists/{ndr_slug}.json")
                seeds.append(f"https://ndr.de/public/radioplaylists/{ndr_slug}.json")

        deduped = []
        seen = set()
        for seed in seeds:
            if not seed:
                continue
            if seed in seen:
                continue
            seen.add(seed)
            deduped.append(seed)

        return deduped

    def _is_br_context(
        self,
        base_domains: set[str],
    ) -> bool:
        return any(domain in base_domains for domain in PROVIDER_BR_BASE_DOMAINS)

    def _build_br_station_slugs(
        self,
        station: StationMatch | None,
        resolved: ResolvedStream,
    ) -> list[str]:
        def compact(value: str) -> str:
            text = (value or "").strip().lower()
            if not text:
                return ""
            for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
                text = text.replace(src, dst)
            return re.sub(r"[^a-z0-9]+", "", text)

        candidates = []
        if station:
            candidates.append(str(station.raw_record.get("slug") or ""))
            candidates.append(station.name or "")
        candidates.append(resolved.station_name or "")

        slugs: list[str] = []
        seen = set()
        for candidate in candidates:
            slug = compact(candidate)
            if not slug or slug in seen:
                continue
            if not (slug.startswith("bayern") or slug.startswith("b5") or slug.startswith("br")):
                continue
            seen.add(slug)
            slugs.append(slug)
        return slugs[:4]

    def _build_br_radio_graphql_url(self, station_slug: str) -> str:
        query = (
            "query broadcastService($stationSlug:String!){"
            "audioBroadcastService(slug:$stationSlug){"
            "...on AudioBroadcastService{"
            "id dvbServiceId name slug fallbackTeaserImage{url} "
            "trackingInfos{pageVars mediaVars} "
            "...on MangoBroadcastService{webcamUrls ...jumpMarkers} "
            "epg(slots:[CURRENT]){"
            "broadcastEvent{"
            "trackingInfos{pageVars mediaVars} "
            "...eventStartEnd "
            "items{...audioElement ...on NewsElement{author} ...on MusicElement{performer composer}} "
            "excludedTimeRanges{start end} "
            "publicationOf{...eventMetadata defaultTeaserImage{url} ...on MangoProgramme{canonicalUrl title kicker}}"
            "}"
            "} "
            "description url sophoraLivestreamDocuments{...regioStreamData}"
            "}"
            "}"
            "} "
            "fragment regioStreamData on SophoraDocumentSummary{"
            "sophoraId streamingUrl title reliveUrl trackingInfos{mediaVars}"
            "} "
            "fragment eventMetadata on MangoCreativeWorkInterface{"
            "id kicker title description"
            "} "
            "fragment jumpMarkers on MangoBroadcastService{"
            "lastNewsDate lastTrafficDate lastWeatherDate"
            "} "
            "fragment audioElement on AudioElement{"
            "guid title class start duration"
            "} "
            "fragment eventStartEnd on MangoBroadcastEvent{"
            "id start end"
            "}"
        )
        params = {
            "query": query,
            "variables[stationSlug]": station_slug,
        }
        return "https://brradio.br.de/radio/v4?" + urlencode(params, doseq=True)

    def _is_ndr_context(
        self,
        base_domains: set[str],
    ) -> bool:
        return any(domain in base_domains for domain in PROVIDER_NDR_BASE_DOMAINS)

    def _build_ndr_station_slugs(
        self,
        station: StationMatch | None,
        resolved: ResolvedStream,
    ) -> list[str]:
        def compact(value: str) -> str:
            text = (value or "").strip().lower()
            if not text:
                return ""
            return re.sub(r"[^a-z0-9]+", "", text)

        candidates = []
        if station:
            candidates.append(str(station.raw_record.get("slug") or ""))
            candidates.append(station.name or "")
            homepage = (station.homepage or "").strip()
            if homepage:
                parsed_home = urlparse(homepage)
                for segment in parsed_home.path.split("/"):
                    segment = segment.strip()
                    if segment:
                        candidates.append(segment)
        candidates.append(resolved.station_name or "")

        slugs: list[str] = []
        seen = set()
        for candidate in candidates:
            slug = compact(candidate)
            if not slug:
                continue
            if "ndr" not in slug:
                continue
            if len(slug) > 32:
                continue
            if slug.startswith("www"):
                slug = slug[3:]
            if slug.endswith("json"):
                slug = slug[:-4]
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
        return slugs[:6]

    def _is_bcs_context(
        self,
        base_domains: set[str],
    ) -> bool:
        return any(domain in base_domains for domain in PROVIDER_BCS_BASE_DOMAINS)

    def _discover_bcs_station_feed_urls(
        self,
        documents: list[tuple[str, str, list[str]]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        base_domains = {
            base
            for base in (
                get_base_domain(resolved.resolved_url),
                get_base_domain(resolved.delivery_url),
                get_base_domain(station.homepage if station else ""),
            )
            if base
        }
        if not self._is_bcs_context(base_domains):
            return set()

        page_urls = set()
        for alias in self._build_bcs_channel_aliases(resolved, station):
            page_urls.add(f"https://{PROVIDER_BCS_WEBRADIO_HOST}/{alias}")
            page_urls.add(
                f"https://{PROVIDER_BCS_IFRAME_HOST}/player/iframe/?no_cache=1&referer=/{alias}&v=2"
            )
            page_urls.add(f"https://{PROVIDER_BCS_IFRAME_HOST}/player/iframe/{alias}")

        for doc_url, _, extracted_urls in documents:
            for candidate in [doc_url, *extracted_urls]:
                parsed = urlparse(candidate or "")
                host = (parsed.netloc or "").lower()
                if host in {PROVIDER_BCS_WEBRADIO_HOST, PROVIDER_BCS_IFRAME_HOST}:
                    page_urls.add(candidate)

        if not page_urls:
            return set()

        document_lookup = {
            str(doc_url or "").strip(): text
            for doc_url, text, _ in documents
            if str(doc_url or "").strip() and text
        }

        feeds = set()
        for page_url in sorted(page_urls):
            text = document_lookup.get(page_url, "")
            if not text:
                text, _ = self._fetch_text(page_url)
            if not text:
                continue
            feeds.update(self._extract_bcs_station_feed_candidates(page_url, text))

        if feeds:
            self._log(f"BCS-Station-Feeds gefunden: {len(feeds)}")
        return feeds

    def _build_bcs_channel_aliases(
        self,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> list[str]:
        aliases: list[str] = []
        seen = set()

        for url_value in (
            resolved.delivery_url,
            resolved.resolved_url,
            station.stream_url if station else "",
        ):
            for alias in self._extract_bcs_aliases_from_stream_url(url_value):
                if alias not in seen:
                    seen.add(alias)
                    aliases.append(alias)

        for name_value in (
            station.name if station else "",
            resolved.station_name or "",
        ):
            for alias in self._extract_bcs_aliases_from_station_name(name_value):
                if alias not in seen:
                    seen.add(alias)
                    aliases.append(alias)

        return aliases[:8]

    def _extract_bcs_aliases_from_stream_url(self, url: str) -> list[str]:
        parsed = urlparse(self._normalize_seed(url))
        host = (parsed.netloc or "").lower()
        parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
        aliases: list[str] = []

        if host.endswith(".radio.hitradio-rtl.de") and parts:
            stream_key = parts[0]
            if stream_key.startswith("hrrtl-") and len(stream_key) > len("hrrtl-"):
                aliases.append(stream_key[len("hrrtl-") :])

        if get_base_domain(host) == "bcs-systems.de" and len(parts) >= 2 and parts[0] == "hrrtl":
            candidate = parts[1]
            if candidate not in {"mp3", "aac", "web"}:
                aliases.append("livestream" if candidate == "livestream" else candidate)

        return [alias for alias in aliases if re.fullmatch(r"[a-z0-9-]{2,32}", alias)]

    def _extract_bcs_aliases_from_station_name(self, value: str) -> list[str]:
        aliases = []
        for token in split_search_tokens(value):
            token = token.strip().lower()
            if not token:
                continue
            if token in PROVIDER_BCS_GENERIC_NAME_TOKENS:
                continue
            if not re.fullmatch(r"[a-z0-9-]{2,32}", token):
                continue
            aliases.append(token)
        return aliases

    def _extract_bcs_station_feed_candidates(self, page_url: str, text: str) -> set[str]:
        if not text:
            return set()

        feed_urls = set()
        normalized_text = html.unescape(html.unescape(text)).replace("\\/", "/")

        def _append_candidate(json_url: str, station_key: str) -> None:
            resolved_json_url = html.unescape(json_url.strip())
            normalized_station_key = station_key.strip().lower()
            if not is_probable_url(resolved_json_url):
                resolved_json_url = urljoin(page_url, resolved_json_url)
            if not is_probable_url(resolved_json_url):
                return
            if not re.fullmatch(r"[a-z0-9-]{2,32}", normalized_station_key):
                return
            feed_urls.add(self._build_bcs_current_candidate_url(resolved_json_url, normalized_station_key))

        script_blocks = re.findall(
            r"<script\b[^>]*>(.*?)</script>",
            normalized_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assignment_pattern = re.compile(
            r"jsonUrl\s*=\s*['\"]([^'\"]+)['\"].{0,800}?station\s*=\s*['\"]([^'\"]+)['\"]",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for block in script_blocks:
            for match in assignment_pattern.finditer(block):
                _append_candidate(match.group(1), match.group(2))

        if feed_urls:
            return feed_urls

        json_match = re.search(r"jsonUrl\s*=\s*['\"]([^'\"]+)['\"]", normalized_text, flags=re.IGNORECASE)
        if not json_match:
            return feed_urls
        search_start = json_match.end()
        station_match = re.search(
            r"station\s*=\s*['\"]([^'\"]+)['\"]",
            normalized_text[search_start : search_start + 800],
            flags=re.IGNORECASE,
        )
        if station_match:
            _append_candidate(json_match.group(1), station_match.group(1))
        return feed_urls

    def _build_bcs_current_candidate_url(self, feed_url: str, station_key: str) -> str:
        parsed = urlparse(feed_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query[BCS_CURRENT_MODE_PARAM] = BCS_CURRENT_MODE_VALUE
        query[BCS_CURRENT_STATION_PARAM] = station_key
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _get_bcs_current_candidate_station(self, url: str) -> str:
        parsed = urlparse(url or "")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        return str(query.get(BCS_CURRENT_STATION_PARAM) or "").strip().lower()

    def _is_bcs_current_candidate(self, url: str) -> bool:
        parsed = urlparse(url or "")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        return query.get(BCS_CURRENT_MODE_PARAM) == BCS_CURRENT_MODE_VALUE and bool(
            query.get(BCS_CURRENT_STATION_PARAM)
        )

    def _probe_bcs_current_candidate(self, url: str) -> SongInfo | None:
        parsed = urlparse(url or "")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        station_key = str(query.pop(BCS_CURRENT_STATION_PARAM, "") or "").strip().lower()
        mode_value = str(query.pop(BCS_CURRENT_MODE_PARAM, "") or "").strip()
        if mode_value != BCS_CURRENT_MODE_VALUE or not station_key:
            return None

        request_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
        payload_text, content_type = self._fetch_text(self._cache_bust_url(request_url))
        if not payload_text:
            return None
        if not self._is_json_candidate(request_url, content_type, payload_text):
            return None

        payload_text = self._unwrap_jsonp_payload(payload_text)
        try:
            data = json.loads(payload_text)
        except json.JSONDecodeError:
            return None

        station_node = self._select_bcs_current_station_entry(data, station_key)
        if not isinstance(station_node, dict):
            return None

        title = self._extract_json_value(station_node, TITLE_KEYS).strip()
        artist = self._extract_artist_from_node(station_node).strip()
        if title and not artist:
            split_artist, split_title = self._split_compound_title(title)
            if split_artist and split_title:
                artist = split_artist
                title = split_title
        if not title and not artist:
            return None

        stream_title = f"{artist} - {title}".strip(" -")
        return SongInfo(
            stream_title=stream_title,
            raw_metadata=payload_text,
            artist=artist,
            title=title,
            source_kind="web_feed_json",
            source_url=url,
        )

    def _select_bcs_current_station_entry(self, payload: dict | list, station_key: str) -> dict | None:
        if not isinstance(payload, dict):
            return None
        root = payload.get("data")
        if not isinstance(root, dict):
            return None

        direct = root.get(station_key)
        if isinstance(direct, dict):
            return direct

        station_compact = re.sub(r"[^a-z0-9]", "", station_key.lower())
        for key, value in root.items():
            if not isinstance(value, dict):
                continue
            key_compact = re.sub(r"[^a-z0-9]", "", str(key or "").lower())
            if key_compact == station_compact:
                return value
        return None

    def _bcs_station_selector_matches_context(
        self,
        url: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> bool:
        station_key = self._get_bcs_current_candidate_station(url)
        if not station_key:
            return False

        aliases = self._build_bcs_channel_aliases(resolved, station)
        if not aliases:
            return True
        if station_key in aliases:
            return True

        compact_station_key = re.sub(r"[^a-z0-9]", "", station_key)
        for alias in aliases:
            if re.sub(r"[^a-z0-9]", "", alias) == compact_station_key:
                return True
        return False

    def _extract_radio_directory_slug(self, homepage: str) -> str:
        parsed = urlparse((homepage or "").strip())
        host = (parsed.hostname or "").lower()
        if not host:
            return ""
        if not (host.endswith("radio.de") or host.endswith("radio.net")):
            return ""

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "s":
            slug = parts[1].strip().lower()
            if re.fullmatch(r"[a-z0-9-]{3,}", slug):
                return slug
        return ""

    def _normalize_seed(self, value: str) -> str:
        clean = value.strip()
        if not clean:
            return ""
        if clean.startswith("www."):
            return f"https://{clean}"
        if is_probable_url(clean):
            return clean
        return ""

    def _extract_urls_from_document(self, text: str, base_url: str) -> list[str]:
        normalized_text = (
            html.unescape(html.unescape(text))
            .replace("\\/", "/")
            .replace("\\u002f", "/")
            .replace("\\u002F", "/")
            .replace("\\u003a", ":")
            .replace("\\u003A", ":")
        )
        urls: list[str] = []
        seen_urls = set()

        preferred_bases = []
        canonical_match = re.search(
            r"<link[^>]*rel=[\"'][^\"']*\bcanonical\b[^\"']*[\"'][^>]*href=[\"']([^\"']+)[\"']",
            normalized_text,
            flags=re.IGNORECASE,
        )
        base_href_match = re.search(
            r"<base[^>]*href=[\"']([^\"']+)[\"']",
            normalized_text,
            flags=re.IGNORECASE,
        )
        for candidate_base in (
            (canonical_match.group(1) if canonical_match else ""),
            (base_href_match.group(1) if base_href_match else ""),
        ):
            candidate_base = html.unescape(str(candidate_base or "").strip())
            if not candidate_base or not is_probable_url(candidate_base):
                continue
            if candidate_base in preferred_bases:
                continue
            preferred_bases.append(candidate_base)
        join_bases = preferred_bases or [base_url]

        def _remember(raw_url: str) -> None:
            normalized = html.unescape(str(raw_url or "").strip())
            if not normalized:
                return
            if not is_probable_url(normalized):
                return
            if normalized in seen_urls:
                return
            seen_urls.add(normalized)
            urls.append(normalized)

        for match in re.findall(r"https?://[^\"'`\s<>()]+", normalized_text, flags=re.IGNORECASE):
            _remember(match)

        for match in re.findall(r"(?:href|src|data-[a-z0-9_-]+)=[\"']([^\"']+)[\"']", normalized_text, flags=re.IGNORECASE):
            if match.startswith("javascript:"):
                continue
            if is_probable_url(match):
                _remember(match)
            elif match.startswith("www."):
                _remember(f"https://{match}")
            elif match.startswith("//"):
                base_scheme = urlparse(base_url).scheme or "https"
                _remember(f"{base_scheme}:{match}")
            else:
                for join_base in join_bases:
                    _remember(urljoin(join_base, match))

        for match in re.findall(
            r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.xml|\.json)(?:\?[^\"'\s<>()]+)?",
            normalized_text,
            flags=re.IGNORECASE,
        ):
            if match.startswith("/www."):
                _remember(f"https://{match.lstrip('/')}")
            else:
                for join_base in join_bases:
                    _remember(urljoin(join_base, match))

        for match in re.findall(
            r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.html|\.htm)(?:\?[^\"'\s<>()]+)?",
            normalized_text,
            flags=re.IGNORECASE,
        ):
            lower = match.lower()
            if not any(
                token in lower
                for token in (
                    "now",
                    "onair",
                    "current",
                    "playlist",
                    "track",
                    "song",
                    "titel",
                    "title",
                    "live",
                    "radiomodul",
                )
            ):
                continue
            if match.startswith("/www."):
                _remember(f"https://{match.lstrip('/')}")
            else:
                for join_base in join_bases:
                    _remember(urljoin(join_base, match))

        for match in re.findall(
            r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.js)(?:\?[^\"'\s<>()]+)?",
            normalized_text,
            flags=re.IGNORECASE,
        ):
            if match.startswith("/www."):
                _remember(f"https://{match.lstrip('/')}")
            else:
                for join_base in join_bases:
                    _remember(urljoin(join_base, match))

        effective_base = join_bases[0] if join_bases else base_url
        parsed_base = urlparse(effective_base)
        base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}" if parsed_base.scheme and parsed_base.netloc else ""
        relative_roots = []
        seen_relative_roots = set()
        for match in re.findall(r"['\"](/[^'\"`\s<>()]{1,120}/)['\"]", normalized_text, flags=re.IGNORECASE):
            lower = match.lower()
            if not any(
                hint in lower
                for hint in ("webradio", "radio", "player", "playlist", "current", "metadata", "stream", "live", "onair")
            ):
                continue
            if match in seen_relative_roots:
                continue
            seen_relative_roots.add(match)
            relative_roots.append(match)

        relative_feed_files = []
        seen_relative_feed_files = set()
        for match in re.findall(
            r"['\"]([A-Za-z0-9][A-Za-z0-9_.~:-]*(?:playlist|current|nowplaying|now-playing|currentsong|track|song|metadata|onair|title|titelliste)[A-Za-z0-9_.~:-]*\.(?:json|xml)(?:\?[^'\"\s<>()]+)?)['\"]",
            normalized_text,
            flags=re.IGNORECASE,
        ):
            if "/" in match:
                continue
            if match in seen_relative_feed_files:
                continue
            seen_relative_feed_files.add(match)
            relative_feed_files.append(match)

        if base_origin and relative_roots and relative_feed_files:
            for relative_root in relative_roots[:8]:
                root_url = urljoin(base_origin, relative_root)
                for relative_feed_file in relative_feed_files[:16]:
                    _remember(urljoin(root_url, relative_feed_file))

        relative_script_files = []
        seen_relative_script_files = set()
        for match in re.findall(
            r"['\"]([A-Za-z0-9][A-Za-z0-9_.~:-]*(?:main|chunk|player|radio|app|bundle|common|prefetch)[A-Za-z0-9_.~:-]*\.js(?:\?[^'\"\s<>()]+)?)['\"]",
            normalized_text,
            flags=re.IGNORECASE,
        ):
            if "/" in match:
                continue
            if match in seen_relative_script_files:
                continue
            seen_relative_script_files.add(match)
            relative_script_files.append(match)

        for relative_script_file in relative_script_files[:16]:
            for join_base in join_bases:
                _remember(urljoin(join_base, relative_script_file))

        # Generic CMS pattern: entries like "something--100" often expose "*-avCustom.xml".
        for content_id in re.findall(r"([a-z0-9][a-z0-9-]{5,}--\d+)", normalized_text, flags=re.IGNORECASE):
            lower_id = content_id.lower()
            if not any(hint in lower_id for hint in ("live", "stream", "radio", "onair")):
                continue
            for join_base in join_bases:
                _remember(urljoin(join_base, f"/stream/{content_id}-avCustom.xml"))
                _remember(urljoin(join_base, f"/{content_id}-avCustom.xml"))

        return urls

    def _looks_like_feed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        query = parsed.query.lower()
        haystack = f"{path}?{query}"
        lower_url = url.lower()

        if "${" in url or "%24%7b" in lower_url:
            return False
        if "form_action_url=" in lower_url:
            return False
        if "well-known" in path:
            return False
        if "video" in haystack:
            return False
        if any(token in path for token in ("current-track", "current_track", "currenttrack")):
            return True

        if "avcustom" in path:
            return any(hint in path for hint in ("live", "stream", "radio", "onair"))

        if "status-json.xsl" in path:
            return True
        if path.endswith(".xsl") and "status" in path:
            return True
        if host.endswith("top-stream-service.loverad.io") and path.startswith("/v1/"):
            return True
        if host.startswith("iris-") and host.endswith(".loverad.io") and path.endswith("/flow.json"):
            return True
        if host == "brradio.br.de" and path == "/radio/v4":
            if "stationslug" in query and ("audiobroadcastservice" in query or "broadcastservice" in query):
                return True

        if any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".m3u8")):
            return False
        if path.endswith("/streams.json") or path.endswith("streams.json"):
            return False

        has_keyword = any(keyword in haystack for keyword in NOWPLAYING_CANDIDATE_KEYWORDS)
        strong_api_keyword = any(
            keyword in haystack
            for keyword in (
                "currentsong",
                "nowplaying",
                "now-playing",
                "onair",
                "playlist",
                "playout",
                "titelliste",
                "metadata",
            )
        )
        has_feed_ext = path.endswith(".xml") or path.endswith(".json")
        has_query_feed_hint = (
            "output=xml" in query
            or "output=json" in query
            or "format=xml" in query
            or "format=json" in query
        )
        has_api_hint = (
            "api" in path
            or "scripts" in path
            or parsed.netloc.lower().startswith("api.")
            or ".api." in parsed.netloc.lower()
        )

        if has_feed_ext and (has_keyword or strong_api_keyword):
            return True
        if has_query_feed_hint and (has_keyword or has_api_hint):
            return True
        if has_api_hint and strong_api_keyword:
            return True
        if "metadata/channel/" in path and (path.endswith(".json") or path.endswith("/")):
            return True
        if self._looks_like_html_nowplaying_endpoint(url):
            if self._is_editorial_html_candidate(url):
                return False
            return True
        return False

    def _is_strong_nowplaying_feed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        path_query = f"{path}?{query}"
        if "current-track" in path_query or "current_track" in path_query:
            return True
        if "status-json.xsl" in path_query:
            return True
        if "currentsong" in path_query:
            return True
        if "output=xml" in path_query or "output=json" in path_query:
            return True
        if "format=xml" in path_query or "format=json" in path_query:
            return True
        if "/metadata/channel/" in path_query:
            return True
        if "/now_on_air" in path_query or "nowonair" in path_query:
            return True
        if "/playlist" in path_query or "titelliste" in path_query or "nowplaying" in path_query:
            return True
        if path.endswith(".json") and any(
            token in path_query for token in ("song", "track", "current", "now", "playlist", "onair", "metadata")
        ):
            return True
        if path.endswith(".xml") and any(
            token in path_query for token in ("song", "track", "current", "now", "playlist", "onair", "metadata")
        ):
            return True
        return False

    def _looks_like_html_nowplaying_endpoint(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        haystack = f"{path}?{query}"
        last_segment = path.rsplit("/", 1)[-1]

        has_html_hint = (
            path.endswith(".html")
            or path.endswith(".htm")
            or path.endswith(".jsp")
            or ".html/" in path
            or ".htm/" in path
        )
        has_reload_hint = any(
            token in haystack
            for token in (
                "ssi=true",
                "module=",
                "box=",
                "middlecolumnlist",
                "reloadcontent",
                "jsb_reloadcontent",
            )
        )
        has_nowplaying_hint = any(
            token in haystack
            for token in (
                "now",
                "onair",
                "now_on_air",
                "nowonair",
                "playlist",
                "track",
                "song",
                "titel",
                "title",
                "livestream",
                "current",
                "radiomodul",
            )
        )
        has_direct_nowplaying_path = any(
            token in path
            for token in (
                "now_on_air",
                "nowonair",
                "now-playing",
                "nowplaying",
                "currenttitle",
                "currentsong",
                "/songs.html",
                "/songs.htm",
                "/playlist/",
                "playlist-",
                "playlist/index.jsp",
                "playlist/index.html",
                "titelsuche",
                "radioplayerplaylist",
                "jetztimprogramm",
                "radiomodul",
            )
        )
        has_plain_endpoint_path = bool(last_segment) and "." not in last_segment and not path.endswith("/")
        if has_html_hint and has_direct_nowplaying_path:
            return True
        if has_plain_endpoint_path and has_direct_nowplaying_path:
            return True
        return (has_html_hint or has_reload_hint) and has_reload_hint and has_nowplaying_hint

    def _looks_like_discovery_page(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
            return False
        if self._is_editorial_html_candidate(url):
            return False
        if path.endswith(".html") or path.endswith("/"):
            hints = ("stream", "livestream", "radio", "playlist", "onair", "now")
            return any(hint in path for hint in hints)
        return False

    def _is_editorial_html_candidate(self, url: str) -> bool:
        if not self._looks_like_html_nowplaying_endpoint(url):
            return False
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        haystack = f"{path}?{query}"
        last_segment = path.rsplit("/", 1)[-1]
        has_dynamic_nowplaying_hint = any(
            token in haystack
            for token in (
                "ssi=true",
                "module=",
                "box=",
                "middlecolumnlist",
                "reloadcontent",
                "jsb_reloadcontent",
                "now_on_air",
                "nowonair",
                "now-playing",
                "currenttitle",
                "currentsong",
                "radiomodul",
            )
        )
        if has_dynamic_nowplaying_hint:
            return False
        has_editorial_token = any(token in haystack for token in NOWPLAYING_HTML_EDITORIAL_TOKENS)
        has_article_like_slug = last_segment.count("-") >= 4 or bool(re.search(r"-\d{2,4}(?:[./]|$)", last_segment))
        if has_editorial_token and has_article_like_slug:
            return True
        if has_editorial_token and "playlist-" in haystack and not parsed.query:
            return True
        return False

    def _take_budgeted_urls(
        self,
        urls: list[str],
        visited_urls: set[str],
        budget: int,
    ) -> tuple[list[str], int]:
        selected = []
        remaining = max(0, int(budget))
        for url in urls:
            if remaining <= 0:
                break
            if not url or url in visited_urls:
                continue
            visited_urls.add(url)
            selected.append(url)
            remaining -= 1
        return selected, remaining

    def _fetch_documents_parallel(self, urls: list[str]) -> list[tuple[str, str]]:
        ordered_urls = []
        seen = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            ordered_urls.append(url)
        if not ordered_urls:
            return []
        if len(ordered_urls) == 1:
            text, _ = self._fetch_text(ordered_urls[0])
            return [(ordered_urls[0], text)] if text else []

        worker_count = max(1, min(self._crawl_max_workers, len(ordered_urls)))
        results: dict[int, tuple[str, str]] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(self._fetch_text, url): idx for idx, url in enumerate(ordered_urls)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    text, _ = future.result()
                except Exception:
                    text = ""
                if text:
                    results[idx] = (ordered_urls[idx], text)
        return [results[idx] for idx in range(len(ordered_urls)) if idx in results]

    def _looks_like_script_asset(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if not path.endswith(".js"):
            return False
        if "video" in path:
            return False
        hints = ("webcode", "player", "radio", "main", "app", "bundle", "chunk", "critical", "entry")
        return any(hint in path for hint in hints)

    def _script_asset_priority(self, url: str) -> int:
        path = urlparse(url).path.lower()
        score = 0
        if "main-chunk" in path:
            score += 80
        if "audioplayer" in path or "webradio" in path:
            score += 70
        if "critical-chunk" in path:
            score += 50
        if "main" in path:
            score += 25
        if "entry" in path:
            score += 22
        if "chunk" in path:
            score += 20
        if "audio" in path:
            score += 15
        if "player" in path:
            score += 10
        if "radio" in path:
            score += 8
        if "common" in path:
            score -= 10
        if "prefetch" in path:
            score -= 20
        return score

    def _prioritize_script_asset_urls(self, urls: list[str], seed_url: str) -> list[str]:
        filtered = []
        seen = set()
        for url in urls:
            if not self._looks_like_script_asset(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            filtered.append(url)
        return sorted(filtered, key=lambda url: (self._script_asset_priority(url), url), reverse=True)

    def _looks_like_stream_endpoint(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if path.endswith((".mp3", ".aac", ".ogg", ".m4a", ".flac")):
            return True
        if any(token in path for token in ("/iradio", "/stream", "/live", "/listen", "/radio")) and any(
            token in path for token in ("mp3", "aac", "ogg")
        ):
            return True
        return host.startswith("stream.") and any(token in path for token in ("mp3", "aac", "ogg", "iradio"))

    def _candidate_score(self, url: str) -> int:
        lower = url.lower()
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path_query = f"{parsed.path.lower()}?{parsed.query.lower()}"
        path = parsed.path.lower()
        query = parsed.query.lower()
        query_params = {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
        score = 0
        if host == "brradio.br.de" and path == "/radio/v4":
            score += 140
        if host.startswith("iris-") and host.endswith(".loverad.io") and path.endswith("/flow.json"):
            score += 90
        if path.endswith(".xml"):
            score += 5
        if path.endswith(".json"):
            score += 5
        for keyword in NOWPLAYING_CANDIDATE_KEYWORDS:
            if keyword in path_query:
                score += 10
        if "avcustom" in lower:
            score += 40
        direct_titellisten_file = (
            "/xml/titellisten/" in path
            and "xml-index.do" not in path_query
            and (path.endswith(".json") or path.endswith(".xml"))
        )
        if direct_titellisten_file:
            # Prefer concrete playlist documents over provider index endpoints.
            score += 55
            if path.endswith(".json"):
                score += 20
            elif path.endswith(".xml"):
                score += 10
        elif "titellisten/xml-index.do" in path_query:
            score += 15
        elif "/xml/titellisten/" in path_query:
            score += 35
        if "playlist" in path_query or "titelliste" in path_query:
            score += 20
        if "/webradio/playlist/" in path_query:
            score -= 10
        if "/~webradio/" in path_query and path.endswith(".json"):
            score += 25
        if "/current/" in path_query and path.endswith(".json"):
            score += 60
        if "current-track" in path_query or "current_track" in path_query:
            score += 55
        if "output=xml" in path_query or "output=json" in path_query:
            score += 25
        if "currentsong" in path_query:
            score += 50
        if "livestream" in path_query and "box=2" in path_query and "middlecolumnlist" in path_query:
            score += 95
        if "radiomodul-" in path_query:
            score += 90
        if "/musik/playlist/index.jsp" in path_query:
            score += 45
        if "now_on_air" in path_query:
            score += 80
        elif "nowonair" in path_query:
            score += 35
        if "songs.html" in path_query or "songs.htm" in path_query:
            score -= 20
        if "ctrl-api" in path_query:
            score += 25
        if "ctrl-api/getplaylist" in path_query:
            score += 55
            if "typ=hour" in query:
                score += 20
            if "ts=" in query:
                score += 15
            ts_value = str(query_params.get("ts") or "").strip()
            if ts_value.isdigit():
                bucket = int(ts_value)
                current_bucket = int(time.time() // 3600) * 3600
                if bucket == current_bucket:
                    score += 35
                elif bucket == current_bucket - 3600:
                    score += 10
                elif bucket > current_bucket:
                    score -= 120
                else:
                    score -= 40
        elif "ctrl-api/getcurrentsong" in path_query:
            score += 10
        if "metadata/channel/" in path_query:
            score += 30
        if "status-json.xsl" in path_query:
            score += 45
        elif "status" in path_query and ".xsl" in path_query:
            score += 25
        if any(param in query for param in ("k=", "skey=", "channelkey=", "streamkey=", "key=")):
            score += 30
        if self._looks_like_html_nowplaying_endpoint(url):
            score += 35
        if "${" in lower or "%24%7b" in lower:
            score -= 120
        return score

    def _is_json_candidate(self, url: str, content_type: str, payload: str) -> bool:
        lower = url.lower()
        if "json" in content_type.lower():
            return True
        if ".json" in lower:
            return True
        stripped = payload.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return True
        return bool(JSONP_WRAPPER_RE.match(stripped))

    def _parse_xml_payload(self, payload: str, source_url: str) -> SongInfo | None:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            return None

        best = None
        best_score = -1

        for elem in root.iter():
            title = self._get_xml_child_text(elem, TITLE_KEYS)
            artist = self._get_xml_child_text(elem, ARTIST_KEYS)
            time_text = self._get_xml_child_text(elem, TIME_KEYS)
            duration_text = self._get_xml_child_text(elem, DURATION_KEYS)
            if not title and not artist:
                continue

            status_score = self._xml_status_score(elem)
            quality_score = 10 if title else 0
            quality_score += 7 if artist else 0
            score = status_score + quality_score

            age_minutes = self._age_minutes(time_text)
            if age_minutes is not None:
                if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                    score -= 120
                else:
                    score += 20
            if self._is_duration_window_expired(time_text, duration_text):
                score -= 120

            if score > best_score:
                best = (artist, title, time_text, duration_text, age_minutes)
                best_score = score

        if not best:
            return None

        artist, title, best_time_text, best_duration_text, best_age_minutes = best
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title and not artist:
            return None
        if not artist and best_score < 50:
            # Avoid generic titles from unrelated XML docs.
            return None
        if self._age_minutes(best_time_text) is not None and self._age_minutes(best_time_text) > MAX_NOWPLAYING_AGE_MINUTES:
            return None
        if self._is_duration_window_expired(best_time_text, best_duration_text):
            return None

        stream_title = f"{artist} - {title}".strip(" -")
        return SongInfo(
            stream_title=stream_title,
            raw_metadata=payload,
            artist=artist,
            title=title,
            age_minutes=best_age_minutes,
            source_kind="web_feed_xml",
            source_url=source_url,
        )

    def _xml_status_score(self, elem: ET.Element) -> int:
        status = ""
        for attr_key, attr_value in elem.attrib.items():
            if str(attr_key or "").strip().lower() == "status":
                status = str(attr_value or "").strip().lower()
                break
        if status in {"now", "current", "onair", "live"}:
            return 100
        if status in {"next", "upcoming"}:
            return 20

        for key in STATUS_KEYS:
            value = self._get_xml_child_text(elem, {key})
            if not value:
                continue
            value_lower = value.lower()
            if value_lower in {"now", "current", "onair", "live", "true", "1"}:
                return 90
        return 0

    def _get_xml_child_text(self, elem: ET.Element, keyset: set[str]) -> str:
        for child in list(elem):
            tag = self._strip_xml_ns(child.tag).lower()
            if tag in keyset:
                return (child.text or "").strip()
        return ""

    def _parse_json_payload(self, payload: str, source_url: str) -> SongInfo | None:
        payload = self._unwrap_jsonp_payload(payload)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None

        candidates = self._extract_iris_flow_candidates(data)
        candidates.extend(self._extract_ctrl_api_playlist_candidates(data, source_url))
        candidates.extend(self._extract_radioplayer_event_candidates(data))
        candidates.extend(self._extract_graphql_stream_track_candidates(data))
        br_candidates = self._extract_br_radio_candidates(data)
        if br_candidates:
            candidates.extend(br_candidates)
        else:
            if isinstance(data, dict):
                for key in ("trackInfo", "currentTrack", "nowPlaying"):
                    node = data.get(key)
                    if not isinstance(node, dict):
                        continue
                    title = self._extract_json_value(node, TITLE_KEYS)
                    artist = self._extract_artist_from_node(node)
                    if title and not artist:
                        split_artist, split_title = self._split_compound_title(title)
                        if split_artist and split_title:
                            artist = split_artist
                            title = split_title
                    time_text = self._extract_json_value(data, TIME_KEYS) or self._extract_json_value(node, TIME_KEYS)
                    duration_text = self._extract_json_value(node, DURATION_KEYS)
                    if not title and not artist:
                        continue
                    score = 0
                    if title:
                        score += 10
                    if artist:
                        score += 8
                    if key.lower() in {"trackinfo", "currenttrack", "nowplaying"}:
                        score += 30
                    score += self._json_status_score(node)
                    score += self._json_playing_mode_score(node)
                    score += self._json_time_window_score(time_text, duration_text)
                    age_minutes = self._age_minutes(time_text)
                    if age_minutes is not None:
                        if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                            score -= 120
                        elif age_minutes >= 0:
                            score += 20
                        else:
                            score -= 40
                    candidates.append((score, artist, title, time_text, duration_text, age_minutes))

            for node in self._walk_json_objects(data):
                title = self._extract_json_value(node, TITLE_KEYS)
                artist = self._extract_artist_from_node(node)
                if title and not artist:
                    split_artist, split_title = self._split_compound_title(title)
                    if split_artist and split_title:
                        artist = split_artist
                        title = split_title
                time_text = self._extract_json_value(node, TIME_KEYS)
                duration_text = self._extract_json_value(node, DURATION_KEYS)
                if not title and not artist:
                    continue

                score = 0
                if title:
                    score += 10
                if artist:
                    score += 8
                score += self._json_status_score(node)
                score += self._json_playing_mode_score(node)
                score += self._json_time_window_score(time_text, duration_text)
                age_minutes = self._age_minutes(time_text)
                if age_minutes is not None:
                    if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                        score -= 120
                    elif age_minutes >= 0:
                        score += 20
                    else:
                        score -= 40

                candidates.append((score, artist, title, time_text, duration_text, age_minutes))

        if not candidates:
            return None

        return self._build_song_from_scored_candidates(candidates, payload, source_url)

    def _extract_ctrl_api_playlist_candidates(
        self,
        data: dict | list,
        source_url: str,
    ) -> list[tuple[int, str, str, str, str, int | None]]:
        lower_url = str(source_url or "").strip().lower()
        if "ctrl-api/getplaylist" not in lower_url:
            return []
        if not isinstance(data, dict):
            return []

        entries = data.get("data")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return []

        timeline: list[tuple[float, str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = self._extract_json_value(entry, TITLE_KEYS).strip()
            artist = self._extract_artist_from_node(entry).strip()
            if title and not artist:
                split_artist, split_title = self._split_compound_title(title)
                if split_artist and split_title:
                    artist = split_artist
                    title = split_title
            if not title or not artist:
                continue

            ts_text = self._extract_json_value(entry, {"ts", "timestamp", "starttime", "start"}).strip()
            started_at = self._parse_datetime(ts_text)
            if not started_at:
                continue
            if started_at.tzinfo is None:
                start_ts = started_at.timestamp()
            else:
                start_ts = started_at.astimezone(timezone.utc).timestamp()
            timeline.append((start_ts, artist, title))

        if not timeline:
            return []

        timeline.sort(key=lambda item: item[0])
        now_ts = time.time()
        future_grace = max(0.0, float(DISCOVERY_CTRL_API_FUTURE_GRACE_SECONDS))
        start_delay = max(0.0, float(DISCOVERY_CTRL_API_START_DELAY_SECONDS))
        effective_now_ts = now_ts - start_delay

        active_idx = -1
        for idx, (start_ts, _, _) in enumerate(timeline):
            if start_ts <= effective_now_ts + future_grace:
                active_idx = idx
            else:
                break
        if active_idx < 0:
            return []

        start_ts, artist, title = timeline[active_idx]
        next_start_ts = timeline[active_idx + 1][0] if active_idx + 1 < len(timeline) else 0.0
        if next_start_ts and next_start_ts <= effective_now_ts:
            return []

        duration_text = ""
        if next_start_ts > start_ts:
            duration_seconds = int(next_start_ts - start_ts)
            if 0 < duration_seconds <= 3 * 3600:
                duration_text = str(duration_seconds)

        time_text = str(int(start_ts))
        age_minutes = self._age_minutes(time_text)
        if age_minutes is not None and age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
            return []

        score = 420
        if next_start_ts and start_ts <= effective_now_ts < next_start_ts:
            score += 220
        elif next_start_ts and effective_now_ts < start_ts:
            return []
        elif next_start_ts and effective_now_ts >= next_start_ts:
            score -= 180
        if age_minutes is not None:
            if age_minutes >= 0:
                score += 20
            else:
                score -= 160

        return [(score, artist, title, time_text, duration_text, age_minutes)]

    def _build_song_from_scored_candidates(
        self,
        candidates: list[tuple[int, str, str, str, str, int | None]],
        payload: str,
        source_url: str,
    ) -> SongInfo | None:
        candidates.sort(reverse=True, key=lambda item: item[0])
        best_score, artist, title, time_text, duration_text, age_minutes = candidates[0]
        title = title.strip()
        artist = artist.strip()
        if not artist and best_score < 40:
            return None
        if self._age_minutes(time_text) is not None and self._age_minutes(time_text) > MAX_NOWPLAYING_AGE_MINUTES:
            return None
        if self._is_duration_window_expired(time_text, duration_text):
            return None
        stream_title = f"{artist} - {title}".strip(" -")

        return SongInfo(
            stream_title=stream_title,
            raw_metadata=payload,
            artist=artist,
            title=title,
            age_minutes=age_minutes,
            source_kind="web_feed_json",
            source_url=source_url,
        )

    def _parse_graphql_tracks_payload(
        self,
        data: dict | list,
        payload: str,
        source_url: str,
    ) -> SongInfo | None:
        candidates = self._extract_graphql_stream_track_candidates(data)
        if not candidates:
            return None
        return self._build_song_from_scored_candidates(candidates, payload, source_url)

    def _extract_radioplayer_event_candidates(
        self,
        data: dict | list,
    ) -> list[tuple[int, str, str, str, str, int | None]]:
        if not isinstance(data, dict):
            return []
        results = data.get("results")
        if not isinstance(results, dict):
            return []

        candidates: list[tuple[int, str, str, str, str, int | None]] = []

        def add_candidate(node: dict, bucket: str) -> None:
            if not isinstance(node, dict):
                return
            title = self._extract_json_value(node, TITLE_KEYS | RADIOPLAYER_EVENT_TITLE_KEYS).strip()
            artist = self._extract_json_value(node, ARTIST_KEYS | RADIOPLAYER_EVENT_ARTIST_KEYS).strip()
            if not title or not artist:
                return
            start_value = self._extract_json_value(node, {"starttime", "start_time", "start"}).strip()
            stop_value = self._extract_json_value(node, {"stoptime", "stop_time", "stop", "end"}).strip()
            duration_value = self._duration_from_time_range(start_value, stop_value)
            age_minutes = self._age_minutes(start_value)

            score = 40
            if bucket == "now":
                score += 260
            elif bucket == "next":
                score -= 120
            if self._is_time_range_active(start_value, stop_value):
                score += 280
            elif self._is_time_range_expired(start_value, stop_value):
                score -= 240

            if age_minutes is not None:
                if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                    score -= 160
                elif age_minutes >= 0:
                    score += 20

            candidates.append((score, artist, title, start_value, duration_value, age_minutes))

        now_node = results.get("now")
        if isinstance(now_node, dict):
            add_candidate(now_node, "now")
        for bucket in ("previous", "next"):
            nodes = results.get(bucket)
            if isinstance(nodes, dict):
                nodes = [nodes]
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                add_candidate(node, bucket)

        return candidates

    def _extract_graphql_stream_track_candidates(
        self,
        data: dict | list,
    ) -> list[tuple[int, str, str, str, str, int | None]]:
        if not isinstance(data, dict):
            return []
        root = data.get("data")
        if not isinstance(root, dict):
            return []
        stream = root.get("streamById")
        if not isinstance(stream, dict):
            return []
        stream_values = stream.get("streamValue")
        if isinstance(stream_values, dict):
            stream_values = [stream_values]
        if not isinstance(stream_values, list):
            return []

        candidates: list[tuple[int, str, str, str, str, int | None]] = []
        for stream_value in stream_values:
            if not isinstance(stream_value, dict):
                continue
            date_value = self._extract_json_value(stream_value, {"date"}).strip()
            tracks = stream_value.get("track")
            if isinstance(tracks, dict):
                tracks = [tracks]
            if not isinstance(tracks, list):
                continue
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                artist = self._extract_json_value(track, ARTIST_KEYS | RADIOPLAYER_EVENT_ARTIST_KEYS).strip()
                title = self._extract_json_value(track, TITLE_KEYS | RADIOPLAYER_EVENT_TITLE_KEYS).strip()
                start_time = self._extract_json_value(track, {"start_time", "starttime", "start"}).strip()
                duration_value = self._extract_json_value(track, DURATION_KEYS).strip()
                start_value = self._combine_date_and_time(date_value, start_time)
                if not artist or not title or not start_value or not duration_value:
                    continue

                age_minutes = self._age_minutes(start_value)
                score = 40
                score += self._json_time_window_score(start_value, duration_value)

                if age_minutes is not None:
                    if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                        score -= 140
                    elif age_minutes >= 0:
                        score += 20

                candidates.append((score, artist, title, start_value, duration_value, age_minutes))

        return candidates

    def _extract_iris_flow_candidates(
        self,
        data: dict | list,
    ) -> list[tuple[int, str, str, str, str, int | None]]:
        if not isinstance(data, dict):
            return []

        result = data.get("result")
        if not isinstance(result, dict):
            return []

        entries = result.get("entry")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return []

        candidates: list[tuple[int, str, str, str, str, int | None]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            time_text = self._extract_json_value(entry, TIME_KEYS)
            duration_text = self._extract_json_value(entry, DURATION_KEYS)
            song_root = entry.get("song")
            if not isinstance(song_root, dict):
                continue

            song_entries = song_root.get("entry")
            if isinstance(song_entries, dict):
                song_entries = [song_entries]
            if not isinstance(song_entries, list):
                continue

            for song_node in song_entries:
                if not isinstance(song_node, dict):
                    continue

                title = self._extract_json_value(song_node, TITLE_KEYS)
                if not title:
                    continue

                artist = self._extract_artist_from_node(song_node)
                artist_root = song_node.get("artist")
                if not artist and isinstance(artist_root, dict):
                    artist_entries = artist_root.get("entry")
                    if isinstance(artist_entries, dict):
                        artist_entries = [artist_entries]
                    if isinstance(artist_entries, list):
                        for artist_node in artist_entries:
                            if not isinstance(artist_node, dict):
                                continue
                            artist = self._extract_artist_from_node(artist_node)
                            if artist:
                                break

                if not artist:
                    continue

                score = 35
                age_minutes = self._age_minutes(time_text)
                if age_minutes is not None:
                    if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                        score -= 120
                    else:
                        score += 20
                if self._is_duration_window_expired(time_text, duration_text):
                    score -= 120

                candidates.append((score, artist, title, time_text, duration_text, age_minutes))

        return candidates

    def _extract_br_radio_candidates(
        self,
        data: dict | list,
    ) -> list[tuple[int, str, str, str, str, int | None]]:
        if not isinstance(data, dict):
            return []
        root = data.get("data")
        if not isinstance(root, dict):
            return []
        service = root.get("audioBroadcastService")
        if not isinstance(service, dict):
            return []
        epg = service.get("epg")
        if isinstance(epg, dict):
            epg = [epg]
        if not isinstance(epg, list):
            return []

        candidates: list[tuple[int, str, str, str, str, int | None]] = []
        for slot in epg:
            if not isinstance(slot, dict):
                continue
            broadcast_event = slot.get("broadcastEvent")
            if not isinstance(broadcast_event, dict):
                continue

            event_start = self._extract_json_value(broadcast_event, {"start"})
            event_end = self._extract_json_value(broadcast_event, {"end"})
            event_is_active = self._is_time_range_active(event_start, event_end)

            items = broadcast_event.get("items")
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                item_class = self._extract_json_value(item, {"class"}).strip().lower()
                if item_class != "music":
                    continue

                title = self._extract_json_value(item, {"title", "song", "track"})
                artist = self._extract_br_item_artist(item)
                if not title or not artist:
                    continue

                time_text = self._extract_json_value(item, TIME_KEYS)
                duration_text = self._extract_json_value(item, DURATION_KEYS)
                age_minutes = self._age_minutes(time_text)

                score = 140
                if event_is_active:
                    score += 30
                if self._is_duration_window_active(time_text, duration_text):
                    score += 260
                elif self._is_duration_window_expired(time_text, duration_text):
                    score -= 220
                else:
                    score -= 40

                if age_minutes is not None:
                    if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                        score -= 180
                    elif age_minutes >= 0:
                        score += 20
                    else:
                        score -= 40

                candidates.append((score, artist, title, time_text, duration_text, age_minutes))

        return candidates

    def _extract_artist_from_node(self, node: dict) -> str:
        if not isinstance(node, dict):
            return ""

        direct = self._extract_json_value(node, ARTIST_KEYS | {"name", "realname"})
        if direct:
            return direct

        for key in ("performer", "artist", "author", "interpret", "band", "artists"):
            nested = self._extract_nested_name(node.get(key))
            if nested:
                return nested
        return ""

    def _extract_br_item_artist(self, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        return self._extract_artist_from_node(item)

    def _extract_nested_name(self, value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            direct = self._extract_json_value(
                value,
                ARTIST_KEYS | {"name", "realname", "label", "text", "title", "value"},
            )
            if direct:
                return direct
            for list_key in ("entry", "entries", "items", "values", "list", "data"):
                nested = value.get(list_key)
                if isinstance(nested, dict):
                    nested = [nested]
                if isinstance(nested, list):
                    for child in nested:
                        found = self._extract_nested_name(child)
                        if found:
                            return found
            return ""
        if isinstance(value, list):
            for child in value:
                found = self._extract_nested_name(child)
                if found:
                    return found
        return ""

    def _parse_html_payload(self, payload: str, source_url: str) -> SongInfo | None:
        if not payload:
            return None

        artist = ""
        title = ""
        age_minutes: int | None = None
        current_show_artist, current_show_title = self._extract_current_show_song(payload)
        if current_show_artist and current_show_title:
            artist = current_show_artist
            title = current_show_title

        has_list_blocks = bool(re.search(r"<li\b", payload, flags=re.IGNORECASE))
        if not (artist and title):
            scored_candidates = self._extract_html_song_candidates(payload)
            if scored_candidates:
                scored_candidates.sort(reverse=True, key=lambda item: item[0])
                best_score, artist, title, age_minutes = scored_candidates[0]
                if best_score < 10:
                    return None
            elif has_list_blocks:
                # A structured list was present, but no valid "current" item survived filtering.
                return None
            else:
                age_minutes = None
                artist = self._extract_html_class_value(payload, HTML_ARTIST_CLASS_KEYS)
                title = self._extract_html_class_value(payload, HTML_TITLE_CLASS_KEYS)

                if title and not artist:
                    split_artist, split_title = self._split_compound_title(title)
                    if split_artist and split_title:
                        artist = split_artist
                        title = split_title

        title = title.strip()
        artist = artist.strip()
        if not is_valid_song_candidate(artist, title):
            return None

        stream_title = f"{artist} - {title}".strip(" -")
        return SongInfo(
            stream_title=stream_title,
            raw_metadata=payload,
            artist=artist,
            title=title,
            age_minutes=age_minutes,
            source_kind="web_feed_html",
            source_url=source_url,
        )

    def _extract_current_show_song(self, payload: str) -> tuple[str, str]:
        match = re.search(
            r"<div[^>]*class=[\"'][^\"']*\bcurrentShow\b[^\"']*[\"'][^>]*>(?P<value>.*?)</div>",
            payload,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return "", ""

        text = self._clean_html_text(match.group("value"))
        if not text:
            return "", ""

        text = re.sub(r"^(jetzt\s+l[aä]uft\s*:?)\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"^(gerade\s+l[aä]uft\s*:?)\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"^(now\s+playing\s*:?)\s*", "", text, flags=re.IGNORECASE).strip()
        if not text:
            return "", ""

        for separator in (" von ", " by "):
            lowered = text.lower()
            idx = lowered.rfind(separator.lower())
            if idx <= 0:
                continue
            title = text[:idx].strip(" -:|")
            artist = text[idx + len(separator):].strip(" -:|")
            if title and artist and len(title) >= 2 and len(artist) >= 2:
                return artist, title

        return "", ""

    def _extract_html_song_candidates(self, payload: str) -> list[tuple[int, str, str, int | None]]:
        li_blocks = re.findall(r"<li\b[^>]*>.*?</li>", payload, flags=re.IGNORECASE | re.DOTALL)
        table_blocks = re.findall(r"<tr\b[^>]*>.*?</tr>", payload, flags=re.IGNORECASE | re.DOTALL)
        blocks = []
        for block in li_blocks:
            lower_block = block.lower()
            class_haystack = " ".join(re.findall(r'class=["\']([^"\']+)["\']', block, flags=re.IGNORECASE)).lower()
            has_list_hint = any(
                hint in class_haystack
                for hint in (
                    "playlist",
                    "song",
                    "track",
                    "js_title",
                    "js_artist",
                    "interpret",
                    "performer",
                    "current",
                    "now",
                    "onair",
                )
            )
            has_time_hint = "datetime=" in lower_block or "<time" in lower_block
            if not (has_list_hint or has_time_hint):
                continue
            blocks.append(block)
        blocks.extend(table_blocks)
        if not blocks:
            return []

        candidates: list[tuple[int, str, str, int | None]] = []
        for block in blocks:
            artist = self._extract_html_class_value(block, HTML_ARTIST_CLASS_KEYS).strip()
            title = self._extract_html_class_value(block, HTML_TITLE_CLASS_KEYS).strip()
            if not (artist and title):
                strong_artist, strong_title = self._extract_html_strong_pair(block)
                if not artist and strong_artist:
                    artist = strong_artist
                if not title and strong_title:
                    title = strong_title
            if not (artist and title):
                row_artist, row_title = self._extract_html_table_row_pair(block)
                if not artist and row_artist:
                    artist = row_artist
                if not title and row_title:
                    title = row_title
            if title and not artist:
                split_artist, split_title = self._split_compound_title(title)
                if split_artist and split_title:
                    artist = split_artist
                    title = split_title
            if not artist and not title:
                continue
            if self._looks_like_html_header_value(title, artist):
                continue
            if not is_valid_song_candidate(artist, title):
                continue

            score = 0
            if artist:
                score += 8
            if title:
                score += 10

            lower_block = block.lower()
            if "comingup" in lower_block or "coming-up" in lower_block or "es folgt" in lower_block:
                continue

            time_text = self._extract_html_datetime(block)
            age_minutes = self._age_minutes(time_text)
            if age_minutes is not None:
                if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                    continue
                elif age_minutes >= 0:
                    score += max(0, MAX_NOWPLAYING_AGE_MINUTES - age_minutes)
                else:
                    score += 5

            candidates.append((score, artist, title, age_minutes))

        return candidates

    def _extract_html_strong_pair(self, payload: str) -> tuple[str, str]:
        lower_payload = payload.lower()
        if not any(hint in lower_payload for hint in ("playlist", "song", "track", "now", "onair", "current")):
            return "", ""

        connector_match = re.search(
            r"<strong[^>]*>(?P<artist>.*?)</strong>\s*(?:mit|with|by|von)\s*<strong[^>]*>(?P<title>.*?)</strong>",
            payload,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if connector_match:
            artist = self._clean_html_text(connector_match.group("artist"))
            title = self._clean_html_text(connector_match.group("title"))
            if len(artist) >= 2 and len(title) >= 2:
                return artist, title

        return "", ""

    def _extract_html_table_row_pair(self, payload: str) -> tuple[str, str]:
        cells_raw = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", payload, flags=re.IGNORECASE | re.DOTALL)
        if len(cells_raw) < 2:
            return "", ""

        for cell_raw in cells_raw:
            tagged_pair = re.search(
                r"<b[^>]*>(?P<artist>.*?)</b>\s*(?:<br\s*/?>\s*)+(?P<title>.*?)\s*$",
                cell_raw,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not tagged_pair:
                continue
            artist = self._clean_html_text(tagged_pair.group("artist")).strip()
            title = self._clean_html_text(tagged_pair.group("title")).strip()
            if artist and title:
                return artist, title

        cells = [self._clean_html_text(cell) for cell in cells_raw]
        cells = [cell for cell in cells if cell]
        if len(cells) < 2:
            return "", ""

        if len(cells) >= 3:
            title = cells[-2].strip()
            artist = cells[-1].strip()
        else:
            title = cells[0].strip()
            artist = cells[1].strip()

        if not title and not artist:
            return "", ""
        return artist, title

    def _unwrap_jsonp_payload(self, payload: str) -> str:
        text = str(payload or "").strip()
        if not text:
            return ""
        match = JSONP_WRAPPER_RE.match(text)
        if not match:
            return text
        return (match.group("payload") or "").strip()

    def _looks_like_html_header_value(self, title: str, artist: str) -> bool:
        title_lower = (title or "").strip().lower()
        artist_lower = (artist or "").strip().lower()
        if not title_lower and not artist_lower:
            return True

        headers = {
            "datum",
            "datum und uhrzeit",
            "uhrzeit",
            "coverbild",
            "cover",
            "titel",
            "title",
            "song",
            "track",
            "interpret",
            "artist",
            "performer",
            "komponist",
            "composer",
        }
        return title_lower in headers and artist_lower in headers

    def _generate_html_nowplaying_variants(self, url: str) -> set[str]:
        variants = set()
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return variants

        path_lower = parsed.path.lower()
        if "/nowonair/" in path_lower:
            base_dir = parsed.path.rsplit("/", 1)[0]
            for filename in ("now_on_air.html", "songs.html", "songs.htm"):
                variants.add(urlunparse(parsed._replace(path=f"{base_dir}/{filename}", query="")))
            variants.add(
                urlunparse(
                    parsed._replace(
                        path="/livestream/index.htm/SSI=true/box=2/module=livestream%21middleColumnList.html",
                        query="",
                    )
                )
            )

        if "/zeitstrahl/" in path_lower and "nowonair" in path_lower:
            prefix = parsed.path.split("/zeitstrahl/", 1)[0]
            for filename in ("now_on_air.html", "songs.html", "songs.htm"):
                variants.add(urlunparse(parsed._replace(path=f"{prefix}/nowonair/{filename}", query="")))

        variants.discard(url)
        return variants

    def _dedupe_url_variants(self, candidates: set[str]) -> set[str]:
        grouped: dict[str, list[str]] = {}
        for url in candidates:
            key = self._url_variant_key(url)
            grouped.setdefault(key, []).append(url)

        deduped = set()
        for variants in grouped.values():
            best = max(variants, key=self._url_variant_priority)
            deduped.add(best)
        return deduped

    def _prefer_ctrl_api_timestamped_candidates(self, candidates: set[str]) -> set[str]:
        timestamped_groups = set()
        for url in candidates:
            if not self._is_ctrl_api_playlist_candidate(url):
                continue
            signature = self._ctrl_api_candidate_signature(url)
            if signature:
                timestamped_groups.add(signature)

        if not timestamped_groups:
            return candidates

        filtered = set()
        suppressed_count = 0
        for url in candidates:
            if self._is_ctrl_api_snapshot_candidate(url):
                signature = self._ctrl_api_candidate_signature(url)
                if signature and signature in timestamped_groups:
                    suppressed_count += 1
                    continue
            filtered.add(url)

        if suppressed_count:
            self._log(
                "Ctrl-API Snapshot-Kandidaten verdrängt: "
                f"{suppressed_count} (timestamped siblings={len(timestamped_groups)})"
            )
        return filtered

    def _ctrl_api_candidate_signature(self, url: str) -> tuple[str, str] | None:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.lower()
        if not host or "/ctrl-api/" not in path:
            return None
        if not (path.endswith("/getcurrentsong") or path.endswith("/getplaylist")):
            return None

        query_params = {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
        stream_key = ""
        for key_name in ("k", "skey", "streamkey", "channelkey", "key"):
            stream_key = str(query_params.get(key_name) or "").strip().lower()
            if stream_key:
                break
        if not stream_key:
            return None
        return (host, stream_key)

    def _is_ctrl_api_playlist_candidate(self, url: str) -> bool:
        lower = str(url or "").strip().lower()
        return "ctrl-api/getplaylist" in lower

    def _is_ctrl_api_snapshot_candidate(self, url: str) -> bool:
        lower = str(url or "").strip().lower()
        return "ctrl-api/getcurrentsong" in lower

    def _url_variant_key(self, url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.lower()
        query = parsed.query.lower()
        return f"{host}|{path}|{query}"

    def _url_variant_priority(self, url: str) -> int:
        parsed = urlparse(url)
        score = 0
        if parsed.scheme.lower() == "https":
            score += 20
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            score += 10
        return score

    def _candidate_domain_preference(
        self,
        url: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> int:
        score = 0
        candidate_base = get_base_domain(url)
        if not candidate_base:
            return score

        station_base = get_base_domain(station.homepage) if station and station.homepage else ""
        if station_base and candidate_base == station_base:
            score += 80

        stream_base = get_base_domain(resolved.resolved_url)
        if stream_base and candidate_base == stream_base:
            score += 20

        input_base = get_base_domain(resolved.input_url)
        if input_base and candidate_base == input_base:
            score += 15

        if self._is_bcs_current_candidate(url):
            if self._bcs_station_selector_matches_context(url, resolved, station):
                score += 120
            else:
                score -= 120

        return score

    def _candidate_matches_input_context(
        self,
        url: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> bool:
        lower_url = (url or "").lower()
        parsed = urlparse(lower_url)
        candidate_base = get_base_domain(url)
        station_base = get_base_domain(station.homepage) if station and station.homepage else ""
        stream_bases = {
            base
            for base in (
                get_base_domain(resolved.resolved_url),
                get_base_domain(resolved.delivery_url),
                get_base_domain(resolved.input_url),
            )
            if base
        }
        same_station_domain = bool(candidate_base and station_base and candidate_base == station_base)
        same_stream_domain = bool(candidate_base and candidate_base in stream_bases)
        if parsed.netloc == "brradio.br.de" and parsed.path == "/radio/v4":
            return True
        if self._is_bcs_current_candidate(url):
            return self._bcs_station_selector_matches_context(url, resolved, station)
        if self._ctrl_api_candidate_signature(url):
            # ctrl-api feeds are keyed provider APIs; they often live on a shared host
            # without station-identifying path tokens, so domain/token overlap is not
            # a reliable context signal here.
            return True
        is_radiomodul = "radiomodul" in lower_url
        is_playlist_like = any(
            token in lower_url
            for token in (
                "playlist",
                "titelsuche",
                "now_on_air",
                "nowonair",
                "middlecolumnlist",
                "livestream",
            )
        )
        is_channelized_api = "/webradio/" in lower_url and ("/current/" in lower_url or "/playlist/" in lower_url)
        source_type = str((station.raw_record or {}).get("source") or "").strip().lower() if station else ""
        is_fallback_station = source_type == "channel_page_fallback"
        is_web_directory_fallback = source_type == "web_directory_fallback"

        station_name_tokens = self._tokenize_station_name_context_tokens(station.name if station else "")
        station_name_token_set = set(station_name_tokens)
        combined_station_token = "".join(station_name_tokens) if len(station_name_tokens) >= 2 else ""

        if not (is_radiomodul or is_playlist_like or is_channelized_api):
            return True

        candidate_tokens = self._tokenize_context_tokens(url)

        if is_playlist_like and len(station_name_token_set) >= 2:
            station_name_overlap = station_name_token_set & candidate_tokens
            if len(station_name_overlap) >= 2:
                pass
            elif combined_station_token and combined_station_token in candidate_tokens:
                pass
            elif same_station_domain or same_stream_domain:
                pass
            else:
                return False

        if (is_playlist_like or is_channelized_api) and is_web_directory_fallback:
            raw_slug = str((station.raw_record or {}).get("slug") or "").strip().lower() if station else ""
            if raw_slug:
                compact_candidate = re.sub(r"[^a-z0-9]", "", lower_url)
                compact_slug = re.sub(r"[^a-z0-9]", "", raw_slug)
                brand_token = station_name_tokens[0] if station_name_tokens else ""
                compact_slug_tail = compact_slug
                if brand_token and compact_slug.startswith(brand_token):
                    compact_slug_tail = compact_slug[len(brand_token) :]
                tail_tokens = station_name_tokens[1:] if len(station_name_tokens) > 1 else []

                slug_tail_matches = bool(compact_slug_tail and compact_slug_tail in compact_candidate)
                token_tail_matches = bool(tail_tokens) and all(token in candidate_tokens for token in tail_tokens)
                if compact_slug and compact_slug in compact_candidate:
                    pass
                elif slug_tail_matches:
                    pass
                elif token_tail_matches:
                    pass
                else:
                    return False

        query_tokens = set()
        if station and station.name:
            query_tokens.update(self._tokenize_context_tokens(station.name))
        if station and station.stream_url:
            query_tokens.update(self._tokenize_context_tokens(station.stream_url))
        if not query_tokens:
            query_tokens = self._tokenize_context_tokens(resolved.input_url)
        if len(query_tokens) < 2:
            return True

        common_tokens = query_tokens & candidate_tokens
        if is_radiomodul or is_fallback_station:
            min_overlap = 2 if len(query_tokens) < 3 else 3
        elif is_playlist_like:
            mixed_query_tokens = {token for token in query_tokens if is_mixed_alnum_token(token)}
            if mixed_query_tokens:
                mixed_candidate_tokens = {token for token in candidate_tokens if is_mixed_alnum_token(token)}
                if mixed_candidate_tokens and not (mixed_query_tokens & mixed_candidate_tokens):
                    return False
            min_overlap = 1
        else:
            min_overlap = 1
        return len(common_tokens) >= min_overlap

    def _tokenize_context_tokens(self, value: str) -> set[str]:
        raw_tokens = split_search_tokens(value)
        if not raw_tokens:
            return set()

        tokens = set()
        for token in raw_tokens:
            if token in NOWPLAYING_QUERY_CONTEXT_IGNORE_TOKENS:
                continue
            if len(token) < 3 and not is_mixed_alnum_token(token):
                continue
            if token.isdigit():
                continue
            tokens.add(token)
        return tokens

    def _tokenize_station_name_context_tokens(self, station_name: str) -> list[str]:
        raw_tokens = split_search_tokens(station_name)
        if not raw_tokens:
            return []

        tokens = []
        seen = set()
        for token in raw_tokens:
            if token in NOWPLAYING_QUERY_CONTEXT_IGNORE_TOKENS:
                continue
            if token in STATION_LOOKUP_OPTIONAL_PREFIX_TOKENS:
                continue
            if len(token) < 3 and not is_mixed_alnum_token(token):
                continue
            if token.isdigit():
                continue
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    def _extract_html_datetime(self, payload: str) -> str:
        match = re.search(r'datetime=["\']([^"\']+)["\']', payload, flags=re.IGNORECASE)
        if match:
            return (match.group(1) or "").strip()

        clean = self._clean_html_text(payload)
        datetime_match = re.search(
            r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\s*,?\s*(?P<hour>\d{1,2})[.:](?P<minute>\d{2})\s*(?:uhr)?",
            clean,
            flags=re.IGNORECASE,
        )
        if datetime_match:
            day = int(datetime_match.group("day"))
            month = int(datetime_match.group("month"))
            year = int(datetime_match.group("year"))
            hour = int(datetime_match.group("hour"))
            minute = int(datetime_match.group("minute"))
            return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:00"

        time_match = re.search(r"(?<!\d)(?P<hour>\d{1,2})[.:](?P<minute>\d{2})\s*(?:uhr)?", clean, flags=re.IGNORECASE)
        if time_match:
            hour = int(time_match.group("hour"))
            minute = int(time_match.group("minute"))
            now = datetime.now()
            return f"{now.year:04d}-{now.month:02d}-{now.day:02d} {hour:02d}:{minute:02d}:00"

        return ""

    def _extract_html_class_value(self, payload: str, class_keys: tuple[str, ...]) -> str:
        for class_key in class_keys:
            pattern = (
                r"<(?P<tag>[a-z0-9]+)[^>]*class=[\"'][^\"']*\b"
                + re.escape(class_key)
                + r"\b[^\"']*[\"'][^>]*>(?P<value>.*?)</(?P=tag)>"
            )
            match = re.search(pattern, payload, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue

            clean = self._clean_html_text(match.group("value"))
            if clean:
                return clean
        return ""

    def _clean_html_text(self, value: str) -> str:
        text = html.unescape(value or "")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return repair_mojibake_text(text.strip())

    def _walk_json_objects(self, value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_json_objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json_objects(child)

    def _extract_json_value(self, node: dict, keyset: set[str]) -> str:
        normalized_keyset = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in keyset}
        for key, value in node.items():
            key_norm = re.sub(r"[^a-z0-9]", "", key.lower())
            if key_norm not in normalized_keyset:
                continue
            if isinstance(value, str):
                return repair_mojibake_text(value.strip())
            if isinstance(value, (int, float)):
                return str(value)
        return ""

    def _json_status_score(self, node: dict) -> int:
        for key, value in node.items():
            key_lower = key.lower()
            if key_lower not in STATUS_KEYS:
                continue
            value_lower = str(value).strip().lower()
            if value_lower in {"now", "current", "onair", "live", "true", "1"}:
                return 100
            if value_lower in {"next", "upcoming"}:
                return 15
        return 0

    def _json_playing_mode_score(self, node: dict) -> int:
        value = self._extract_json_value(node, {"playingmode", "playing_mode", "playmode", "play_mode"})
        normalized = (value or "").strip().lower()
        if not normalized:
            return 0
        if normalized in {"1", "current", "now", "live", "onair", "playing"}:
            return 180
        if normalized in {"2", "previous", "last", "history"}:
            return -60
        if normalized in {"0", "next", "upcoming", "future", "queued"}:
            return -90
        return 0

    def _json_time_window_score(self, time_text: str, duration_text: str) -> int:
        if self._is_duration_window_active(time_text, duration_text):
            return 260
        if self._is_duration_window_expired(time_text, duration_text):
            return -220
        if time_text and duration_text:
            return -40
        return 0

    def _split_compound_title(self, value: str) -> tuple[str, str]:
        clean = (value or "").strip()
        if " - " not in clean:
            return "", clean

        parts = [part.strip() for part in clean.split(" - ") if part.strip()]
        if len(parts) < 2:
            return "", clean

        artist = " - ".join(parts[:-1]).strip()
        title = parts[-1].strip()
        if len(artist) < 2 or len(title) < 2:
            return "", clean
        return artist, title

    def _strip_xml_ns(self, tag: str) -> str:
        if "}" in tag:
            return tag.split("}", 1)[1]
        return tag

    def _age_minutes(self, value: str) -> int | None:
        parsed = self._parse_datetime(value)
        if not parsed:
            return None
        if parsed.tzinfo is None:
            delta = datetime.now() - parsed
        else:
            delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        return int(delta.total_seconds() // 60)

    def _parse_datetime(self, value: str) -> datetime | None:
        text = (value or "").strip()
        if not text:
            return None

        if re.fullmatch(r"\d{10,13}", text):
            raw = int(text)
            if len(text) == 13:
                raw = raw // 1000
            try:
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None

        local_match = re.match(
            r"^(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\s*,?\s*(?P<hour>\d{1,2})[.:](?P<minute>\d{2})(?::(?P<second>\d{2}))?\s*(?:uhr)?$",
            text,
            flags=re.IGNORECASE,
        )
        if local_match:
            second = int(local_match.group("second") or 0)
            return datetime(
                int(local_match.group("year")),
                int(local_match.group("month")),
                int(local_match.group("day")),
                int(local_match.group("hour")),
                int(local_match.group("minute")),
                second,
            )

        time_only_match = re.match(
            r"^(?P<hour>\d{1,2})[.:](?P<minute>\d{2})(?::(?P<second>\d{2}))?\s*(?:uhr)?$",
            text,
            flags=re.IGNORECASE,
        )
        if time_only_match:
            second = int(time_only_match.group("second") or 0)
            now = datetime.now()
            return datetime(
                now.year,
                now.month,
                now.day,
                int(time_only_match.group("hour")),
                int(time_only_match.group("minute")),
                second,
            )

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                if fmt.endswith("Z"):
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                continue

        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _build_document_index(
        self,
        documents: list[tuple[str, str]],
    ) -> list[tuple[str, str, list[str]]]:
        indexed_documents = []
        for doc_url, text in documents:
            extracted_urls = self._extract_urls_from_document(text, doc_url) if text else []
            indexed_documents.append((doc_url, text, extracted_urls))
        return indexed_documents

    def _build_generated_candidates(
        self,
        documents: list[tuple[str, str, list[str]]],
        known_candidates: set[str],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        if not documents:
            return set()

        api_bases = set()
        for url in known_candidates:
            lower = url.lower()
            if any(token in lower for token in ("currentsong", "getplaylist", "metadata/channel/", "nowplaying")):
                api_bases.add(url)

        for _, text, extracted_urls in documents:
            for extracted in extracted_urls:
                lower = extracted.lower()
                if any(token in lower for token in ("currentsong", "getplaylist", "metadata/channel/", "nowplaying")):
                    api_bases.add(extracted)

            for extracted in self._extract_embedded_feed_urls(text):
                lower = extracted.lower()
                if any(token in lower for token in ("currentsong", "getplaylist", "metadata/channel/", "nowplaying")):
                    api_bases.add(extracted)

            for host_fragment in re.findall(
                r"(?:https?://)?api\.streamabc\.net/metadata/channel/",
                text,
                flags=re.IGNORECASE,
            ):
                clean = host_fragment
                if not clean.lower().startswith("http"):
                    clean = f"https://{clean.lstrip('/')}"
                api_bases.add(clean)

        stream_keys = self._discover_stream_keys(documents, resolved, station)
        if stream_keys:
            self._log(f"Stream-Key Kandidaten gefunden: {len(stream_keys)}")

        generated = set()
        for base_url in api_bases:
            cleaned_base = base_url.replace("%24%7B", "${")
            cleaned_base = cleaned_base.replace("`", "").strip()
            cleaned_base = re.sub(r"\$\{[^}]+\}", "", cleaned_base)
            if stream_keys:
                for key in stream_keys[:1]:
                    for generated_url in self._inject_stream_key(cleaned_base, key):
                        for variant_url in self._expand_ctrl_api_feed_variants(generated_url):
                            if self._looks_like_feed_url(variant_url):
                                generated.add(variant_url)
            elif self._looks_like_feed_url(cleaned_base):
                for variant_url in self._expand_ctrl_api_feed_variants(cleaned_base):
                    generated.add(variant_url)

        channel_sources = set(known_candidates)
        for doc_url, text, extracted_urls in documents:
            if doc_url:
                channel_sources.add(doc_url)
            if not text:
                continue
            for extracted in extracted_urls:
                channel_sources.add(extracted)
        for url in self._extract_channel_current_track_urls(channel_sources):
            if self._looks_like_feed_url(url):
                generated.add(url)

        return generated

    def _extract_embedded_feed_urls(self, text: str) -> set[str]:
        if not text:
            return set()

        extracted = set()
        for match in re.findall(
            r"https?:\\\\?/\\\\?/[^\"'\s<>()]+",
            text,
            flags=re.IGNORECASE,
        ):
            normalized = str(match or "").strip()
            if not normalized:
                continue
            normalized = normalized.replace("\\/", "/")
            normalized = normalized.replace("\\u0026", "&")
            normalized = normalized.replace("&amp;", "&")
            if is_probable_url(normalized):
                extracted.add(normalized)
        return extracted

    def _extract_channel_current_track_urls(self, urls: set[str]) -> set[str]:
        candidates = set()
        for raw_url in urls:
            url = str(raw_url or "").strip()
            if not url:
                continue
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                continue
            parts = [part for part in parsed.path.split("/") if part]
            lowered_parts = [part.lower() for part in parts]
            if "channels" not in lowered_parts:
                continue
            idx = lowered_parts.index("channels")
            if idx + 1 >= len(parts):
                continue
            channel_id = parts[idx + 1].strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{2,80}", channel_id):
                continue
            trailing_parts = lowered_parts[idx + 2 :]
            has_stream_like_tail = any(
                ("stream" in part) or ("guide" in part) or ("current-track" in part) or ("current_track" in part)
                for part in trailing_parts
            )
            if not has_stream_like_tail:
                continue

            base = f"{parsed.scheme}://{parsed.netloc}"
            candidates.add(f"{base}/channels/{channel_id}/current-track")

            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            source_host = str(query.get("source") or "").strip()
            if source_host:
                source_host = re.sub(r"^https?://", "", source_host, flags=re.IGNORECASE).strip("/")
                if re.fullmatch(r"[A-Za-z0-9.-]+", source_host) and "." in source_host:
                    candidates.add(f"https://{source_host}/channels/{channel_id}/current-track")

        return candidates

    def _discover_graphql_track_feed_urls(
        self,
        documents: list[tuple[str, str, list[str]]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        endpoints = self._extract_graphql_track_endpoints(documents)
        if not endpoints:
            return set()

        feeds = set()
        ordered_endpoints = sorted(endpoints)
        worker_count = max(1, min(self._crawl_max_workers, len(ordered_endpoints)))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(self._fetch_graphql_track_feed_urls_for_endpoint, endpoint, resolved, station): endpoint
                for endpoint in ordered_endpoints
            }
            for future in as_completed(future_map):
                try:
                    feeds.update(future.result())
                except Exception:
                    continue
        return feeds

    def _extract_graphql_track_endpoints(
        self,
        documents: list[tuple[str, str, list[str]]],
    ) -> set[str]:
        endpoints = set()
        for _, text, _ in documents:
            if not text:
                continue
            lowered = text.lower()
            if "streambyid" not in lowered or "taxonomytermlist" not in lowered:
                continue
            for match in GRAPHQL_ENDPOINT_RE.findall(text):
                endpoints.add(match)
        return endpoints

    def _fetch_graphql_track_feed_urls_for_endpoint(
        self,
        endpoint: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        catalog = self._fetch_graphql_stream_catalog(endpoint)
        if not catalog:
            return set()

        feeds = set()
        for entry in self._match_graphql_stream_catalog_entries(catalog, resolved, station):
            stream_id = str(entry.get("id") or "").strip()
            if stream_id:
                feeds.add(self._build_graphql_tracks_candidate_url(endpoint, stream_id))
        return feeds

    def _fetch_graphql_stream_catalog(self, endpoint: str) -> list[dict[str, str]]:
        cached = self._graphql_stream_catalog_cache.get(endpoint)
        if cached is not None:
            return list(cached)

        payload = self._post_graphql_json(endpoint, GRAPHQL_STREAMS_QUERY)
        root = payload.get("data") if isinstance(payload, dict) else None
        taxonomy = root.get("taxonomyTermList") if isinstance(root, dict) else None
        items = taxonomy.get("items") if isinstance(taxonomy, dict) else None
        if not isinstance(items, list):
            self._graphql_stream_catalog_cache[endpoint] = []
            return []

        catalog = []
        for item in items:
            if not isinstance(item, dict):
                continue
            stream_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            field_link = item.get("fieldLink")
            url_root = field_link.get("url") if isinstance(field_link, dict) else None
            stream_url = str(url_root.get("path") or "").strip() if isinstance(url_root, dict) else ""
            if stream_id and stream_url:
                catalog.append({"id": stream_id, "label": label, "url": stream_url})

        self._graphql_stream_catalog_cache[endpoint] = list(catalog)
        return catalog

    def _match_graphql_stream_catalog_entries(
        self,
        catalog: list[dict[str, str]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> list[dict[str, str]]:
        comparable_urls = [
            normalized
            for normalized in (
                self._normalize_stream_match_url(url)
                for url in (
                    resolved.input_url,
                    resolved.resolved_url,
                    resolved.delivery_url,
                    station.stream_url if station else "",
                )
            )
            if normalized
        ]
        station_hints = build_station_hints((station.name if station else "", resolved.station_name or ""))
        compact_hints = {compact_station_compare_text(hint) for hint in station_hints if compact_station_compare_text(hint)}

        exact_matches = []
        label_matches = []
        for entry in catalog:
            entry_url = self._normalize_stream_match_url(entry.get("url") or "")
            if entry_url and any(
                self._stream_match_urls_compatible(entry_url, comparable_url)
                for comparable_url in comparable_urls
            ):
                exact_matches.append(entry)
                continue

            label_compact = compact_station_compare_text(entry.get("label") or "")
            path_compact = compact_station_compare_text(urlparse(entry.get("url") or "").path)
            if any(hint and (hint in label_compact or hint in path_compact) for hint in compact_hints):
                label_matches.append(entry)

        return exact_matches or label_matches[:2]

    def _normalize_stream_match_url(self, url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        if not parsed.netloc:
            return ""
        path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
        return f"{parsed.netloc.lower()}{path.lower()}"

    def _stream_match_urls_compatible(self, left: str, right: str) -> bool:
        left = str(left or "").strip().lower().strip("/")
        right = str(right or "").strip().lower().strip("/")
        if not left or not right:
            return False
        if left == right:
            return True

        left_host, _, left_path = left.partition("/")
        right_host, _, right_path = right.partition("/")
        if left_host != right_host:
            return False

        left_parts = [part for part in left_path.split("/") if part]
        right_parts = [part for part in right_path.split("/") if part]
        if not left_parts or not right_parts:
            return False

        shorter, longer = (left_parts, right_parts) if len(left_parts) <= len(right_parts) else (right_parts, left_parts)
        return shorter == longer[: len(shorter)]

    def _build_graphql_tracks_candidate_url(self, endpoint: str, stream_id: str) -> str:
        parsed = urlparse(endpoint)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query[GRAPHQL_TRACKS_MODE_PARAM] = GRAPHQL_TRACKS_MODE_VALUE
        query[GRAPHQL_TRACKS_ID_PARAM] = str(stream_id)
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _is_graphql_tracks_candidate(self, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        return query.get(GRAPHQL_TRACKS_MODE_PARAM) == GRAPHQL_TRACKS_MODE_VALUE and bool(query.get(GRAPHQL_TRACKS_ID_PARAM))

    def _probe_graphql_tracks_candidate(self, url: str) -> SongInfo | None:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        stream_id = str(query.get(GRAPHQL_TRACKS_ID_PARAM) or "").strip()
        if not stream_id:
            return None
        endpoint = urlunparse(parsed._replace(query=""))
        payload = self._post_graphql_json(endpoint, GRAPHQL_TRACKS_QUERY, variables={"id": stream_id})
        if not payload:
            return None
        payload_text = json.dumps(payload)
        return self._parse_graphql_tracks_payload(payload, payload_text, url)

    def _post_graphql_json(
        self,
        endpoint: str,
        query_text: str,
        variables: dict | None = None,
        *,
        context=None,
    ) -> dict | list | None:
        body = {"query": query_text}
        if variables:
            body["variables"] = variables
        request = Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=DISCOVERY_REQUEST_TIMEOUT_SECONDS, context=context) as response:
                payload = decode_text_bytes(
                    response.read(DISCOVERY_READ_BYTES),
                    content_type=response.headers.get("Content-Type") or "",
                )
        except URLError as err:
            if context is None and isinstance(err.reason, ssl.SSLCertVerificationError):
                insecure_context = ssl._create_unverified_context()
                return self._post_graphql_json(endpoint, query_text, variables, context=insecure_context)
            return None
        except Exception:
            return None

        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    def _discover_official_player_feed_urls(
        self,
        documents: list[tuple[str, str, list[str]]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        config_urls = self._extract_official_player_config_urls(documents)
        if not config_urls:
            return set()

        self._log(f"Official-Player-Konfigurationen gefunden: {len(config_urls)}")
        feeds = set()
        followup_urls: list[str] = []
        seen_followups = set()
        for config_url in sorted(config_urls):
            payload_text, content_type = self._fetch_text(config_url)
            if not payload_text:
                continue
            if not self._is_json_candidate(config_url, content_type, payload_text):
                continue
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            for entry in self._extract_official_player_entries(payload, config_url, resolved, station):
                for feed_url in entry.feed_urls:
                    for variant_url in self._expand_feed_format_variants(feed_url):
                        if is_probable_url(variant_url):
                            feeds.add(variant_url)
                for follow_url in entry.follow_urls:
                    if not is_probable_url(follow_url):
                        continue
                    if follow_url in seen_followups:
                        continue
                    seen_followups.add(follow_url)
                    followup_urls.append(follow_url)

        if followup_urls:
            budget = max(0, int(DISCOVERY_OFFICIAL_PLAYER_FOLLOWUP_BUDGET))
            if budget > 0:
                planned_followups = followup_urls[:budget]
                self._log(f"Official-Player-Folgedokumente geplant: {len(planned_followups)}")
                for doc_url, doc_text in self._fetch_documents_parallel(planned_followups):
                    for extracted_url in self._extract_urls_from_document(doc_text, doc_url):
                        if self._looks_like_feed_url(extracted_url):
                            for variant_url in self._expand_feed_format_variants(extracted_url):
                                feeds.add(variant_url)

        if feeds:
            self._log(f"Official-Player-Feeds gefunden: {len(feeds)}")

        return feeds

    def _extract_official_player_config_urls(self, documents: list[tuple[str, str, list[str]]]) -> set[str]:
        config_urls = set(self._extract_official_config_urls(documents))
        for doc_url, _, extracted_urls in documents:
            for candidate_url in [doc_url, *extracted_urls]:
                if self._looks_like_official_player_config_url(candidate_url):
                    config_urls.add(candidate_url)
        return config_urls

    def _extract_official_player_entries(
        self,
        payload: dict,
        config_url: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> list[OfficialPlayerEntry]:
        station_name = (station.name if station else "") or resolved.station_name or ""
        input_label = (resolved.input_url or "").strip()
        parsed_config = urlparse(config_url)
        config_base = f"{parsed_config.scheme}://{parsed_config.netloc}" if parsed_config.scheme and parsed_config.netloc else ""
        scored_entries: list[OfficialPlayerEntry] = []

        for schema_name, node in (
            ("channels", payload.get("channels")),
            ("streams", payload.get("streams")),
        ):
            items: list[dict] = []
            if isinstance(node, dict):
                items = [value for value in node.values() if isinstance(value, dict)]
            elif isinstance(node, list):
                items = [value for value in node if isinstance(value, dict)]
            if not items:
                continue

            for item in items:
                entry = self._build_official_player_entry(
                    schema_name=schema_name,
                    item=item,
                    config_base=config_base,
                    resolved=resolved,
                    station_name=station_name,
                    input_label=input_label,
                )
                if entry:
                    scored_entries.append(entry)

        if not scored_entries:
            return []

        scored_entries.sort(key=lambda entry: entry.score, reverse=True)
        best_score = scored_entries[0].score
        entry_limit = max(1, int(DISCOVERY_OFFICIAL_PLAYER_ENTRY_LIMIT))
        return [entry for entry in scored_entries if entry.score == best_score][:entry_limit]

    def _build_official_player_entry(
        self,
        schema_name: str,
        item: dict,
        config_base: str,
        resolved: ResolvedStream,
        station_name: str,
        input_label: str,
    ) -> OfficialPlayerEntry | None:
        names = self._extract_official_player_entry_names(item)
        feed_urls = self._extract_official_player_entry_urls(
            item,
            config_base,
            (
                {"currenturl", "current_url", "nowplayingurl", "now_playing_url", "feedurl", "feed_url"},
                {"playlisturl", "playlist_url", "historyurl", "history_url"},
            ),
        )
        follow_urls: list[str] = []
        stream_url = ""

        url_candidates = self._extract_official_player_entry_urls(
            item,
            config_base,
            (
                {"url", "pageurl", "page_url", "htmlurl", "html_url", "documenturl", "document_url"},
                {"configurl", "config_url", "playerurl", "player_url"},
                {"streamurl", "stream_url", "audiourl", "audio_url", "mount"},
            ),
        )
        for candidate_url in url_candidates:
            if self._looks_like_stream_endpoint(candidate_url):
                if not stream_url:
                    stream_url = candidate_url
                continue
            if candidate_url not in follow_urls:
                follow_urls.append(candidate_url)

        if schema_name == "channels" and not stream_url:
            direct_stream = self._extract_json_value(item, {"url"})
            direct_stream = self._absolutize_official_player_url(direct_stream, config_base)
            if direct_stream and self._looks_like_stream_endpoint(direct_stream):
                stream_url = direct_stream

        score = 0
        if feed_urls:
            score += 40
        if follow_urls:
            score += 15
        if stream_url and self._stream_url_matches(stream_url, resolved.resolved_url):
            score += 120
        if stream_url and resolved.delivery_url and self._stream_url_matches(stream_url, resolved.delivery_url):
            score += 40
        if any(self._station_name_matches(name, station_name) for name in names if name and station_name):
            score += 90
        if any(self._station_name_matches(name, input_label) for name in names if name and input_label):
            score += 70

        if score <= 0:
            return None
        return OfficialPlayerEntry(score=score, feed_urls=feed_urls, follow_urls=follow_urls)

    def _extract_official_player_entry_names(self, item: dict) -> list[str]:
        names: list[str] = []
        seen = set()
        for keyset in (
            {"title", "label", "channel", "stationname"},
            {"id", "slug", "name"},
        ):
            value = self._extract_json_value(item, keyset)
            if not value:
                continue
            if value in seen:
                continue
            seen.add(value)
            names.append(value)
        return names

    def _extract_official_player_entry_urls(
        self,
        item: dict,
        config_base: str,
        keysets: tuple[set[str], ...],
    ) -> list[str]:
        urls: list[str] = []
        seen = set()
        for keyset in keysets:
            value = self._extract_json_value(item, keyset)
            absolute = self._absolutize_official_player_url(value, config_base)
            if not absolute:
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            urls.append(absolute)
        return urls

    def _absolutize_official_player_url(self, value: str, config_base: str) -> str:
        if not value:
            return ""
        if is_probable_url(value):
            return value
        if not config_base:
            return ""
        absolute = urljoin(config_base, value)
        if not is_probable_url(absolute):
            return ""
        return absolute

    def _expand_feed_format_variants(self, url: str) -> list[str]:
        normalized = str(url or "").strip()
        if not normalized:
            return []

        variants: list[str] = []
        seen = set()

        def _remember(candidate: str) -> None:
            candidate = str(candidate or "").strip()
            if not candidate or candidate in seen:
                return
            if not is_probable_url(candidate):
                return
            seen.add(candidate)
            variants.append(candidate)

        _remember(normalized)
        parsed = urlparse(normalized)
        path = parsed.path
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        lower_path = path.lower()

        if "avcustom" not in lower_path:
            if lower_path.endswith(".xml"):
                _remember(urlunparse(parsed._replace(path=re.sub(r"\.xml$", ".json", path, flags=re.IGNORECASE))))
            elif lower_path.endswith(".json"):
                _remember(urlunparse(parsed._replace(path=re.sub(r"\.json$", ".xml", path, flags=re.IGNORECASE))))

        output_value = str(query.get("output") or "").strip().lower()
        if output_value == "xml":
            query["output"] = "json"
            _remember(urlunparse(parsed._replace(query=urlencode(query, doseq=True))))
        elif output_value == "json":
            query["output"] = "xml"
            _remember(urlunparse(parsed._replace(query=urlencode(query, doseq=True))))

        return variants

    def _extract_channel_feed_urls(
        self,
        payload: dict,
        config_url: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        feeds = set()
        for entry in self._extract_official_player_entries(payload, config_url, resolved, station):
            for feed_url in entry.feed_urls:
                if is_probable_url(feed_url):
                    feeds.add(feed_url)
        return feeds

    def _discover_playerbar_playlist_urls(
        self,
        documents: list[tuple[str, str, list[str]]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        container_urls = set()
        for doc_url, _, extracted_urls in documents:
            if self._looks_like_playerbar_container_url(doc_url):
                container_urls.add(doc_url)
            for extracted in extracted_urls:
                if self._looks_like_playerbar_container_url(extracted):
                    container_urls.add(extracted)

        if not container_urls:
            return set()

        ordered_containers = sorted(
            container_urls,
            key=lambda url: (
                self._candidate_matches_input_context(url, resolved, station),
                self._candidate_score(url),
                url,
            ),
            reverse=True,
        )

        feeds = set()
        station_name = (station.name if station else "") or resolved.station_name or ""
        candidate_containers = ordered_containers[: self._playerbar_max_containers]
        if not candidate_containers:
            return feeds

        if len(candidate_containers) == 1 or self._playerbar_max_workers <= 1:
            for container_url in candidate_containers:
                feed_url = self._probe_playerbar_container(container_url, resolved, station_name)
                if not feed_url:
                    continue
                feeds.add(feed_url)
            return feeds

        worker_count = max(1, min(self._playerbar_max_workers, len(candidate_containers)))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(self._probe_playerbar_container, container_url, resolved, station_name): container_url
                for container_url in candidate_containers
            }
            for future in as_completed(future_map):
                try:
                    feed_url = future.result()
                except Exception:
                    feed_url = ""
                if not feed_url:
                    continue
                feeds.add(feed_url)

        return feeds

    def _probe_playerbar_container(
        self,
        container_url: str,
        resolved: ResolvedStream,
        station_name: str,
    ) -> str:
        payload_text, content_type = self._fetch_text(container_url)
        if not payload_text:
            return ""
        if not self._is_json_candidate(container_url, content_type, payload_text):
            return ""
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return ""
        if not isinstance(payload, dict):
            return ""
        if not self._playerbar_container_matches(payload, container_url, resolved, station_name):
            return ""

        playlist = payload.get("playlist")
        if not isinstance(playlist, dict):
            return ""

        feed_url = self._extract_json_value(playlist, {"feedurl", "feed_url", "playlisturl", "playlist_url"})
        if not feed_url:
            return ""
        if not is_probable_url(feed_url):
            feed_url = urljoin(container_url, feed_url)
        if not is_probable_url(feed_url):
            return ""
        if not self._looks_like_feed_url(feed_url):
            return ""
        return feed_url

    def _extract_official_config_urls(self, documents: list[tuple[str, str, list[str]]]) -> set[str]:
        config_urls = set()

        for doc_url, text, extracted_urls in documents:
            if not text:
                continue

            mandates = {
                match.strip().lower()
                for match in re.findall(r'data-mandate=["\']([a-z0-9-]+)["\']', text, flags=re.IGNORECASE)
            }
            if not mandates:
                continue

            script_hosts = set()
            for extracted in extracted_urls:
                parsed = urlparse(extracted)
                if parsed.path.lower().endswith("/build/webradio.js"):
                    scheme = parsed.scheme or "https"
                    script_hosts.add((scheme, parsed.netloc))

            for match in re.findall(
                r"(?:https?:)?//[^\"'\s<>()]+/build/webradio\.js(?:\?[^\"'\s<>()]+)?",
                text,
                flags=re.IGNORECASE,
            ):
                normalized = match
                if normalized.startswith("//"):
                    normalized = "https:" + normalized
                parsed = urlparse(normalized)
                if parsed.netloc:
                    scheme = parsed.scheme or "https"
                    script_hosts.add((scheme, parsed.netloc))

            if not script_hosts:
                parsed_doc = urlparse(doc_url)
                if parsed_doc.scheme and parsed_doc.netloc:
                    script_hosts.add((parsed_doc.scheme, parsed_doc.netloc))

            for mandate in mandates:
                for scheme, netloc in script_hosts:
                    if not netloc:
                        continue
                    config_urls.add(f"{scheme}://{netloc}/webradio/{mandate}/config.json")

        return config_urls

    def _looks_like_official_player_config_url(self, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        path = parsed.path.lower()
        if not parsed.scheme or not parsed.netloc:
            return False
        if not path.endswith(".json"):
            return False
        if "config" not in path:
            return False
        if "/webradio/" in path:
            return True
        if "/radiolivestreams/" in path:
            return True
        if any(token in path for token in ("livestream", "stream", "radio")):
            return True
        return False

    def _looks_like_playerbar_container_url(self, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        path = parsed.path.lower()
        if not parsed.scheme or not parsed.netloc:
            return False
        if not path.endswith(".json"):
            return False
        if "playerbarcontainer" not in path:
            return False
        return "/~webradio/" in path

    def _playerbar_container_matches(
        self,
        payload: dict,
        container_url: str,
        resolved: ResolvedStream,
        station_name: str,
    ) -> bool:
        audioplayer = payload.get("audioplayer")
        saw_stream_source = False
        if isinstance(audioplayer, dict):
            sources = audioplayer.get("sources")
            if isinstance(sources, list):
                for item in sources:
                    if not isinstance(item, dict):
                        continue
                    src = str(item.get("src") or "").strip()
                    if not src:
                        continue
                    saw_stream_source = True
                    if self._stream_url_matches(src, resolved.resolved_url):
                        return True
                    if resolved.delivery_url and self._stream_url_matches(src, resolved.delivery_url):
                        return True
        if saw_stream_source:
            return False

        if not station_name:
            return False

        candidates = [container_url]
        if isinstance(audioplayer, dict):
            candidates.append(str(audioplayer.get("name") or ""))
            candidates.append(str(audioplayer.get("mediaId") or ""))

        show = payload.get("show")
        if isinstance(show, dict):
            show_data = show.get("data")
            if isinstance(show_data, dict):
                candidates.append(str(show_data.get("title") or ""))
                candidates.append(str(show_data.get("stationid") or ""))

        for candidate in candidates:
            if candidate and self._station_name_matches(candidate, station_name):
                return True
        return False

    def _discover_loverad_flow_urls(
        self,
        known_candidates: set[str],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        flow_urls = set()
        station_name = (station.name if station else "") or resolved.station_name or ""

        for candidate_url in sorted(known_candidates):
            parsed = urlparse(candidate_url)
            host = (parsed.netloc or "").lower()
            if host != "top-stream-service.loverad.io":
                continue

            path_parts = [part for part in parsed.path.lower().split("/") if part]
            if len(path_parts) < 2 or path_parts[0] != "v1":
                continue
            mandate = path_parts[1]
            if not re.fullmatch(r"[a-z0-9-]{2,32}", mandate):
                continue

            payload_text, content_type = self._fetch_text(candidate_url)
            if not payload_text:
                continue
            if not self._is_json_candidate(candidate_url, content_type, payload_text):
                continue
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            best_station_id = ""
            best_score = -1
            for node in payload.values():
                if not isinstance(node, dict):
                    continue

                station_id = self._extract_json_value(node, {"station_id", "stationid"})
                if not station_id.isdigit():
                    continue

                stream_url = self._extract_json_value(
                    node,
                    {"url_low", "url_high", "stream_url", "streamurl", "url"},
                )
                channel_name = self._extract_json_value(
                    node,
                    {"stream", "name", "radio_name", "title", "channel"},
                )

                score = 0
                if stream_url:
                    if self._stream_url_matches(stream_url, resolved.resolved_url):
                        score += 100
                    if resolved.delivery_url and self._stream_url_matches(stream_url, resolved.delivery_url):
                        score += 40
                if station_name and channel_name and self._station_name_matches(channel_name, station_name):
                    score += 25

                if score > best_score:
                    best_score = score
                    best_station_id = station_id

            if best_station_id:
                iris_hosts = {f"iris-{mandate}.loverad.io"}
                resolved_parts = [part for part in urlparse(resolved.resolved_url).path.lower().split("/") if part]
                for part in resolved_parts:
                    match = re.match(r"([a-z0-9]{2,8})[-_]", part)
                    if not match:
                        continue
                    iris_hosts.add(f"iris-{match.group(1)}.loverad.io")
                    break

                for iris_host in sorted(iris_hosts):
                    flow_url = f"https://{iris_host}/flow.json?station={best_station_id}&offset=1&count=1"
                    probe_text, probe_type = self._fetch_text(flow_url)
                    if not probe_text:
                        continue
                    if not self._is_json_candidate(flow_url, probe_type, probe_text):
                        continue
                    flow_urls.add(flow_url)

        return flow_urls

    def _extract_channel_feed_urls(
        self,
        payload: dict,
        config_url: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        channels_raw = payload.get("channels")
        channels: list[dict] = []
        if isinstance(channels_raw, dict):
            channels = [value for value in channels_raw.values() if isinstance(value, dict)]
        elif isinstance(channels_raw, list):
            channels = [value for value in channels_raw if isinstance(value, dict)]
        if not channels:
            return set()

        station_name = (station.name if station else "") or resolved.station_name or ""
        input_label = (resolved.input_url or "").strip()
        parsed_config = urlparse(config_url)
        config_base = f"{parsed_config.scheme}://{parsed_config.netloc}" if parsed_config.scheme and parsed_config.netloc else ""

        scored_channels: list[tuple[int, dict]] = []
        for channel in channels:
            score = 0

            channel_id = self._extract_json_value(channel, {"id", "slug", "name"})
            channel_title = self._extract_json_value(channel, {"title", "label", "channel"})
            candidate_name = channel_title or channel_id
            stream_url = self._extract_json_value(
                channel,
                {"streamurl", "stream_url", "audiourl", "audio_url", "url", "mount"},
            )
            current_url = self._extract_json_value(
                channel,
                {"currenturl", "current_url", "nowplayingurl", "now_playing_url"},
            )
            playlist_url = self._extract_json_value(
                channel,
                {"playlisturl", "playlist_url", "historyurl", "history_url"},
            )

            if current_url:
                score += 40
            if playlist_url:
                score += 10
            if stream_url and self._stream_url_matches(stream_url, resolved.resolved_url):
                score += 120
            if candidate_name and station_name and self._station_name_matches(candidate_name, station_name):
                score += 90
            if candidate_name and input_label and self._station_name_matches(candidate_name, input_label):
                score += 70

            if score > 0:
                scored_channels.append((score, channel))

        if not scored_channels:
            return set()

        scored_channels.sort(key=lambda item: item[0], reverse=True)
        best_score = scored_channels[0][0]
        top_channels = [channel for score, channel in scored_channels if score == best_score]

        feed_urls = set()
        for channel in top_channels:
            for keyset in (
                {"currenturl", "current_url", "nowplayingurl", "now_playing_url"},
                {"playlisturl", "playlist_url", "historyurl", "history_url"},
            ):
                value = self._extract_json_value(channel, keyset)
                if not value:
                    continue
                if is_probable_url(value):
                    feed_urls.add(value)
                elif config_base:
                    feed_urls.add(urljoin(config_base, value))

        return feed_urls

    def _discover_stream_keys(
        self,
        documents: list[tuple[str, str, list[str]]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> list[str]:
        strong_primary = []
        strong_secondary = []
        weak = []

        station_name = (station.name if station else "") or resolved.station_name or ""
        for _, text, _ in documents:
            for key, name in re.findall(
                r"skey\s*[:=]\s*['\"]([A-Za-z0-9_-]{6,})['\"][^{}]{0,200}?name\s*[:=]\s*['\"]([^'\"]+)['\"]",
                text,
                flags=re.IGNORECASE,
            ):
                if self._station_name_matches(name, station_name):
                    if key not in strong_secondary:
                        strong_secondary.append(key)
                elif key not in weak:
                    weak.append(key)

            if not (text.lstrip().startswith("{") or text.lstrip().startswith("[")):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue

            for node in self._walk_json_objects(payload):
                key = self._extract_json_value(
                    node,
                    {"skey", "streamkey", "channelkey", "key", "k"},
                )
                if not key:
                    continue

                stream_url = self._extract_json_value(
                    node,
                    {"audiourl", "streamurl", "audio_url", "stream", "url", "playurl", "mount"},
                )
                item_name = self._extract_json_value(node, {"name", "title", "station", "channel"})

                if stream_url and self._stream_url_matches(stream_url, resolved.resolved_url):
                    if key not in strong_primary:
                        strong_primary.append(key)
                elif item_name and self._station_name_matches(item_name, station_name):
                    if key not in strong_secondary:
                        strong_secondary.append(key)
                elif key not in weak:
                    weak.append(key)

        strong = strong_primary + [key for key in strong_secondary if key not in strong_primary]
        if strong:
            merged = strong
        else:
            merged = weak
        return merged[:8]

    def _inject_stream_key(self, base_url: str, key: str) -> set[str]:
        candidates = set()
        if not base_url or not key:
            return candidates

        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            return candidates

        if "metadata/channel/" in parsed.path.lower():
            path = parsed.path
            if path.endswith("/"):
                path = f"{path}{key}.json"
            elif path.endswith(".json"):
                path = re.sub(r"/[^/]+\.json$", f"/{key}.json", path)
            else:
                path = f"{path.rstrip('/')}/{key}.json"
            candidates.add(urlunparse(parsed._replace(path=path, query="")))

        # Replace known key-like query parameters if present.
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        replaced = False
        for param in ("k", "skey", "streamkey", "channelkey", "key"):
            if param in existing:
                existing[param] = key
                replaced = True
        if replaced:
            candidates.add(urlunparse(parsed._replace(query=urlencode(existing, doseq=True))))

        # Generic fallback parameter variants.
        base_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key_param in ("k", "skey", "streamkey", "channelkey", "key"):
            base_query.pop(key_param, None)
        for param in ("k", "skey", "key"):
            query = dict(base_query)
            query[param] = key
            candidates.add(urlunparse(parsed._replace(query=urlencode(query, doseq=True))))

        return candidates

    def _expand_ctrl_api_feed_variants(self, url: str) -> list[str]:
        normalized = str(url or "").strip()
        if not normalized:
            return []

        variants: list[str] = []
        seen = set()

        def _remember(candidate: str) -> None:
            candidate = str(candidate or "").strip()
            if not candidate or candidate in seen:
                return
            seen.add(candidate)
            variants.append(candidate)

        _remember(normalized)
        parsed = urlparse(normalized)
        lower_path = parsed.path.lower()
        if "/ctrl-api/" not in lower_path:
            return variants

        if not (
            lower_path.endswith("/getcurrentsong")
            or lower_path.endswith("/getplaylist")
        ):
            return variants

        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        base_query = dict(query)
        base_query.pop("typ", None)
        base_query.pop("ts", None)

        bucket_now = int(time.time() // 3600) * 3600
        bucket_prev = bucket_now - 3600
        playlist_path = re.sub(r"/getcurrentsong$", "/getPlaylist", parsed.path, flags=re.IGNORECASE)
        playlist_path = re.sub(r"/getplaylist$", "/getPlaylist", playlist_path, flags=re.IGNORECASE)

        for bucket in (bucket_now, bucket_prev):
            playlist_query = dict(base_query)
            playlist_query["typ"] = "hour"
            playlist_query["ts"] = str(bucket)
            _remember(urlunparse(parsed._replace(path=playlist_path, query=urlencode(playlist_query, doseq=True))))

        return variants

    def _stream_url_matches(self, candidate_url: str, resolved_url: str) -> bool:
        a = urlparse(self._normalize_seed(candidate_url))
        b = urlparse(self._normalize_seed(resolved_url))
        if not a.netloc or not b.netloc:
            return False
        if get_base_domain(a.netloc) != get_base_domain(b.netloc):
            return False

        a_parts = [part for part in a.path.lower().split("/") if part]
        b_parts = [part for part in b.path.lower().split("/") if part]
        if not a_parts or not b_parts:
            return False
        if a_parts == b_parts:
            return True
        if len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts[:2] == b_parts[:2]:
            return True
        if len(a_parts) >= 3 and len(b_parts) >= 3 and a_parts[:3] == b_parts[:3]:
            return True
        return False

    def _station_name_matches(self, candidate_name: str, station_name: str) -> bool:
        left_sig = self._tokenize_name(candidate_name, strict=True)
        right_sig = self._tokenize_name(station_name, strict=True)
        if left_sig and right_sig:
            return bool(left_sig & right_sig)

        left = self._tokenize_name(candidate_name, strict=False)
        right = self._tokenize_name(station_name, strict=False)
        if not left or not right:
            return False
        common = left & right
        return len(common) >= 2

    def _tokenize_name(self, value: str, strict: bool) -> set[str]:
        if strict:
            stopwords = {
                "radio",
                "fm",
                "deutschland",
                "star",
                "maximum",
                "rock",
                "max",
                "music",
                "network",
            }
        else:
            stopwords = {"radio", "fm", "deutschland"}
        tokens = split_search_tokens(value)
        return {
            token
            for token in tokens
            if (len(token) >= 3 or is_mixed_alnum_token(token)) and token not in stopwords and not token.isdigit()
        }

    def _is_time_range_active(self, start_value: str, end_value: str) -> bool:
        start_at = self._parse_datetime(start_value)
        end_at = self._parse_datetime(end_value)
        if not start_at or not end_at:
            return False

        start_ref, now_ref = self._normalize_window_datetimes(start_at)
        end_ref, _ = self._normalize_window_datetimes(end_at)
        return start_ref <= now_ref <= end_ref

    def _is_time_range_expired(self, start_value: str, end_value: str) -> bool:
        start_at = self._parse_datetime(start_value)
        end_at = self._parse_datetime(end_value)
        if not start_at or not end_at:
            return False
        end_ref, now_ref = self._normalize_window_datetimes(end_at)
        return now_ref > end_ref

    def _duration_from_time_range(self, start_value: str, stop_value: str) -> str:
        start_at = self._parse_datetime(start_value)
        stop_at = self._parse_datetime(stop_value)
        if not start_at or not stop_at:
            return ""
        delta_seconds = int((stop_at - start_at).total_seconds())
        if delta_seconds <= 0:
            return ""
        return str(delta_seconds)

    def _combine_date_and_time(self, date_value: str, time_value: str) -> str:
        date_text = str(date_value or "").strip()
        time_text = str(time_value or "").strip()
        if not date_text or not time_text:
            return ""
        if "T" in time_text or " " in time_text:
            return time_text
        return f"{date_text} {time_text}"

    def _duration_seconds(self, value: str) -> int | None:
        text = (value or "").strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        parts = text.split(":")
        if not parts:
            return None
        try:
            if len(parts) == 3:
                hours, minutes, seconds = [int(part) for part in parts]
                return hours * 3600 + minutes * 60 + seconds
            if len(parts) == 2:
                minutes, seconds = [int(part) for part in parts]
                return minutes * 60 + seconds
        except ValueError:
            return None
        return None

    def _is_duration_window_active(self, start_value: str, duration_value: str) -> bool:
        start_at = self._parse_datetime(start_value)
        duration_seconds = self._duration_seconds(duration_value)
        if not start_at or duration_seconds is None:
            return False
        if duration_seconds <= 0 or duration_seconds > 4 * 3600:
            return False

        start_ref, now_ref = self._normalize_window_datetimes(start_at)

        if now_ref < (start_ref - timedelta(seconds=120)):
            return False

        end_ref = start_ref + timedelta(seconds=duration_seconds + NOWPLAYING_DURATION_GRACE_SECONDS)
        return start_ref <= now_ref <= end_ref

    def _is_duration_window_expired(self, start_value: str, duration_value: str) -> bool:
        start_at = self._parse_datetime(start_value)
        duration_seconds = self._duration_seconds(duration_value)
        if not start_at or duration_seconds is None:
            return False
        if duration_seconds <= 0 or duration_seconds > 4 * 3600:
            return False

        start_ref, now_ref = self._normalize_window_datetimes(start_at)
        age_seconds = (now_ref - start_ref).total_seconds()

        if age_seconds < -120:
            return False

        return age_seconds > (duration_seconds + NOWPLAYING_DURATION_GRACE_SECONDS)

    def _normalize_window_datetimes(self, value: datetime) -> tuple[datetime, datetime]:
        if value.tzinfo is None:
            return value, datetime.now()
        return value.astimezone(timezone.utc), datetime.now(timezone.utc)

    def _cache_bust_url(self, url: str) -> str:
        if not url:
            return url
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["_ts"] = str(int(time.time()))
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _mark_trusted_candidate(self, url: str) -> None:
        if not url:
            return
        self._trusted_candidates.add(url)
        base = get_base_domain(url)
        if base:
            self._linked_domains.add(base)

    def _fetch_text_once(self, url: str, context=None) -> tuple[str, str]:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=DISCOVERY_REQUEST_TIMEOUT_SECONDS, context=context) as response:
            content_type = response.headers.get("Content-Type") or ""
            lower_type = content_type.lower()
            if lower_type.startswith("audio/") or lower_type.startswith("video/"):
                return "", content_type
            payload = response.read(DISCOVERY_READ_BYTES)
            return decode_text_bytes(payload, content_type=content_type), content_type

    def _http_fallback_url(self, url: str) -> str:
        parsed = urlparse(url)
        if (parsed.scheme or "").lower() != "https":
            return ""
        return urlunparse(parsed._replace(scheme="http"))

    def _fetch_text(self, url: str) -> tuple[str, str]:
        try:
            return self._fetch_text_once(url)
        except URLError as err:
            if isinstance(err.reason, ssl.SSLCertVerificationError):
                # Best effort fallback for feeds with broken cert chains.
                context = ssl._create_unverified_context()
                try:
                    return self._fetch_text_once(url, context=context)
                except Exception:
                    pass
            fallback_url = self._http_fallback_url(url)
            if fallback_url:
                try:
                    return self._fetch_text_once(fallback_url)
                except Exception:
                    return "", ""
            return "", ""
        except Exception:
            fallback_url = self._http_fallback_url(url)
            if fallback_url:
                try:
                    return self._fetch_text_once(fallback_url)
                except Exception:
                    return "", ""
            return "", ""
