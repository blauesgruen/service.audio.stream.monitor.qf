"""Read ICY metadata from a resolved stream URL."""

from __future__ import annotations

import re
import ssl
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import REQUEST_TIMEOUT_SECONDS, STREAM_READ_BYTES, USER_AGENT
from .models import SongInfo
from .utils import has_unicode_letter, normalize_for_token_search


class MetadataError(Exception):
    pass


class SongMetadataFetcher:
    def __init__(self, log) -> None:
        self._log = log

    def fetch(self, stream_url: str) -> SongInfo:
        req = Request(
            stream_url,
            headers={
                "User-Agent": USER_AGENT,
                "Icy-MetaData": "1",
                "Connection": "close",
            },
        )

        try:
            response_ctx = urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS)
        except URLError as err:
            if isinstance(err.reason, ssl.SSLCertVerificationError):
                context = ssl._create_unverified_context()
                response_ctx = urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS, context=context)
            else:
                raise

        with response_ctx as response:
            metaint_raw = response.headers.get("icy-metaint")
            if not metaint_raw:
                raise MetadataError("Stream liefert kein ICY-Metadaten-Intervall (icy-metaint).")

            try:
                metaint = int(metaint_raw)
            except ValueError as exc:
                raise MetadataError(f"Ungültiger icy-metaint-Wert: {metaint_raw}") from exc

            self._log(f"ICY metaint erkannt: {metaint}")

            if metaint > STREAM_READ_BYTES:
                raise MetadataError(
                    f"icy-metaint={metaint} ist zu groß für schnelles Polling (Limit={STREAM_READ_BYTES})."
                )

            response.read(metaint)

            length_byte = response.read(1)
            if not length_byte:
                raise MetadataError("Kein Metadatenblock gefunden.")

            metadata_length = length_byte[0] * 16
            if metadata_length == 0:
                raise MetadataError("Metadatenblock ist leer (keine Songinfo im aktuellen Slot).")

            metadata_block = response.read(metadata_length)
            raw_text = metadata_block.decode("utf-8", errors="ignore").strip("\x00")
            stream_title = self._extract_stream_title(raw_text)
            source_headers = {key: value for key, value in response.headers.items()}
            station_name = response.headers.get("icy-name") or ""
            artist, title = self._split_artist_title(stream_title, station_name)

            if not stream_title:
                raise MetadataError("ICY-Metadaten vorhanden, aber kein StreamTitle gefunden.")

            return SongInfo(
                stream_title=stream_title,
                raw_metadata=raw_text,
                artist=artist,
                title=title,
                source_kind="stream_icy",
                source_url=stream_url,
                source_headers=source_headers,
            )

    def _extract_stream_title(self, raw_metadata: str) -> str:
        for part in raw_metadata.split(";"):
            clean = part.strip()
            if clean.lower().startswith("streamtitle="):
                _, _, value = clean.partition("=")
                return value.strip().strip("'").strip()
        return ""

    def _split_artist_title(self, stream_title: str, station_name: str = "") -> tuple[str, str]:
        if " - " in stream_title:
            artist, title = stream_title.split(" - ", 1)
            artist = artist.strip()
            title = title.strip()
            if self._looks_like_song_pair(artist, title, station_name):
                return artist, title

        match_de = re.match(
            r"^(?P<title>.+?)\s+von\s+(?P<artist>.+?)\s+jetzt\b.*$",
            stream_title.strip(),
            flags=re.IGNORECASE,
        )
        if match_de:
            artist = match_de.group("artist").strip()
            title = match_de.group("title").strip()
            if self._looks_like_song_pair(artist, title, station_name):
                return artist, title

        match_en = re.match(
            r"^(?P<title>.+?)\s+by\s+(?P<artist>.+?)\s+now\b.*$",
            stream_title.strip(),
            flags=re.IGNORECASE,
        )
        if match_en:
            artist = match_en.group("artist").strip()
            title = match_en.group("title").strip()
            if self._looks_like_song_pair(artist, title, station_name):
                return artist, title

        return "", stream_title.strip()

    def _looks_like_song_pair(self, artist: str, title: str, station_name: str = "") -> bool:
        if not artist or not title:
            return False

        artist_lower = artist.lower()
        title_lower = title.lower()

        # Ignore ID-like payloads such as "281085 - 393065".
        if not has_unicode_letter(artist_lower):
            return False
        if (
            re.fullmatch(r"[0-9][0-9 _.-]{2,}", artist_lower)
            and re.fullmatch(r"[0-9][0-9 _.-]{2,}", title_lower)
        ):
            return False

        blocked_artist_markers = (
            "mdr",
            "radio",
            "jump",
            "studio",
            "livestream",
            "traffic",
            "verkehr",
        )
        if any(marker in artist_lower for marker in blocked_artist_markers):
            return False

        blocked_title_markers = (
            "anruf",
            "mikrofon",
            "studio",
            "verkehr",
            "nachrichten",
        )
        if any(marker in title_lower for marker in blocked_title_markers):
            return False

        station_tokens = self._tokenize(station_name)
        if station_tokens and (self._tokenize(artist) & station_tokens):
            return False

        return True

    def _tokenize(self, value: str) -> set[str]:
        cleaned = normalize_for_token_search(value)
        return {token for token in cleaned.split() if len(token) >= 3}
