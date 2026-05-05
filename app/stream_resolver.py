"""Resolve radio URLs to the original stream endpoint."""

from __future__ import annotations

from pathlib import Path
import ssl
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import (
    MAX_REDIRECTS,
    PLAYLIST_READ_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    SUPPORTED_PLAYLIST_CONTENT_TYPES,
    SUPPORTED_PLAYLIST_SUFFIXES,
    USER_AGENT,
)
from .models import ResolvedStream
from .utils import decode_text_bytes, is_probable_url


class StreamResolveError(Exception):
    pass


class StreamResolver:
    def __init__(self, log) -> None:
        self._log = log

    def resolve(self, input_url: str, original_input: str | None = None) -> ResolvedStream:
        current_url = input_url.strip()
        if not current_url:
            raise StreamResolveError("Bitte eine URL eingeben.")

        was_playlist = False
        content_type = ""

        for step in range(MAX_REDIRECTS):
            self._log(f"Resolve step {step + 1}: {current_url}")
            final_url, content_type, payload = self._fetch_probe(current_url)

            if self._is_playlist(final_url, content_type):
                was_playlist = True
                playlist_url = final_url
                next_url = self._extract_stream_from_playlist(payload, playlist_url)
                if not next_url:
                    raise StreamResolveError("Playlist gefunden, aber keine Stream-URL erkannt.")
                self._log(f"Playlist erkannt -> erste Stream-URL: {next_url}")
                current_url = next_url
                continue

            return ResolvedStream(
                input_url=(original_input or input_url),
                resolved_url=current_url,
                delivery_url=final_url,
                content_type=content_type,
                was_playlist=was_playlist,
            )

        raise StreamResolveError("Zu viele Redirects/Playlist-Schritte.")

    def _fetch_probe(self, url: str) -> tuple[str, str, bytes]:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()

                payload = b""
                if self._is_playlist(final_url, content_type):
                    payload = response.read(PLAYLIST_READ_BYTES)
        except URLError as err:
            if isinstance(err.reason, ssl.SSLCertVerificationError):
                context = ssl._create_unverified_context()
                with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
                    final_url = response.geturl()
                    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()

                    payload = b""
                    if self._is_playlist(final_url, content_type):
                        payload = response.read(PLAYLIST_READ_BYTES)
            else:
                raise

        return final_url, content_type, payload

    def _is_playlist(self, url: str, content_type: str) -> bool:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in SUPPORTED_PLAYLIST_SUFFIXES:
            return True
        if content_type in SUPPORTED_PLAYLIST_CONTENT_TYPES:
            return True
        return False

    def _extract_stream_from_playlist(self, payload: bytes, playlist_url: str) -> Optional[str]:
        text = decode_text_bytes(payload)

        m3u_url = self._extract_from_m3u(text, playlist_url)
        if m3u_url:
            return m3u_url

        pls_url = self._extract_from_pls(text, playlist_url)
        if pls_url:
            return pls_url

        xspf_url = self._extract_from_xspf(text, playlist_url)
        if xspf_url:
            return xspf_url

        return None

    def _extract_from_m3u(self, text: str, base_url: str) -> Optional[str]:
        for line in text.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            if self._looks_like_url(clean):
                return urljoin(base_url, clean)
        return None

    def _extract_from_pls(self, text: str, base_url: str) -> Optional[str]:
        for line in text.splitlines():
            clean = line.strip()
            if not clean.lower().startswith("file"):
                continue
            _, _, value = clean.partition("=")
            candidate = value.strip()
            if self._looks_like_url(candidate):
                return urljoin(base_url, candidate)
        return None

    def _extract_from_xspf(self, text: str, base_url: str) -> Optional[str]:
        start_tag = "<location>"
        end_tag = "</location>"
        start = text.lower().find(start_tag)
        if start < 0:
            return None
        start_content = start + len(start_tag)
        end = text.lower().find(end_tag, start_content)
        if end < 0:
            return None
        candidate = text[start_content:end].strip()
        if self._looks_like_url(candidate):
            return urljoin(base_url, candidate)
        return None

    def _looks_like_url(self, value: str) -> bool:
        return is_probable_url(value)
