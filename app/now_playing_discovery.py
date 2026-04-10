"""Generic discovery and parsing of web now-playing feeds (XML/JSON)."""

from __future__ import annotations

import html
import json
import re
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .config import (
    DISCOVERY_MAX_CANDIDATES,
    DISCOVERY_READ_BYTES,
    DISCOVERY_REQUEST_TIMEOUT_SECONDS,
    MAX_NOWPLAYING_AGE_MINUTES,
    NOWPLAYING_DURATION_GRACE_SECONDS,
    NOWPLAYING_CANDIDATE_KEYWORDS,
    NOWPLAYING_QUERY_CONTEXT_IGNORE_TOKENS,
    USER_AGENT,
)
from .models import ResolvedStream, SongInfo, StationMatch
from .utils import get_base_domain, is_mixed_alnum_token, is_probable_url, split_search_tokens

TITLE_KEYS = {"title", "song", "track", "tracktitle", "songtitle", "songname", "trackname", "name"}
ARTIST_KEYS = {"artist", "author", "interpret", "performer", "band", "artistname"}
STATUS_KEYS = {"status", "state", "playstate", "onair", "current", "isplaying"}
TIME_KEYS = {"starttime", "start", "timestamp", "time", "date", "datetime"}
DURATION_KEYS = {"duration", "length", "duration_sec", "duration_seconds", "runtime"}
HTML_TITLE_CLASS_KEYS = ("js_title", "songtitle", "tracktitle", "title", "track", "song", "songname", "trackname")
HTML_ARTIST_CLASS_KEYS = ("js_artist", "interpret", "artist", "artistname", "performer", "band", "author")


class NowPlayingDiscoveryService:
    def __init__(self, log) -> None:
        self._log = log
        self._trusted_candidates: set[str] = set()
        self._linked_domains: set[str] = set()

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
            return station_domain_matches
        return generic_html_matches

    def discover_candidate_urls(
        self,
        resolved: ResolvedStream,
        station: StationMatch | None,
        stream_headers: dict[str, str],
    ) -> list[str]:
        self._trusted_candidates = set()
        self._linked_domains = set()

        seeds = self._build_seed_urls(resolved, station, stream_headers)
        candidates = set()
        visited_pages = set()
        discovery_page_budget = 4
        script_fetch_budget = 2
        seed_documents: list[tuple[str, str]] = []

        for seed in seeds:
            visited_pages.add(seed)
            if self._looks_like_feed_url(seed):
                candidates.add(seed)
                self._mark_trusted_candidate(seed)

            if self._looks_like_stream_endpoint(seed):
                continue

            text, _ = self._fetch_text(seed)
            if not text:
                continue

            seed_documents.append((seed, text))
            for extracted in self._extract_urls_from_document(text, seed):
                if self._looks_like_feed_url(extracted):
                    candidates.add(extracted)
                    self._mark_trusted_candidate(extracted)

                if (
                    discovery_page_budget > 0
                    and self._looks_like_discovery_page(extracted)
                    and get_base_domain(extracted) == get_base_domain(seed)
                    and extracted not in visited_pages
                ):
                    visited_pages.add(extracted)
                    discovery_page_budget -= 1
                    page_text, _ = self._fetch_text(extracted)
                    if page_text:
                        seed_documents.append((extracted, page_text))
                        for nested in self._extract_urls_from_document(page_text, extracted):
                            if self._looks_like_feed_url(nested):
                                candidates.add(nested)
                                self._mark_trusted_candidate(nested)

                # Generic second-level discovery for player descriptor docs.
                if "avcustom" in extracted.lower() and get_base_domain(extracted) == get_base_domain(seed):
                    sub_text, _ = self._fetch_text(extracted)
                    if not sub_text:
                        continue
                    seed_documents.append((extracted, sub_text))
                    for nested in self._extract_urls_from_document(sub_text, extracted):
                        if self._looks_like_feed_url(nested):
                            candidates.add(nested)
                            self._mark_trusted_candidate(nested)

                if (
                    script_fetch_budget > 0
                    and self._looks_like_script_asset(extracted)
                    and get_base_domain(extracted) == get_base_domain(seed)
                    and extracted not in visited_pages
                ):
                    visited_pages.add(extracted)
                    script_fetch_budget -= 1
                    script_text, _ = self._fetch_text(extracted)
                    if not script_text:
                        continue
                    seed_documents.append((extracted, script_text))
                    for nested in self._extract_urls_from_document(script_text, extracted):
                        if self._looks_like_feed_url(nested):
                            candidates.add(nested)
                            self._mark_trusted_candidate(nested)

        generated = self._build_generated_candidates(seed_documents, candidates, resolved, station)
        for generated_url in generated:
            candidates.add(generated_url)
            self._mark_trusted_candidate(generated_url)

        official_player_feeds = self._discover_official_player_feed_urls(seed_documents, resolved, station)
        for feed_url in official_player_feeds:
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

        normalized_candidates = self._dedupe_url_variants(candidates)
        context_filtered_candidates = {
            url
            for url in normalized_candidates
            if self._candidate_matches_input_context(url, resolved, station)
        }
        if context_filtered_candidates:
            normalized_candidates = context_filtered_candidates
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
        if limited:
            self._log(f"Now-Playing Kandidaten gefunden: {len(limited)}")
        else:
            self._log("Keine Now-Playing Kandidaten gefunden")
        return limited

    def fetch_now_playing(self, candidate_urls: list[str]) -> SongInfo | None:
        partial_match: SongInfo | None = None
        for url in candidate_urls[:DISCOVERY_MAX_CANDIDATES]:
            request_url = url if self._looks_like_html_nowplaying_endpoint(url) else self._cache_bust_url(url)
            text, content_type = self._fetch_text(request_url)
            if not text:
                continue

            song = None
            if self._is_json_candidate(url, content_type, text):
                song = self._parse_json_payload(text, url)

            if not song:
                song = self._parse_xml_payload(text, url)

            if not song and self._looks_like_html_nowplaying_endpoint(url):
                song = self._parse_html_payload(text, url)

            if not song:
                continue

            if song.artist and song.title:
                self._log(f"Now-Playing Treffer aus Feed: {url}")
                return song

            if not partial_match and (song.artist or song.title or song.stream_title):
                partial_match = song

        if partial_match:
            self._log(f"Now-Playing Fallback ohne vollständigen Artist+Title: {partial_match.source_url}")
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
        normalized_text = html.unescape(html.unescape(text)).replace("\\/", "/")
        urls = set()

        for match in re.findall(r"https?://[^\"'`\s<>()]+", normalized_text, flags=re.IGNORECASE):
            urls.add(match)

        for match in re.findall(r"(?:href|src|data-[a-z0-9_-]+)=[\"']([^\"']+)[\"']", normalized_text, flags=re.IGNORECASE):
            if match.startswith("javascript:"):
                continue
            if match.startswith("www."):
                urls.add(f"https://{match}")
            elif match.startswith("//"):
                base_scheme = urlparse(base_url).scheme or "https"
                urls.add(f"{base_scheme}:{match}")
            else:
                urls.add(urljoin(base_url, match))

        for match in re.findall(
            r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.xml|\.json)(?:\?[^\"'\s<>()]+)?",
            normalized_text,
            flags=re.IGNORECASE,
        ):
            if match.startswith("/www."):
                urls.add(f"https://{match.lstrip('/')}")
            else:
                urls.add(urljoin(base_url, match))

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
                urls.add(f"https://{match.lstrip('/')}")
            else:
                urls.add(urljoin(base_url, match))

        # Generic CMS pattern: entries like "something--100" often expose "*-avCustom.xml".
        for content_id in re.findall(r"([a-z0-9][a-z0-9-]{5,}--\d+)", normalized_text, flags=re.IGNORECASE):
            lower_id = content_id.lower()
            if not any(hint in lower_id for hint in ("live", "stream", "radio", "onair")):
                continue
            urls.add(urljoin(base_url, f"/stream/{content_id}-avCustom.xml"))
            urls.add(urljoin(base_url, f"/{content_id}-avCustom.xml"))

        cleaned = []
        seen = set()
        for url in urls:
            normalized = html.unescape(url.strip())
            if not is_probable_url(normalized):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)

        return cleaned

    def _looks_like_feed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        haystack = f"{path}?{query}"
        lower_url = url.lower()

        if "${" in url or "%24%7b" in lower_url:
            return False
        if "form_action_url=" in lower_url:
            return False

        if "avcustom" in path:
            return any(hint in path for hint in ("live", "stream", "radio", "onair"))

        if "status-json.xsl" in path:
            return True
        if path.endswith(".xsl") and "status" in path:
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
        has_api_hint = "api" in path or "scripts" in path or parsed.netloc.lower().startswith("api.")

        if has_feed_ext and (has_keyword or strong_api_keyword):
            return True
        if has_query_feed_hint and (has_keyword or has_api_hint):
            return True
        if has_api_hint and strong_api_keyword:
            return True
        if "metadata/channel/" in path and (path.endswith(".json") or path.endswith("/")):
            return True
        if self._looks_like_html_nowplaying_endpoint(url):
            return True
        return False

    def _looks_like_html_nowplaying_endpoint(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        haystack = f"{path}?{query}"

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
                "songs.html",
                "songs.htm",
                "playlist/index.jsp",
                "radiomodul",
            )
        )
        if has_html_hint and has_direct_nowplaying_path:
            return True
        return (has_html_hint or has_reload_hint) and has_reload_hint and has_nowplaying_hint

    def _looks_like_discovery_page(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
            return False
        if path.endswith(".html") or path.endswith("/"):
            hints = ("stream", "livestream", "radio", "playlist", "onair", "now")
            return any(hint in path for hint in hints)
        return False

    def _looks_like_script_asset(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if not path.endswith(".js"):
            return False
        hints = ("webcode", "player", "radio", "main", "app", "bundle")
        return any(hint in path for hint in hints)

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
        path_query = f"{parsed.path.lower()}?{parsed.query.lower()}"
        path = parsed.path.lower()
        query = parsed.query.lower()
        score = 0
        if path.endswith(".xml"):
            score += 5
        if path.endswith(".json"):
            score += 5
        for keyword in NOWPLAYING_CANDIDATE_KEYWORDS:
            if keyword in path_query:
                score += 10
        if "avcustom" in lower:
            score += 40
        if "playlist" in path_query or "titelliste" in path_query:
            score += 20
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
        return payload.strip().startswith("{") or payload.strip().startswith("[")

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
            if self._looks_like_placeholder_title(title, artist):
                score -= 80

            age_minutes = self._age_minutes(time_text)
            if age_minutes is not None:
                if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                    score -= 120
                else:
                    score += 20
            if self._is_duration_window_expired(time_text, duration_text):
                score -= 120

            if score > best_score:
                best = (artist, title, time_text, duration_text)
                best_score = score

        if not best:
            return None

        artist, title, best_time_text, best_duration_text = best
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
            source_kind="web_feed_xml",
            source_url=source_url,
        )

    def _xml_status_score(self, elem: ET.Element) -> int:
        status = (elem.attrib.get("status") or "").strip().lower()
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
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None

        candidates = []
        for node in self._walk_json_objects(data):
            title = self._extract_json_value(node, TITLE_KEYS)
            artist = self._extract_json_value(node, ARTIST_KEYS)
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
            if self._looks_like_placeholder_title(title, artist):
                score -= 80
            age_minutes = self._age_minutes(time_text)
            if age_minutes is not None:
                if age_minutes > MAX_NOWPLAYING_AGE_MINUTES:
                    score -= 120
                else:
                    score += 20
            if self._is_duration_window_expired(time_text, duration_text):
                score -= 120

            candidates.append((score, artist, title, time_text, duration_text))

        if not candidates:
            return None

        candidates.sort(reverse=True, key=lambda item: item[0])
        best_score, artist, title, time_text, duration_text = candidates[0]
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
            source_kind="web_feed_json",
            source_url=source_url,
        )

    def _parse_html_payload(self, payload: str, source_url: str) -> SongInfo | None:
        if not payload:
            return None

        artist = ""
        title = ""
        current_show_artist, current_show_title = self._extract_current_show_song(payload)
        if current_show_artist and current_show_title:
            artist = current_show_artist
            title = current_show_title

        has_list_blocks = bool(re.search(r"<li\b", payload, flags=re.IGNORECASE))
        if not (artist and title):
            scored_candidates = self._extract_html_song_candidates(payload)
            if scored_candidates:
                scored_candidates.sort(reverse=True, key=lambda item: item[0])
                best_score, artist, title = scored_candidates[0]
                if best_score < 10:
                    return None
            elif has_list_blocks:
                # A structured list was present, but no valid "current" item survived filtering.
                return None
            else:
                artist = self._extract_html_class_value(payload, HTML_ARTIST_CLASS_KEYS)
                title = self._extract_html_class_value(payload, HTML_TITLE_CLASS_KEYS)

                if title and not artist:
                    split_artist, split_title = self._split_compound_title(title)
                    if split_artist and split_title:
                        artist = split_artist
                        title = split_title

        title = title.strip()
        artist = artist.strip()
        if not title and not artist:
            return None
        if not artist or not title:
            return None
        if self._looks_like_placeholder_title(title, artist):
            return None

        stream_title = f"{artist} - {title}".strip(" -")
        return SongInfo(
            stream_title=stream_title,
            raw_metadata=payload,
            artist=artist,
            title=title,
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

    def _extract_html_song_candidates(self, payload: str) -> list[tuple[int, str, str]]:
        blocks = re.findall(r"<li\b[^>]*>.*?</li>", payload, flags=re.IGNORECASE | re.DOTALL)
        if not blocks:
            return []

        candidates: list[tuple[int, str, str]] = []
        for block in blocks:
            artist = self._extract_html_class_value(block, HTML_ARTIST_CLASS_KEYS).strip()
            title = self._extract_html_class_value(block, HTML_TITLE_CLASS_KEYS).strip()
            if title and not artist:
                split_artist, split_title = self._split_compound_title(title)
                if split_artist and split_title:
                    artist = split_artist
                    title = split_title
            if not artist and not title:
                continue
            if self._looks_like_placeholder_title(title, artist):
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

            candidates.append((score, artist, title))

        return candidates

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

        return score

    def _candidate_matches_input_context(
        self,
        url: str,
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> bool:
        lower_url = (url or "").lower()
        if "radiomodul" not in lower_url:
            return True

        query_tokens = set()
        if station and station.name:
            query_tokens.update(self._tokenize_context_tokens(station.name))
        if station and station.stream_url:
            query_tokens.update(self._tokenize_context_tokens(station.stream_url))
        if not query_tokens:
            query_tokens = self._tokenize_context_tokens(resolved.input_url)
        if len(query_tokens) < 2:
            return True

        candidate_tokens = self._tokenize_context_tokens(url)
        common_tokens = query_tokens & candidate_tokens
        min_overlap = 2 if len(query_tokens) < 3 else 3
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

    def _extract_html_datetime(self, payload: str) -> str:
        match = re.search(r'datetime=["\']([^"\']+)["\']', payload, flags=re.IGNORECASE)
        if not match:
            return ""
        return (match.group(1) or "").strip()

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
        return text.strip()

    def _walk_json_objects(self, value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_json_objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json_objects(child)

    def _extract_json_value(self, node: dict, keyset: set[str]) -> str:
        for key, value in node.items():
            if key.lower() not in keyset:
                continue
            if isinstance(value, str):
                return value.strip()
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

    def _looks_like_placeholder_title(self, title: str, artist: str) -> bool:
        if artist and artist.strip():
            return False
        lower = (title or "").strip().lower()
        if not lower:
            return True
        markers = (
            "keine aktuelle",
            "no current",
            "no info",
            "no information",
            "unknown",
            "unbekannt",
        )
        return any(marker in lower for marker in markers)

    def _build_generated_candidates(
        self,
        documents: list[tuple[str, str]],
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

        for doc_url, text in documents:
            for extracted in self._extract_urls_from_document(text, doc_url):
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
                        if self._looks_like_feed_url(generated_url):
                            generated.add(generated_url)
            elif self._looks_like_feed_url(cleaned_base):
                generated.add(cleaned_base)

        return generated

    def _discover_official_player_feed_urls(
        self,
        documents: list[tuple[str, str]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> set[str]:
        config_urls = self._extract_official_config_urls(documents)
        if not config_urls:
            return set()

        feeds = set()
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

            for feed_url in self._extract_channel_feed_urls(payload, config_url, resolved, station):
                if is_probable_url(feed_url):
                    feeds.add(feed_url)

        return feeds

    def _extract_official_config_urls(self, documents: list[tuple[str, str]]) -> set[str]:
        config_urls = set()

        for doc_url, text in documents:
            if not text:
                continue

            mandates = {
                match.strip().lower()
                for match in re.findall(r'data-mandate=["\']([a-z0-9-]+)["\']', text, flags=re.IGNORECASE)
            }
            if not mandates:
                continue

            script_hosts = set()
            for extracted in self._extract_urls_from_document(text, doc_url):
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
        documents: list[tuple[str, str]],
        resolved: ResolvedStream,
        station: StationMatch | None,
    ) -> list[str]:
        strong_primary = []
        strong_secondary = []
        weak = []

        station_name = (station.name if station else "") or resolved.station_name or ""
        for _, text in documents:
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

    def _is_duration_window_expired(self, start_value: str, duration_value: str) -> bool:
        start_at = self._parse_datetime(start_value)
        duration_seconds = self._duration_seconds(duration_value)
        if not start_at or duration_seconds is None:
            return False
        if duration_seconds <= 0 or duration_seconds > 4 * 3600:
            return False

        if start_at.tzinfo is None:
            age_seconds = (datetime.now() - start_at).total_seconds()
        else:
            age_seconds = (datetime.now(timezone.utc) - start_at.astimezone(timezone.utc)).total_seconds()

        if age_seconds < -120:
            return False

        return age_seconds > (duration_seconds + NOWPLAYING_DURATION_GRACE_SECONDS)

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

    def _fetch_text(self, url: str) -> tuple[str, str]:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=DISCOVERY_REQUEST_TIMEOUT_SECONDS) as response:
                content_type = response.headers.get("Content-Type") or ""
                lower_type = content_type.lower()
                if lower_type.startswith("audio/") or lower_type.startswith("video/"):
                    return "", content_type
                payload = response.read(DISCOVERY_READ_BYTES)
                return payload.decode("utf-8", errors="ignore"), content_type
        except URLError as err:
            if isinstance(err.reason, ssl.SSLCertVerificationError):
                # Best effort fallback for feeds with broken cert chains.
                context = ssl._create_unverified_context()
                try:
                    with urlopen(
                        request,
                        timeout=DISCOVERY_REQUEST_TIMEOUT_SECONDS,
                        context=context,
                    ) as response:
                        content_type = response.headers.get("Content-Type") or ""
                        lower_type = content_type.lower()
                        if lower_type.startswith("audio/") or lower_type.startswith("video/"):
                            return "", content_type
                        payload = response.read(DISCOVERY_READ_BYTES)
                        return payload.decode("utf-8", errors="ignore"), content_type
                except Exception:
                    return "", ""
            return "", ""
        except Exception:
            return "", ""
