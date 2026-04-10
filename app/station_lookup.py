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

    def find_best_match(self, query: str) -> StationMatch:
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
                    break
            if collected:
                break

        if not collected:
            for search_query in search_queries:
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
                        collected.extend(candidates)
                        used_search_fallback = True
                        break
                if collected:
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

        if not self._is_confident_station_match(best, query_clean):
            if used_search_fallback:
                self._log(
                    "Search-Fallback Treffer verworfen (zu geringe Token-Deckung): "
                    f"{best.name}"
                )
            else:
                self._log(
                    "Sender-Match verworfen (zu geringe Token-Deckung): "
                    f"{best.name}"
                )
            channel_fallback_station = self._fallback_channel_station_from_anchor(query_clean, best)
            if channel_fallback_station:
                self._log(
                    "Sender-Match (Channel-Fallback): "
                    f"{channel_fallback_station.name} | {channel_fallback_station.country or '-'} | "
                    f"{channel_fallback_station.codec or '-'} {channel_fallback_station.bitrate}kbps | "
                    f"votes={channel_fallback_station.votes}"
                )
                return channel_fallback_station
            fallback_station = self._fallback_web_directory_station(query_clean)
            if fallback_station and self._is_confident_station_match(fallback_station, query_clean):
                self._log(
                    "Sender-Match (Web-Fallback): "
                    f"{fallback_station.name} | {fallback_station.country or '-'} | "
                    f"{fallback_station.codec or '-'} {fallback_station.bitrate}kbps | "
                    f"votes={fallback_station.votes}"
                )
                return fallback_station
            raise StationLookupError(f"Keinen passenden Sender gefunden für '{query_clean}'.")

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

        return (similarity * 500) + exact_bonus + health_score + vote_score + bitrate_score + signature_score

    def _fallback_channel_station_from_anchor(self, query: str, anchor_station: StationMatch) -> StationMatch | None:
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
        if not self._is_confident_station_match(best, query):
            return None
        return best

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
                payload = response.read(500000).decode("utf-8", errors="ignore")
        except Exception as err:
            if isinstance(getattr(err, "reason", None), ssl.SSLCertVerificationError):
                context = ssl._create_unverified_context()
                try:
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
                        payload = response.read(500000).decode("utf-8", errors="ignore")
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
                compacted = self._compact_alpha_digit_tokens(candidate)
                if compacted and compacted.lower() not in seen:
                    seen.add(compacted.lower())
                    variants.append(compacted)
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

        for lookup_query in lookup_queries:
            add(lookup_query)
            if len(queries) >= STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS:
                return queries

        tokens = split_search_tokens(normalized)
        if not tokens:
            return queries

        mapped_tokens = [STATION_LOOKUP_NUMBER_TOKEN_MAP.get(token, token) for token in tokens]
        filtered_tokens = [
            token
            for token in mapped_tokens
            if (
                token not in STATION_LOOKUP_IGNORED_TOKENS
                and token not in STATION_LOOKUP_SKIP_PREFIX_TOKENS
                and (
                    len(token) >= STATION_LOOKUP_SEARCH_MIN_TOKEN_LENGTH
                    or self._is_significant_short_token(token)
                )
            )
        ]

        if filtered_tokens:
            add(" ".join(filtered_tokens))
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

    def _is_confident_station_match(self, station: StationMatch, query: str) -> bool:
        if not self._is_confident_search_match(station, query):
            return False

        query_tokens_with_pos = self._build_query_tokens_for_strict_match(query)
        if len(query_tokens_with_pos) < STATION_LOOKUP_STRICT_MIN_QUERY_TOKENS:
            return True

        station_tokens = self._build_station_tokens_for_strict_match(station)
        missing = [(token, pos) for token, pos in query_tokens_with_pos if token not in station_tokens]
        if not missing:
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
        return False

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
        stripped = [token for token in tokens if token.lower() not in STATION_LOOKUP_IGNORED_TOKENS]
        if stripped and stripped != tokens:
            groups.append(stripped)

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
