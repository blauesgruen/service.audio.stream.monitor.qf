"""Station lookup by broadcaster name using Radio-Browser."""

from __future__ import annotations

import html
import json
import re
import ssl
from difflib import SequenceMatcher
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .config import (
    RADIO_BROWSER_BASE_URLS,
    RADIO_BROWSER_LOOKUP_LIMIT,
    RADIO_BROWSER_SEARCH_LOOKUP_LIMIT,
    REQUEST_TIMEOUT_SECONDS,
    STATION_LOOKUP_CHANNEL_FALLBACK_MAX_CHANNELS,
    STATION_LOOKUP_CHANNEL_FALLBACK_MAX_PAGES,
    STATION_LOOKUP_CHANNEL_FALLBACK_MIN_SCORE,
    STATION_LOOKUP_IGNORED_TOKENS,
    STATION_LOOKUP_MAX_QUERY_VARIANTS,
    STATION_LOOKUP_OPTIONAL_PREFIX_TOKENS,
    STATION_LOOKUP_OPTIONAL_SUFFIX_TOKENS,
    STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS,
    STATION_LOOKUP_SEARCH_MIN_TOKEN_LENGTH,
    STATION_LOOKUP_SIGNIFICANT_SHORT_TOKENS,
    STATION_LOOKUP_STRICT_MIN_QUERY_TOKENS,
    STATION_LOOKUP_MAX_SLUG_VARIANTS,
    STATION_LOOKUP_MIN_QUERY_LENGTH,
    STATION_LOOKUP_MIN_TOKENS_PER_VARIANT,
    STATION_LOOKUP_NUMBER_TOKEN_MAP,
    STATION_LOOKUP_SKIP_PREFIX_TOKENS,
    STATION_LOOKUP_SLUG_MIN_LENGTH,
    SUPPORTED_PLAYLIST_CONTENT_TYPES,
    USER_AGENT,
)
from .models import StationMatch
from .utils import (
    decode_text_bytes,
    get_base_domain,
    is_mixed_alnum_token,
    is_non_origin_directory_url,
    is_probable_url,
    safe_int,
    split_search_tokens,
)


class StationLookupError(Exception):
    pass


class StationLookupService:
    def __init__(self, log) -> None:
        self._log = log

    def _fold_german_umlauts(self, value: str) -> str:
        text = str(value or "")
        if not text:
            return ""
        return (
            text.replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
            .replace("Ä", "Ae")
            .replace("Ö", "Oe")
            .replace("Ü", "Ue")
        )

    def find_best_match(self, query: str, station_id: str = "") -> StationMatch:
        query_clean = query.strip()
        if not query_clean:
            raise StationLookupError("Kein Sendername angegeben.")

        collected = []
        errors = []
        successful_requests = 0
        used_search_fallback = False
        lookup_queries = self._build_lookup_queries(query_clean)
        search_queries = self._build_search_queries(query_clean, lookup_queries)

        for lookup_query in lookup_queries:
            query_candidates: list[StationMatch] = []
            for base_url in RADIO_BROWSER_BASE_URLS:
                endpoint = (
                    f"{base_url}/json/stations/byname/{quote(lookup_query)}"
                    f"?hidebroken=true&limit={RADIO_BROWSER_LOOKUP_LIMIT}&order=votes&reverse=true"
                )
                self._log(f"Sender-Suche gegen: {base_url} (query='{lookup_query}')")
                try:
                    request = Request(endpoint, headers={"User-Agent": USER_AGENT})
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                        payload = json.load(response)
                except Exception as err:
                    errors.append(f"{base_url} [{lookup_query}]: {err}")
                    continue

                successful_requests += 1
                candidates = self._extract_candidates(payload)
                if candidates:
                    collected.extend(candidates)
                    query_candidates.extend(candidates)
                    break
            if query_candidates and any(
                self._is_confident_station_match(candidate, query_clean, station_id=station_id) for candidate in query_candidates
            ):
                break

        if not collected:
            search_candidates, search_successful_requests = self._collect_search_candidates(
                query_clean,
                search_queries,
                errors,
                station_id=station_id,
            )
            successful_requests += search_successful_requests
            if search_candidates:
                collected.extend(search_candidates)
                used_search_fallback = True

        if not collected:
            fallback_station = self._fallback_web_directory_station(query_clean)
            if fallback_station:
                self._apply_query_alias_name(fallback_station, query_clean)
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

        if not station_id and self._is_single_token_lookup_query(query_clean):
            direct_fallback_station = self._fallback_web_directory_station(query_clean)
            if (
                direct_fallback_station
                and self._is_confident_station_match(direct_fallback_station, query_clean, station_id=station_id)
                and not self._has_strong_name_equivalence(best.name or "", query_clean)
            ):
                self._apply_query_alias_name(direct_fallback_station, query_clean)
                self._log(
                    "Sender-Match (Single-Token-Web-Priority): "
                    f"{direct_fallback_station.name} | {direct_fallback_station.country or '-'} | "
                    f"{direct_fallback_station.codec or '-'} {direct_fallback_station.bitrate}kbps | "
                    f"votes={direct_fallback_station.votes}"
                )
                return direct_fallback_station

        if not self._is_confident_station_match(best, query_clean, station_id=station_id):
            if not used_search_fallback:
                search_candidates, search_successful_requests = self._collect_search_candidates(
                    query_clean,
                    search_queries,
                    errors,
                    station_id=station_id,
                )
                successful_requests += search_successful_requests
                if search_candidates:
                    used_search_fallback = True
                    deduped = self._dedupe_candidates([*deduped, *search_candidates])
                    ranked = sorted(deduped, key=lambda station: self._score_station(station, query_clean), reverse=True)
                    best = ranked[0]
                    if self._is_confident_station_match(best, query_clean, station_id=station_id):
                        self._log(
                            "Sender-Match (Search-Refine): "
                            f"{best.name} | {best.country} | {best.codec} {best.bitrate}kbps | votes={best.votes}"
                        )
                        return best

            if used_search_fallback:
                if not station_id and self._has_stream_channel_conflict(best, query_clean):
                    self._log(
                        "Search-Fallback Treffer verworfen (Kanal-Konflikt zwischen Name und Stream): "
                        f"{best.name}"
                    )
                else:
                    self._log(
                        "Search-Fallback Treffer verworfen (zu geringe Token-Deckung): "
                        f"{best.name}"
                    )
            else:
                if not station_id and self._has_stream_channel_conflict(best, query_clean):
                    self._log(
                        "Sender-Match verworfen (Kanal-Konflikt zwischen Name und Stream): "
                        f"{best.name}"
                    )
                else:
                    self._log(
                        "Sender-Match verworfen (zu geringe Token-Deckung): "
                        f"{best.name}"
                    )
            channel_fallback_station = self._fallback_channel_station_from_anchor(query_clean, best, station_id=station_id)
            if channel_fallback_station:
                self._apply_query_alias_name(channel_fallback_station, query_clean)
                self._log(
                    "Sender-Match (Channel-Fallback): "
                    f"{channel_fallback_station.name} | {channel_fallback_station.country or '-'} | "
                    f"{channel_fallback_station.codec or '-'} {channel_fallback_station.bitrate}kbps | "
                    f"votes={channel_fallback_station.votes}"
                )
                return channel_fallback_station
            homepage_stream_fallback = self._fallback_stream_from_homepage(query_clean, best)
            if homepage_stream_fallback and self._is_confident_station_match(homepage_stream_fallback, query_clean, station_id=station_id):
                self._apply_query_alias_name(homepage_stream_fallback, query_clean)
                self._log(
                    "Sender-Match (Homepage-Stream-Fallback): "
                    f"{homepage_stream_fallback.name} | {homepage_stream_fallback.country or '-'} | "
                    f"{homepage_stream_fallback.codec or '-'} {homepage_stream_fallback.bitrate}kbps | "
                    f"votes={homepage_stream_fallback.votes}"
                )
                return homepage_stream_fallback
            fallback_station = self._fallback_web_directory_station(query_clean)
            if fallback_station and self._is_confident_station_match(fallback_station, query_clean, station_id=station_id):
                self._apply_query_alias_name(fallback_station, query_clean)
                self._log(
                    "Sender-Match (Web-Fallback): "
                    f"{fallback_station.name} | {fallback_station.country or '-'} | "
                    f"{fallback_station.codec or '-'} {fallback_station.bitrate}kbps | "
                    f"votes={fallback_station.votes}"
                )
                return fallback_station
            raise StationLookupError(f"Keinen passenden Sender gefunden für '{query_clean}'.")

        self._apply_query_alias_name(best, query_clean)
        self._log(
            f"Sender-Match: {best.name} | {best.country} | {best.codec} {best.bitrate}kbps | votes={best.votes}"
        )
        return best

    def find_by_id(self, stationuuid: str) -> StationMatch:
        uuid_clean = str(stationuuid or "").strip()
        if not uuid_clean:
            raise StationLookupError("Keine Station-UUID angegeben.")

        errors = []
        for base_url in RADIO_BROWSER_BASE_URLS:
            endpoint = f"{base_url}/json/stations/byuuid/{quote(uuid_clean)}"
            self._log(f"Sender-Direkt-Abfrage gegen: {base_url} (uuid='{uuid_clean}')")
            try:
                request = Request(endpoint, headers={"User-Agent": USER_AGENT})
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    payload = json.load(response)
            except Exception as err:
                errors.append(f"{base_url} [uuid:{uuid_clean}]: {err}")
                continue

            candidates = self._extract_candidates(payload)
            if candidates:
                best = candidates[0]
                self._log(
                    "Sender-Match (ID): "
                    f"{best.name} | {best.country} | {best.codec} {best.bitrate}kbps | votes={best.votes}"
                )
                return best

        # Fallback: Namens-Suche mit der ID (viele Addons nutzen Slugs als IDs)
        for base_url in RADIO_BROWSER_BASE_URLS:
            endpoint = f"{base_url}/json/stations/byname/{quote(uuid_clean)}?limit=1"
            self._log(f"Sender-ID-Suche (Slug) gegen: {base_url} (id='{uuid_clean}')")
            try:
                request = Request(endpoint, headers={"User-Agent": USER_AGENT})
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    payload = json.load(response)
            except Exception as err:
                errors.append(f"{base_url} [id-slug:{uuid_clean}]: {err}")
                continue

            candidates = self._extract_candidates(payload)
            if candidates:
                best = candidates[0]
                self._log(
                    "Sender-Match (ID-Slug): "
                    f"{best.name} | {best.country} | {best.codec} {best.bitrate}kbps | votes={best.votes}"
                )
                return best

        fallback_station = self._fallback_web_directory_station(uuid_clean)
        if fallback_station:
            self._apply_query_alias_name(fallback_station, uuid_clean)
            self._log(
                "Sender-Match (ID-Web-Fallback): "
                f"{fallback_station.name} | {fallback_station.country or '-'} | "
                f"{fallback_station.codec or '-'} {fallback_station.bitrate}kbps | "
                f"votes={fallback_station.votes}"
            )
            return fallback_station

        if errors:
            raise StationLookupError("Sender-Abfrage fehlgeschlagen: " + " | ".join(errors))
        raise StationLookupError(f"Keinen passenden Sender gefunden für ID '{uuid_clean}'.")

    def _apply_query_alias_name(self, station: StationMatch, query: str) -> None:
        query_tokens = self._build_signature_tokens(query)
        query_alpha_tokens = {token for token in query_tokens if not token.isdigit()}
        if len(query_alpha_tokens) < 2:
            return

        station_name_tokens = self._build_signature_tokens(station.name or "")
        missing_alpha = {token for token in query_alpha_tokens if token not in station_name_tokens}
        if not missing_alpha:
            return

        stream_tokens = self._build_signature_tokens(
            " ".join(
                (
                    station.stream_url or "",
                    station.homepage or "",
                )
            )
        )
        if not all(token in stream_tokens for token in missing_alpha):
            return

        overlap_alpha = {token for token in query_alpha_tokens if token in station_name_tokens}
        if len(overlap_alpha) < 2:
            return

        station.name = re.sub(r"\s+", " ", (query or "").strip())

    def _collect_search_candidates(
        self,
        query: str,
        search_queries: list[str],
        errors: list[str],
        station_id: str = "",
    ) -> tuple[list[StationMatch], int]:
        collected: list[StationMatch] = []
        successful_requests = 0

        for search_query in search_queries:
            query_candidates: list[StationMatch] = []
            for base_url in RADIO_BROWSER_BASE_URLS:
                endpoint = (
                    f"{base_url}/json/stations/search"
                    f"?name={quote(search_query)}"
                    f"&hidebroken=true&limit={RADIO_BROWSER_SEARCH_LOOKUP_LIMIT}&order=votes&reverse=true"
                )
                self._log(f"Sender-Suche (search) gegen: {base_url} (query='{search_query}')")
                try:
                    request = Request(endpoint, headers={"User-Agent": USER_AGENT})
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                        payload = json.load(response)
                except Exception as err:
                    errors.append(f"{base_url} [search:{search_query}]: {err}")
                    continue

                successful_requests += 1
                candidates = self._extract_candidates(payload)
                if candidates:
                    query_candidates.extend(candidates)
                    collected.extend(candidates)
                    break

            if query_candidates and any(
                self._is_confident_station_match(candidate, query, station_id=station_id) for candidate in query_candidates
            ):
                break

        return collected, successful_requests

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

        query_signature = self._build_signature_tokens(query)
        station_signature = self._build_signature_tokens(
            " ".join(
                (
                    station.name or "",
                    station.homepage or "",
                    station.stream_url or "",
                    station.country or "",
                    station.language or "",
                )
            )
        )
        common_tokens = query_signature & station_signature
        missing_tokens = query_signature - common_tokens

        signature_score = 0.0
        for token in common_tokens:
            signature_score += 250 if token.isdigit() else 900
        for token in missing_tokens:
            signature_score -= 80 if token.isdigit() else 200
        if query_signature and not common_tokens:
            signature_score -= 250

        station_name_signature = self._build_signature_tokens(station.name or "")
        common_name_tokens = query_signature & station_name_signature
        name_token_score = 0.0
        for token in common_name_tokens:
            name_token_score += 120 if token.isdigit() else 450

        return (
            (similarity * 500)
            + exact_bonus
            + health_score
            + vote_score
            + bitrate_score
            + signature_score
            + name_token_score
        )

    def _fallback_channel_station_from_anchor(self, query: str, anchor_station: StationMatch, station_id: str = "") -> StationMatch | None:
        homepage = (anchor_station.homepage or "").strip()
        if not is_probable_url(homepage):
            return None

        page_html = self._fetch_text(homepage)
        if not page_html:
            return None

        page_urls = [homepage]
        page_urls.extend(self._extract_channel_page_urls(page_html, homepage))

        channel_candidates: list[StationMatch] = []
        for page_url in page_urls[:STATION_LOOKUP_CHANNEL_FALLBACK_MAX_PAGES]:
            html_text = page_html if page_url == homepage else self._fetch_text(page_url)
            if not html_text:
                continue
            channel_candidates.extend(self._extract_channel_candidates_from_page(html_text, page_url))

        if not channel_candidates:
            return None

        ranked_channels = sorted(
            channel_candidates,
            key=lambda station: self._score_station(station, query),
            reverse=True,
        )
        best = ranked_channels[0]
        best_score = self._score_station(best, query)
        if best_score < STATION_LOOKUP_CHANNEL_FALLBACK_MIN_SCORE:
            return None
        if not self._is_confident_station_match(best, query, station_id=station_id):
            return None
        return best

    def _fallback_stream_from_homepage(self, query: str, anchor_station: StationMatch) -> StationMatch | None:
        homepage = (anchor_station.homepage or "").strip()
        if not is_probable_url(homepage):
            return None

        page_html = self._fetch_text(homepage)
        if not page_html:
            return None

        normalized = (
            html.unescape(page_html)
            .replace("\\/", "/")
            .replace("\\u002f", "/")
            .replace("\\u002F", "/")
            .replace("\\u003a", ":")
            .replace("\\u003A", ":")
        )
        urls = set(re.findall(r"https?://[^\"'\s<>()]+", normalized, flags=re.IGNORECASE))
        if not urls:
            return None

        query_tokens = self._build_signature_tokens(query)
        if not query_tokens:
            return None

        homepage_tokens = self._build_signature_tokens(homepage)
        generic_channel_tokens = {
            "berliner",
            "rundfunk",
            "radio",
            "stream",
            "live",
            "musik",
            "hits",
            "pop",
        }
        channel_tokens = {
            token
            for token in query_tokens
            if not token.isdigit() and len(token) >= 5 and token not in homepage_tokens and token not in generic_channel_tokens
        }

        best_candidate_url = ""
        best_score = float("-inf")
        homepage_base = get_base_domain(homepage)
        for raw_url in urls:
            candidate_url = html.unescape(self._sanitize_candidate_url(raw_url))
            if not candidate_url:
                continue
            if not self._looks_like_stream_pattern(candidate_url):
                continue

            candidate_tokens = self._build_signature_tokens(candidate_url)
            if not candidate_tokens:
                continue
            if channel_tokens and not (channel_tokens & candidate_tokens):
                continue

            common_tokens = query_tokens & candidate_tokens
            if not common_tokens:
                continue

            score = 0.0
            for token in common_tokens:
                score += 60 if token.isdigit() else 220
            for token in (channel_tokens & candidate_tokens):
                score += 450
            if homepage_base and get_base_domain(candidate_url) == homepage_base:
                score += 60

            lower_candidate_url = candidate_url.lower()
            if "mp3-128" in lower_candidate_url:
                score += 20
            elif "mp3-192" in lower_candidate_url or "aac-64" in lower_candidate_url:
                score += 10

            if score > best_score:
                best_score = score
                best_candidate_url = candidate_url

        if not best_candidate_url:
            return None
        if not self._looks_like_stream_endpoint(best_candidate_url):
            return None

        raw_record = dict(anchor_station.raw_record or {})
        raw_record.update(
            {
                "source": "homepage_stream_fallback",
                "anchor_stationuuid": anchor_station.stationuuid,
                "anchor_stream_url": anchor_station.stream_url,
                "stream_url": best_candidate_url,
                "homepage": homepage,
            }
        )

        return StationMatch(
            stationuuid=f"homepage-stream-fallback:{anchor_station.stationuuid or 'unknown'}",
            name=anchor_station.name or query,
            stream_url=best_candidate_url,
            homepage=homepage,
            country=anchor_station.country,
            language=anchor_station.language,
            codec=anchor_station.codec,
            bitrate=anchor_station.bitrate,
            votes=anchor_station.votes,
            lastcheckok=anchor_station.lastcheckok,
            raw_record=raw_record,
        )

    def _extract_channel_page_urls(self, page_html: str, base_url: str) -> list[str]:
        normalized = html.unescape(page_html or "").replace("\\/", "/")
        if not normalized:
            return []

        canonical_match = re.search(
            r"<link[^>]*rel=[\"']canonical[\"'][^>]*href=[\"']([^\"']+)[\"']",
            normalized,
            flags=re.IGNORECASE,
        )
        canonical_url = (canonical_match.group(1) or "").strip() if canonical_match else ""
        join_bases = [base_url]
        if is_probable_url(canonical_url):
            join_bases.insert(0, canonical_url)

        urls = set()
        for match in re.findall(r"(?:href|src)=[\"']([^\"']+)[\"']", normalized, flags=re.IGNORECASE):
            candidate = match.strip()
            if not candidate:
                continue
            for join_base in join_bases:
                absolute = urljoin(join_base, candidate)
                lower = absolute.lower()
                if not lower.endswith(".html"):
                    continue
                if "radioplayer" not in lower and "/radio/player/" not in lower:
                    continue
                urls.add(absolute)

        return sorted(urls)

    def _extract_channel_candidates_from_page(self, page_html: str, base_url: str) -> list[StationMatch]:
        normalized = html.unescape(page_html).replace("\\/", "/")
        blocks = re.findall(
            r"<li\b[^>]*class=[\"'][^\"']*wdrrChannelListChannel[^\"']*[\"'][^>]*>.*?</li>",
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not blocks:
            return []

        stations: list[StationMatch] = []
        seen_stream_urls = set()

        for block in blocks[:STATION_LOOKUP_CHANNEL_FALLBACK_MAX_CHANNELS]:
            lines = [
                self._clean_html_text(match)
                for match in re.findall(
                    r"<span[^>]*class=[\"'][^\"']*\bline\b[^\"']*[\"'][^>]*>(.*?)</span>",
                    block,
                    flags=re.IGNORECASE | re.DOTALL,
                )
            ]
            lines = [line for line in lines if line]
            display_name = " ".join(lines).strip()

            if not display_name:
                link_match = re.search(
                    r"<a\b[^>]*class=[\"'][^\"']*wdrrChannelListStreamLnk[^\"']*[\"'][^>]*>(.*?)</a>",
                    block,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if link_match:
                    display_name = self._clean_html_text(link_match.group(1))

            href_match = re.search(
                r"<a\b[^>]*class=[\"'][^\"']*wdrrChannelListStreamLnk[^\"']*[\"'][^>]*href=[\"']([^\"']+)[\"']",
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            channel_page_url = ""
            if href_match:
                channel_page_url = urljoin(base_url, href_match.group(1).strip())

            asset_url = self._extract_assetjsonp_url(block, base_url)
            if not asset_url:
                continue

            stream_url = self._extract_stream_url_from_assetjsonp(asset_url)
            if not stream_url:
                continue
            if stream_url in seen_stream_urls:
                continue
            seen_stream_urls.add(stream_url)

            station_name = display_name or channel_page_url or stream_url
            stations.append(
                StationMatch(
                    stationuuid=f"channel-fallback:{len(stations) + 1}",
                    name=station_name,
                    stream_url=stream_url,
                    homepage=channel_page_url or base_url,
                    country="",
                    language="",
                    codec="",
                    bitrate=0,
                    votes=0,
                    lastcheckok=1,
                    raw_record={
                        "source": "channel_page_fallback",
                        "base_page": base_url,
                        "channel_page": channel_page_url,
                        "asset_url": asset_url,
                        "stream_url": stream_url,
                        "channel_name": station_name,
                    },
                )
            )
        return stations

    def _extract_assetjsonp_url(self, text: str, base_url: str) -> str:
        match = re.search(r'"url"\s*:\s*"([^"]+\.assetjsonp[^"]*)"', text, flags=re.IGNORECASE)
        if not match:
            return ""
        candidate = (match.group(1) or "").strip()
        if candidate.startswith("//"):
            return f"https:{candidate}"
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
        return urljoin(base_url, candidate)

    def _extract_stream_url_from_assetjsonp(self, asset_url: str) -> str:
        if not asset_url:
            return ""

        request = Request(asset_url, headers={"User-Agent": USER_AGENT})
        payload = ""
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = decode_text_bytes(
                    response.read(500000),
                    content_type=response.headers.get("Content-Type") or "",
                )
        except Exception as err:
            if isinstance(getattr(err, "reason", None), ssl.SSLCertVerificationError):
                context = ssl._create_unverified_context()
                try:
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
                        payload = decode_text_bytes(
                            response.read(500000),
                            content_type=response.headers.get("Content-Type") or "",
                        )
                except Exception:
                    return ""
            else:
                return ""

        match = re.search(r'"audioURL"\s*:\s*"([^"]+)"', payload, flags=re.IGNORECASE)
        if not match:
            return ""
        stream_url = (match.group(1) or "").strip().replace("\\/", "/")
        if stream_url.startswith("//"):
            return f"https:{stream_url}"
        if stream_url.startswith("http://") or stream_url.startswith("https://"):
            return stream_url
        return ""

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

        tokens = split_search_tokens(normalized)
        if not tokens:
            return []

        token_groups = self._build_token_groups(tokens)
        folded_normalized = self._fold_german_umlauts(normalized).lower()
        if folded_normalized and folded_normalized != normalized:
            folded_tokens = split_search_tokens(folded_normalized)
            if folded_tokens:
                token_groups.extend(self._build_token_groups(folded_tokens))

        slugs: list[str] = []
        seen = set()
        for group in token_groups:
            for variant_tokens in self._build_token_variants(
                group,
                min_tokens=STATION_LOOKUP_MIN_TOKENS_PER_VARIANT,
                max_variants=STATION_LOOKUP_MAX_SLUG_VARIANTS,
            ):
                for joiner in ("", "-"):
                    slug = joiner.join(variant_tokens).strip("-")
                    if len(slug) < STATION_LOOKUP_SLUG_MIN_LENGTH:
                        continue
                    if slug in seen:
                        continue
                    seen.add(slug)
                    slugs.append(slug)
                    if len(slugs) >= STATION_LOOKUP_MAX_SLUG_VARIANTS:
                        return slugs
        return slugs

    def _build_lookup_queries(self, query: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", (query or "").strip())
        if not normalized:
            return []

        variants: list[str] = []
        seen = set()
        tokens = split_search_tokens(normalized)
        token_groups = self._build_token_groups(tokens)
        folded_normalized = re.sub(r"\s+", " ", self._fold_german_umlauts(normalized).strip())
        if folded_normalized and folded_normalized.lower() != normalized.lower():
            folded_tokens = split_search_tokens(folded_normalized)
            if folded_tokens:
                token_groups.extend(self._build_token_groups(folded_tokens))
        original_key = normalized.lower()
        original_tokens = [token.lower() for token in tokens]

        for group in token_groups:
            for variant_tokens in self._build_token_variants(
                group,
                min_tokens=STATION_LOOKUP_MIN_TOKENS_PER_VARIANT,
                max_variants=STATION_LOOKUP_MAX_QUERY_VARIANTS,
            ):
                if self._should_skip_short_variant(variant_tokens, original_tokens):
                    continue
                candidate = " ".join(variant_tokens).strip()
                if len(candidate) < STATION_LOOKUP_MIN_QUERY_LENGTH and candidate.lower() != original_key:
                    continue
                key = candidate.lower()
                if key in seen:
                    continue
                seen.add(key)
                variants.append(candidate)
                for dotted_variant in self._expand_frequency_decimal_variants(candidate):
                    dotted_key = dotted_variant.lower()
                    if dotted_key in seen:
                        continue
                    seen.add(dotted_key)
                    variants.append(dotted_variant)
                    if len(variants) >= STATION_LOOKUP_MAX_QUERY_VARIANTS:
                        return variants
                for compound_variant in self._expand_compound_token_variants(candidate):
                    compound_key = compound_variant.lower()
                    if compound_key in seen:
                        continue
                    seen.add(compound_key)
                    variants.append(compound_variant)
                    if len(variants) >= STATION_LOOKUP_MAX_QUERY_VARIANTS:
                        return variants
                compacted = self._compact_alpha_digit_tokens(candidate)
                if compacted and compacted.lower() not in seen:
                    seen.add(compacted.lower())
                    variants.append(compacted)
                    for dotted_variant in self._expand_frequency_decimal_variants(compacted):
                        dotted_key = dotted_variant.lower()
                        if dotted_key in seen:
                            continue
                        seen.add(dotted_key)
                        variants.append(dotted_variant)
                        if len(variants) >= STATION_LOOKUP_MAX_QUERY_VARIANTS:
                            return variants
                    for compound_variant in self._expand_compound_token_variants(compacted):
                        compound_key = compound_variant.lower()
                        if compound_key in seen:
                            continue
                        seen.add(compound_key)
                        variants.append(compound_variant)
                        if len(variants) >= STATION_LOOKUP_MAX_QUERY_VARIANTS:
                            return variants
                if len(variants) >= STATION_LOOKUP_MAX_QUERY_VARIANTS:
                    return variants

        if not variants:
            return [normalized]
        return variants

    def _build_search_queries(self, query: str, lookup_queries: list[str]) -> list[str]:
        normalized = re.sub(r"\s+", " ", (query or "").strip().lower())
        if not normalized:
            return []

        queries: list[str] = []
        seen = set()

        def add(value: str) -> None:
            key = (value or "").strip().lower()
            if not key or key in seen:
                return
            seen.add(key)
            queries.append(value.strip())

        max_lookup_seed = max(1, STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS // 2)
        for lookup_query in lookup_queries[:max_lookup_seed]:
            add(lookup_query)
            if len(queries) >= STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS:
                return queries

        folded_normalized = self._fold_german_umlauts(normalized).lower()
        if folded_normalized and folded_normalized != normalized:
            add(folded_normalized)
            if len(queries) >= STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS:
                return queries

        tokens = split_search_tokens(normalized)
        if not tokens:
            return queries

        token_sets = [tokens]
        if folded_normalized and folded_normalized != normalized:
            folded_tokens = split_search_tokens(folded_normalized)
            if folded_tokens:
                token_sets.append(folded_tokens)

        for token_set in token_sets:
            mapped_tokens = [STATION_LOOKUP_NUMBER_TOKEN_MAP.get(token, token) for token in token_set]
            filtered_tokens = [
                token
                for token in mapped_tokens
                if (
                    token not in STATION_LOOKUP_IGNORED_TOKENS
                    and token not in STATION_LOOKUP_SKIP_PREFIX_TOKENS
                    and (
                        len(token) >= STATION_LOOKUP_SEARCH_MIN_TOKEN_LENGTH
                        or (token.isdigit() and len(token) >= 2)
                        or self._is_significant_short_token(token)
                    )
                )
            ]

            if not filtered_tokens:
                continue

            add(" ".join(filtered_tokens))
            if len(filtered_tokens) >= 2:
                add(" ".join(filtered_tokens[-2:]))
            if len(filtered_tokens) >= 3:
                add(" ".join(filtered_tokens[-3:]))
            for token in filtered_tokens:
                add(token)
                if len(queries) >= STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS:
                    return queries

        return queries[:STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS]

    def _is_confident_search_match(self, station: StationMatch, query: str) -> bool:
        query_signature = self._build_signature_tokens(query)
        if not query_signature:
            return True

        station_signature = self._build_signature_tokens(
            " ".join(
                (
                    station.name or "",
                    station.homepage or "",
                    station.stream_url or "",
                    station.country or "",
                    station.language or "",
                )
            )
        )
        common_tokens = query_signature & station_signature
        if not common_tokens:
            return False

        query_alpha = {token for token in query_signature if not token.isdigit()}
        common_alpha = {token for token in common_tokens if not token.isdigit()}
        if len(query_alpha) >= 2 and len(common_alpha) < 2:
            return False
        return True

    def _is_confident_station_match(self, station: StationMatch, query: str, station_id: str = "") -> bool:
        if not self._is_confident_search_match(station, query):
            return False
        source_type = str(station.raw_record.get("source") or "").strip().lower()
        if source_type != "web_directory_fallback" and not station_id and self._has_stream_channel_conflict(station, query):
            if self._has_strong_name_equivalence(station.name or "", query):
                query_tokens_with_pos = self._build_query_tokens_for_strict_match(query)
                if len(query_tokens_with_pos) < STATION_LOOKUP_STRICT_MIN_QUERY_TOKENS:
                    return self._is_short_query_name_compatible(station.name or "", query)
                return True
            return False

        query_tokens_with_pos = self._build_query_tokens_for_strict_match(query)
        if len(query_tokens_with_pos) < STATION_LOOKUP_STRICT_MIN_QUERY_TOKENS:
            return self._is_short_query_name_compatible(station.name or "", query)

        station_tokens = self._build_station_tokens_for_strict_match(station)
        missing = [(token, pos) for token, pos in query_tokens_with_pos if token not in station_tokens]
        if not missing:
            return True

        if self._has_optional_trailing_region_suffix(query_tokens_with_pos, missing, station_tokens):
            return True

        missing_alpha = [token for token, _ in missing if not token.isdigit()]
        if not missing_alpha:
            query_alpha_tokens = [token for token, _ in query_tokens_with_pos if not token.isdigit()]
            if len(query_alpha_tokens) >= 3:
                alpha_overlap = {token for token in query_alpha_tokens if token in station_tokens}
                if len(alpha_overlap) >= 3:
                    return True

        if len(missing) == 1:
            missing_token, missing_pos = missing[0]
            first_query_pos = query_tokens_with_pos[0][1]
            if (
                missing_pos == first_query_pos
                and missing_token in STATION_LOOKUP_OPTIONAL_PREFIX_TOKENS
                and missing_token not in STATION_LOOKUP_SIGNIFICANT_SHORT_TOKENS
            ):
                return True
        if self._has_strong_name_equivalence(station.name or "", query):
            return True
        return False

    def _has_optional_trailing_region_suffix(
        self,
        query_tokens_with_pos: list[tuple[str, int]],
        missing: list[tuple[str, int]],
        station_tokens: set[str],
    ) -> bool:
        missing_alpha = [token for token, _ in missing if not token.isdigit()]
        if not missing_alpha:
            return False
        if any(token not in STATION_LOOKUP_OPTIONAL_SUFFIX_TOKENS for token in missing_alpha):
            return False

        trailing_missing = query_tokens_with_pos[-len(missing) :]
        if trailing_missing != missing:
            return False

        overlap_alpha = {
            token
            for token, _ in query_tokens_with_pos[:-len(missing)]
            if not token.isdigit() and token in station_tokens
        }
        return len(overlap_alpha) >= STATION_LOOKUP_STRICT_MIN_QUERY_TOKENS

    def _compact_compare_key(self, value: str) -> str:
        text = (value or "").strip().lower()
        if not text:
            return ""
        for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
            text = text.replace(src, dst)
        return re.sub(r"[^a-z0-9]+", "", text)

    def _is_allowed_short_query_affix(self, extra: str) -> bool:
        raw = (extra or "").strip().lower()
        if not raw:
            return True
        if re.fullmatch(r"\d{2,3}(aac|mp3|opus)?", raw):
            return True
        if raw.isdigit():
            return False
        stripped = re.sub(r"(mp3|aac|opus|stream|live|radio|fm|am|hq|lq|dlf|ukw)+", "", raw)
        if not stripped:
            return False
        return stripped in {"orf"}

    def _is_short_query_name_compatible(self, station_name: str, query: str) -> bool:
        query_compact = self._compact_compare_key(query)
        station_compact = self._compact_compare_key(station_name)
        if not query_compact or not station_compact:
            return True
        if station_compact == query_compact:
            return True

        if len(query_compact) >= 3 and query_compact in station_compact:
            idx = station_compact.find(query_compact)
            extra = station_compact[:idx] + station_compact[idx + len(query_compact) :]
            return self._is_allowed_short_query_affix(extra)
        if len(station_compact) >= 3 and station_compact in query_compact:
            idx = query_compact.find(station_compact)
            extra = query_compact[:idx] + query_compact[idx + len(station_compact) :]
            return self._is_allowed_short_query_affix(extra)
        return False

    def _has_strong_name_equivalence(self, station_name: str, query: str) -> bool:
        def small_affix(value: str) -> bool:
            extra = (value or "").strip().lower()
            if not extra:
                return True
            extra = re.sub(r"\d+", "", extra)
            extra = re.sub(r"(mp3|aac|opus|kbit|kbps|stream|audio|hq|lq|dlf)+", "", extra)
            return len(extra) <= 8

        station_compact = self._compact_compare_key(station_name)
        query_compact = self._compact_compare_key(query)
        if not station_compact or not query_compact:
            return False
        if station_compact == query_compact:
            return True

        # Allow small affix extensions around the exact compact query token,
        # e.g. "hitradiooe3" vs. "orfhitradiooe3hq".
        if len(query_compact) >= 6 and query_compact in station_compact:
            extra = station_compact.replace(query_compact, "", 1)
            if small_affix(extra):
                return True
        if len(station_compact) >= 6 and station_compact in query_compact:
            extra = query_compact.replace(station_compact, "", 1)
            if small_affix(extra):
                return True
        return False

    def _has_stream_channel_conflict(self, station: StationMatch, query: str) -> bool:
        query_tokens = self._build_signature_tokens(query)
        if not query_tokens:
            return False

        name_tokens = self._build_signature_tokens(station.name or "")
        stream_tokens = self._build_signature_tokens(
            " ".join(
                (
                    station.stream_url or "",
                    station.homepage or "",
                )
            )
        )
        homepage_tokens = self._build_signature_tokens(station.homepage or "")

        # Focus only on channel-descriptor-like tokens from the query that the station name claims,
        # excluding generic homepage brand tokens.
        focus_tokens = {
            token
            for token in (query_tokens & name_tokens)
            if not token.isdigit() and len(token) >= 5 and token not in homepage_tokens
        }
        if not focus_tokens:
            return False

        missing_focus_tokens = {token for token in focus_tokens if token not in stream_tokens}
        if not missing_focus_tokens:
            return False

        generic_stream_tokens = {
            "http",
            "https",
            "www",
            "stream",
            "live",
            "audio",
            "radio",
            "mp3",
            "aac",
            "m3u",
            "m3u8",
            "edge",
            "frontend",
            "dispatcher",
            "rndfnk",
            "icecast",
            "icecastssl",
        }
        conflicting_stream_tokens = {
            token
            for token in stream_tokens
            if (
                token not in query_tokens
                and token not in homepage_tokens
                and token not in generic_stream_tokens
                and not token.isdigit()
                and len(token) >= 5
            )
        }
        return bool(conflicting_stream_tokens)

    def _build_query_tokens_for_strict_match(self, value: str) -> list[tuple[str, int]]:
        raw_tokens = split_search_tokens(value)
        if not raw_tokens:
            return []

        tokens_with_pos: list[tuple[str, int]] = []
        for pos, raw_token in enumerate(raw_tokens):
            token = STATION_LOOKUP_NUMBER_TOKEN_MAP.get(raw_token, raw_token)
            if token in STATION_LOOKUP_IGNORED_TOKENS or token in STATION_LOOKUP_SKIP_PREFIX_TOKENS:
                continue
            if token.isdigit():
                if len(token) <= 2:
                    tokens_with_pos.append((token, pos))
                continue
            if len(token) >= 3 or self._is_significant_short_token(token):
                tokens_with_pos.append((token, pos))
        return tokens_with_pos

    def _is_single_token_lookup_query(self, query: str) -> bool:
        tokens_with_pos = self._build_query_tokens_for_strict_match(query)
        if len(tokens_with_pos) != 1:
            return False
        token = tokens_with_pos[0][0]
        if token.isdigit():
            return False
        if any(char.isdigit() for char in token):
            return True
        return len(token) <= 5

    def _build_station_tokens_for_strict_match(self, station: StationMatch) -> set[str]:
        text = " ".join(
            (
                station.name or "",
                station.homepage or "",
                station.stream_url or "",
                station.country or "",
                station.language or "",
            )
        )
        tokens = self._build_signature_tokens(text)
        raw_tokens = split_search_tokens(text)
        if not raw_tokens:
            return tokens
        for raw_token in raw_tokens:
            token = STATION_LOOKUP_NUMBER_TOKEN_MAP.get(raw_token, raw_token)
            if self._is_significant_short_token(token):
                tokens.add(token)
        return tokens

    def _build_token_groups(self, tokens: list[str]) -> list[list[str]]:
        if not tokens:
            return []

        groups = [tokens]
        stripped_suffix = self._strip_optional_trailing_tokens(tokens)
        if stripped_suffix and stripped_suffix != tokens:
            groups.append(stripped_suffix)
        stripped = [token for token in tokens if token.lower() not in STATION_LOOKUP_IGNORED_TOKENS]
        if stripped and stripped != tokens:
            groups.append(stripped)
            stripped_suffix = self._strip_optional_trailing_tokens(stripped)
            if stripped_suffix and stripped_suffix != stripped:
                groups.append(stripped_suffix)

        mapped_groups = []
        for group in groups:
            mapped = [STATION_LOOKUP_NUMBER_TOKEN_MAP.get(token.lower(), token) for token in group]
            if mapped != group:
                mapped_groups.append(mapped)
        groups.extend(mapped_groups)

        deduped = []
        seen = set()
        for group in groups:
            key = tuple(group)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(group)
        return deduped

    def _strip_optional_trailing_tokens(self, tokens: list[str]) -> list[str]:
        if len(tokens) <= STATION_LOOKUP_MIN_TOKENS_PER_VARIANT:
            return tokens

        end = len(tokens)
        while end > STATION_LOOKUP_MIN_TOKENS_PER_VARIANT:
            token = tokens[end - 1].lower()
            if token not in STATION_LOOKUP_OPTIONAL_SUFFIX_TOKENS:
                break
            end -= 1
        return tokens[:end]

    def _build_token_variants(self, tokens: list[str], min_tokens: int, max_variants: int) -> list[list[str]]:
        if not tokens:
            return []

        min_window_size = min_tokens if len(tokens) >= min_tokens else len(tokens)
        variants: list[list[str]] = []
        seen = set()
        for window_size in range(len(tokens), min_window_size - 1, -1):
            for start in range(0, len(tokens) - window_size + 1):
                variant_tuple = tuple(tokens[start : start + window_size])
                if variant_tuple in seen:
                    continue
                seen.add(variant_tuple)
                variants.append(list(variant_tuple))
                if max_variants > 0 and len(variants) >= max_variants:
                    return variants
        return variants

    def _should_skip_short_variant(self, variant_tokens: list[str], original_tokens: list[str]) -> bool:
        lowered = [token.lower() for token in variant_tokens]
        if lowered == original_tokens:
            return False
        if lowered and lowered[0] in STATION_LOOKUP_SKIP_PREFIX_TOKENS:
            return True
        return False

    def _compact_alpha_digit_tokens(self, value: str) -> str:
        compacted = re.sub(r"\b([^\W\d_]+)\s+([0-9]{1,2})\b", r"\1\2", value, flags=re.IGNORECASE).strip()
        if compacted.lower() == value.lower():
            return ""
        return compacted

    def _expand_frequency_decimal_variants(self, value: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", (value or "").strip())
        if not normalized:
            return []

        candidates = [
            re.sub(
                r"\b([^\W\d_]{2,})([0-9]{3})\b",
                lambda match: f"{match.group(1)} {match.group(2)[:2]}.{match.group(2)[2]}",
                normalized,
                flags=re.IGNORECASE,
            ).strip(),
            re.sub(
                r"\b([^\W\d_]{2,})\s+([0-9]{3})\b",
                lambda match: f"{match.group(1)} {match.group(2)[:2]}.{match.group(2)[2]}",
                normalized,
                flags=re.IGNORECASE,
            ).strip(),
        ]

        variants: list[str] = []
        seen = {normalized.lower()}
        for candidate in candidates:
            key = candidate.lower()
            if not candidate or key in seen:
                continue
            seen.add(key)
            variants.append(candidate)
        return variants

    def _expand_compound_token_variants(self, value: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
        tokens = split_search_tokens(normalized)
        if len(tokens) != 1:
            return []
        token = tokens[0]
        if len(token) < 6:
            return []

        fragment_map = {
            "rheinlandpfalz": "rheinland pfalz",
            "badenwuerttemberg": "baden wuerttemberg",
            "sachsenanhalt": "sachsen anhalt",
            "kulturradio": "kultur radio",
            "saarlandwelle": "saarland welle",
            "europawelle": "europa welle",
            "classicrock": "classic rock",
        }
        prefix_tokens = (
            "deutschlandfunk",
            "antenne",
            "radio",
            "energy",
            "bigfm",
            "sunshine",
            "hitradio",
            "absolut",
        )
        suffix_tokens = (
            "kultur",
            "nova",
            "info",
            "aktuell",
            "hamburg",
            "berlin",
            "koeln",
            "muenchen",
            "chillout",
            "classicrock",
            "relax",
            "hot",
        )

        def split_fragment(fragment: str) -> str:
            clean = fragment_map.get(fragment, fragment)
            return re.sub(r"\s+", " ", clean).strip()

        variants: list[str] = []
        seen = {normalized}

        def add(candidate: str) -> None:
            clean = re.sub(r"\s+", " ", (candidate or "").strip().lower())
            if not clean or clean in seen:
                return
            seen.add(clean)
            variants.append(clean)

        # Examples: swr1rheinlandpfalz, ndr3, sr2kulturradio
        mixed_match = re.match(r"^([^\W\d_]{2,})([0-9]{1,2})([^\W\d_]{0,})$", token, flags=re.IGNORECASE)
        if mixed_match:
            prefix = mixed_match.group(1)
            number = mixed_match.group(2)
            tail = split_fragment(mixed_match.group(3))
            if tail:
                add(f"{prefix}{number} {tail}")
                add(f"{prefix} {number} {tail}")
            else:
                add(f"{prefix} {number}")

        for prefix in prefix_tokens:
            if token.startswith(prefix) and len(token) > len(prefix) + 2:
                rest = split_fragment(token[len(prefix) :])
                add(f"{prefix} {rest}")

        for suffix in suffix_tokens:
            if token.endswith(suffix) and len(token) > len(suffix) + 2:
                head = split_fragment(token[: -len(suffix)])
                add(f"{head} {suffix}")

        return variants

    def _build_signature_tokens(self, value: str) -> set[str]:
        raw_tokens = split_search_tokens(value)
        if not raw_tokens:
            return set()

        generic_stopwords = {
            "http",
            "https",
            "www",
            "com",
            "net",
            "org",
            "html",
            "php",
            "mp3",
            "aac",
            "ogg",
            "m3u",
            "m3u8",
            "pls",
            "xspf",
            "stream",
            "live",
        }

        tokens = set()
        for raw_token in raw_tokens:
            mapped = STATION_LOOKUP_NUMBER_TOKEN_MAP.get(raw_token, raw_token)
            if (
                mapped in STATION_LOOKUP_IGNORED_TOKENS
                or mapped in STATION_LOOKUP_SKIP_PREFIX_TOKENS
                or mapped in generic_stopwords
            ):
                continue
            if mapped.isdigit():
                if len(mapped) <= 2:
                    tokens.add(mapped)
                continue
            if len(mapped) >= 3 or self._is_significant_short_token(mapped):
                tokens.add(mapped)
        return tokens

    def _is_significant_short_token(self, token: str) -> bool:
        if token in STATION_LOOKUP_SIGNIFICANT_SHORT_TOKENS:
            return True
        return is_mixed_alnum_token(token, min_length=2)

    def _fetch_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read(900000)
                return decode_text_bytes(payload, content_type=response.headers.get("Content-Type") or "")
        except Exception as err:
            if isinstance(getattr(err, "reason", None), ssl.SSLCertVerificationError):
                context = ssl._create_unverified_context()
                try:
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
                        payload = response.read(900000)
                        return decode_text_bytes(payload, content_type=response.headers.get("Content-Type") or "")
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
            token for token in split_search_tokens(query) if len(token) >= 3 and token not in {"radio", "stream", "live"}
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

    def _clean_html_text(self, value: str) -> str:
        text = html.unescape(value or "")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _sanitize_candidate_url(self, value: str) -> str:
        return value.strip().rstrip("\\").rstrip(",;)}]\"'")
