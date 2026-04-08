"""Station lookup by broadcaster name using Radio-Browser."""

from __future__ import annotations

import html
import json
import re
import ssl
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .config import (
    RADIO_BROWSER_BASE_URLS,
    RADIO_BROWSER_LOOKUP_LIMIT,
    REQUEST_TIMEOUT_SECONDS,
    SUPPORTED_PLAYLIST_CONTENT_TYPES,
    USER_AGENT,
)
from .models import StationMatch
from .utils import get_base_domain, is_non_origin_directory_url, is_probable_url, safe_int


class StationLookupError(Exception):
    pass


class StationLookupService:
    def __init__(self, log) -> None:
        self._log = log

    def find_best_match(self, query: str) -> StationMatch:
        query_clean = query.strip()
        if not query_clean:
            raise StationLookupError("Kein Sendername angegeben.")

        collected = []
        errors = []
        successful_requests = 0

        for base_url in RADIO_BROWSER_BASE_URLS:
            endpoint = (
                f"{base_url}/json/stations/byname/{quote(query_clean)}"
                f"?hidebroken=true&limit={RADIO_BROWSER_LOOKUP_LIMIT}&order=votes&reverse=true"
            )
            self._log(f"Sender-Suche gegen: {base_url}")
            try:
                request = Request(endpoint, headers={"User-Agent": USER_AGENT})
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    payload = json.load(response)
            except Exception as err:
                errors.append(f"{base_url}: {err}")
                continue

            successful_requests += 1
            candidates = self._extract_candidates(payload)
            if candidates:
                collected.extend(candidates)
                break

        if not collected:
            fallback_station = self._fallback_web_directory_station(query_clean)
            if fallback_station:
                self._log(
                    "Sender-Match (Web-Fallback): "
                    f"{fallback_station.name} | {fallback_station.country or '-'} | "
                    f"{fallback_station.codec or '-'} {fallback_station.bitrate}kbps | "
                    f"votes={fallback_station.votes}"
                )
                return fallback_station

            if successful_requests == 0 and errors:
                raise StationLookupError("Sender-Suche fehlgeschlagen: " + " | ".join(errors))
            if errors:
                self._log("Hinweis: einzelne Sender-Suche-Mirrors nicht erreichbar: " + " | ".join(errors))
            raise StationLookupError(f"Keinen passenden Sender gefunden für '{query_clean}'.")

        deduped = self._dedupe_candidates(collected)
        ranked = sorted(deduped, key=lambda station: self._score_station(station, query_clean), reverse=True)
        best = ranked[0]

        self._log(
            f"Sender-Match: {best.name} | {best.country} | {best.codec} {best.bitrate}kbps | votes={best.votes}"
        )
        return best

    def _extract_candidates(self, payload: list[dict]) -> list[StationMatch]:
        candidates: list[StationMatch] = []
        for item in payload:
            stream_url = (item.get("url") or item.get("url_resolved") or "").strip()
            if not stream_url:
                continue

            candidates.append(
                StationMatch(
                    stationuuid=(item.get("stationuuid") or "").strip(),
                    name=(item.get("name") or "").strip(),
                    stream_url=stream_url,
                    homepage=(item.get("homepage") or "").strip(),
                    country=(item.get("country") or "").strip(),
                    language=(item.get("language") or "").strip(),
                    codec=(item.get("codec") or "").strip(),
                    bitrate=safe_int(item.get("bitrate")),
                    votes=safe_int(item.get("votes")),
                    lastcheckok=safe_int(item.get("lastcheckok")),
                    raw_record=item,
                )
            )
        return candidates

    def _dedupe_candidates(self, stations: list[StationMatch]) -> list[StationMatch]:
        seen = set()
        deduped = []
        for station in stations:
            key = (station.stationuuid, station.stream_url)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(station)
        return deduped

    def _score_station(self, station: StationMatch, query: str) -> float:
        similarity = SequenceMatcher(None, station.name.lower(), query.lower()).ratio()
        exact_bonus = 300 if station.name.lower() == query.lower() else 0
        health_score = 1000 if station.lastcheckok == 1 else 0
        vote_score = min(station.votes, 100000) * 0.05
        bitrate_score = min(station.bitrate, 320) * 0.2
        return (similarity * 500) + exact_bonus + health_score + vote_score + bitrate_score

    def _fallback_web_directory_station(self, query: str) -> StationMatch | None:
        slugs = self._build_directory_slugs(query)
        if not slugs:
            return None

        for slug in slugs:
            directory_pages = (
                f"https://www.radio.de/s/{slug}",
                f"https://www.radio.net/s/{slug}",
            )
            for page_url in directory_pages:
                self._log(f"Fallback-Lookup Probe: {page_url}")
                page_html = self._fetch_text(page_url)
                if not page_html:
                    continue

                stream_url = self._extract_stream_candidate(page_html)
                if not stream_url:
                    continue

                if not self._looks_like_stream_endpoint(stream_url):
                    continue

                station_name = self._extract_title(page_html) or query
                official_homepage = self._extract_official_homepage(page_html, query=query, stream_url=stream_url)
                homepage = official_homepage or self._homepage_from_stream(stream_url) or page_url
                return StationMatch(
                    stationuuid=f"web-fallback:{slug}",
                    name=station_name,
                    stream_url=stream_url,
                    homepage=homepage,
                    country="",
                    language="",
                    codec="",
                    bitrate=0,
                    votes=0,
                    lastcheckok=1,
                    raw_record={
                    "source": "web_directory_fallback",
                    "page_url": page_url,
                    "official_homepage": official_homepage or "",
                    "derived_homepage": self._homepage_from_stream(stream_url) or "",
                    "slug": slug,
                    "stream_url": stream_url,
                },
            )

        return None

    def _build_directory_slugs(self, value: str) -> list[str]:
        normalized = (value or "").strip().lower()
        if not normalized:
            return []

        tokenized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
        if not tokenized:
            return []

        tokens = [token for token in tokenized.split() if token]
        without_radio_tokens = [token for token in tokens if token != "radio"]

        compact = "".join(tokens)
        dashed = "-".join(tokens)

        variants = [compact, dashed]
        if without_radio_tokens:
            variants.append("".join(without_radio_tokens))
            variants.append("-".join(without_radio_tokens))

        deduped = []
        seen = set()
        for variant in variants:
            clean = variant.strip("-")
            if len(clean) < 3:
                continue
            if clean in seen:
                continue
            seen.add(clean)
            deduped.append(clean)
        return deduped

    def _fetch_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read(900000)
                return payload.decode("utf-8", errors="ignore")
        except Exception as err:
            if isinstance(getattr(err, "reason", None), ssl.SSLCertVerificationError):
                context = ssl._create_unverified_context()
                try:
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
                        payload = response.read(900000)
                        return payload.decode("utf-8", errors="ignore")
                except Exception:
                    return ""
            return ""

    def _extract_stream_candidate(self, page_html: str) -> str:
        normalized = html.unescape(page_html).replace("\\/", "/")
        urls = set(re.findall(r"https?://[^\"'\s<>()]+", normalized, flags=re.IGNORECASE))
        if not urls:
            return ""

        ranked = sorted(urls, key=self._stream_candidate_score, reverse=True)
        for candidate in ranked[:20]:
            clean_candidate = self._sanitize_candidate_url(candidate)
            if self._looks_like_stream_endpoint(clean_candidate):
                return clean_candidate
        return ""

    def _extract_official_homepage(self, page_html: str, query: str, stream_url: str) -> str:
        normalized = html.unescape(page_html).replace("\\/", "/")
        urls = set(re.findall(r"https?://[^\"'\s<>()]+", normalized, flags=re.IGNORECASE))
        if not urls:
            return ""

        query_tokens = {
            token
            for token in re.findall(r"[a-z0-9]{3,}", (query or "").lower())
            if token not in {"radio", "stream", "live"}
        }
        stream_base = get_base_domain(stream_url)
        scored = []

        for url in urls:
            candidate = self._sanitize_candidate_url(url)
            if not is_probable_url(candidate):
                continue
            if is_non_origin_directory_url(candidate):
                continue
            if self._looks_like_non_page_asset(candidate):
                continue
            if self._looks_like_stream_pattern(candidate):
                continue

            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
            if not host:
                continue
            if self._looks_like_generic_metadata_domain(host):
                continue

            candidate_base = get_base_domain(candidate)
            host_match = any(token in host for token in query_tokens)
            path_match = any(token in parsed.path.lower() for token in query_tokens)
            if stream_base and candidate_base != stream_base and not (host_match or path_match):
                continue

            score = 0
            if parsed.scheme == "https":
                score += 10
            if parsed.path in {"", "/"}:
                score += 20
            if stream_base:
                if candidate_base == stream_base:
                    score += 20
                else:
                    score -= 10
            if host_match:
                score += 20
            if path_match:
                score += 8
            if query_tokens and not (host_match or path_match):
                score -= 15

            scored.append((score, candidate))

        if not scored:
            return ""

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def _stream_candidate_score(self, url: str) -> int:
        lower = url.lower().rstrip("\\")
        score = 0
        if any(token in lower for token in ("stream", "listen", "icecast", "hls", "aac", "mp3", "radio")):
            score += 40
        if "nrjaudio.fm" in lower:
            score += 50
        if "/s/" in lower or "station-images" in lower or "podcast-images" in lower:
            score -= 120
        if any(token in lower for token in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".css", ".js")):
            score -= 200
        return score

    def _looks_like_stream_endpoint(self, url: str) -> bool:
        clean_url = url.strip().rstrip("\\")
        if not clean_url:
            return False

        request = Request(clean_url, headers={"User-Agent": USER_AGENT, "Icy-MetaData": "1"})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                headers = response.headers
                content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type.startswith("audio/"):
                    return True
                if content_type in SUPPORTED_PLAYLIST_CONTENT_TYPES:
                    return True
                if headers.get("icy-metaint") or headers.get("icy-name"):
                    return True
        except Exception as err:
            if isinstance(getattr(err, "reason", None), ssl.SSLCertVerificationError):
                context = ssl._create_unverified_context()
                try:
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
                        headers = response.headers
                        content_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                        if content_type.startswith("audio/"):
                            return True
                        if content_type in SUPPORTED_PLAYLIST_CONTENT_TYPES:
                            return True
                        if headers.get("icy-metaint") or headers.get("icy-name"):
                            return True
                except Exception:
                    return False
            return False
        return False

    def _looks_like_stream_pattern(self, url: str) -> bool:
        lower = url.lower()
        if any(lower.endswith(ext) for ext in (".mp3", ".aac", ".ogg", ".m4a", ".flac", ".m3u", ".m3u8", ".pls", ".xspf")):
            return True
        stream_markers = ("stream", "listen", "icecast", "livestream", "radioaudio")
        codec_markers = ("mp3", "aac", "ogg", "m3u", "m3u8", "pls", "xspf")
        return any(marker in lower for marker in stream_markers) and any(marker in lower for marker in codec_markers)

    def _looks_like_non_page_asset(self, url: str) -> bool:
        lower = url.lower()
        return any(
            lower.endswith(ext)
            for ext in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
                ".svg",
                ".css",
                ".js",
                ".woff",
                ".woff2",
                ".ttf",
                ".pdf",
                ".mp4",
                ".webm",
            )
        )

    def _looks_like_generic_metadata_domain(self, host: str) -> bool:
        host = (host or "").lower()
        blocked = {
            "schema.org",
            "www.schema.org",
            "w3.org",
            "www.w3.org",
            "facebook.com",
            "www.facebook.com",
            "twitter.com",
            "www.twitter.com",
            "instagram.com",
            "www.instagram.com",
            "linkedin.com",
            "www.linkedin.com",
            "youtube.com",
            "www.youtube.com",
            "tiktok.com",
            "www.tiktok.com",
        }
        return host in blocked

    def _homepage_from_stream(self, stream_url: str) -> str:
        base = get_base_domain(stream_url)
        if not base:
            return ""
        return f"https://www.{base}/"

    def _extract_title(self, page_html: str) -> str:
        match = re.search(r"<title>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        title = html.unescape(match.group(1)).strip()
        if "|" in title:
            title = title.split("|", 1)[0].strip()
        return title

    def _sanitize_candidate_url(self, value: str) -> str:
        return value.strip().rstrip("\\").rstrip(",;)}]\"'")
