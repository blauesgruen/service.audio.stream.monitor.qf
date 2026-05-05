"""Shared utility helpers."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .config import NON_ORIGIN_ASSET_BASE_DOMAINS, NON_ORIGIN_DIRECTORY_BASE_DOMAINS

TOKEN_SEPARATOR_RE = re.compile(r"[\W_]+", flags=re.UNICODE)
UNICODE_LETTER_RE = re.compile(r"[^\W\d_]", flags=re.UNICODE)
MOJIBAKE_HINT_RE = re.compile(r"(?:Ã.|Â.|â..)", flags=re.UNICODE)
CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?(?P<charset>[^;,'\"\s]+)", flags=re.IGNORECASE)


def is_probable_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_base_domain(value: str) -> str:
    clean = value.strip()
    if not clean:
        return ""

    if "://" in clean:
        host = (urlparse(clean).hostname or "").lower()
    else:
        host = clean.lower().lstrip(".")

    if not host:
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    if "." not in host:
        return ""

    labels = host.split(".")
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def is_origin_url(url: str, allowed_base_domains: set[str]) -> bool:
    if not url:
        return False
    base = get_base_domain(url)
    return bool(base and base in allowed_base_domains)


def is_non_origin_directory_url(url: str) -> bool:
    base = get_base_domain(url)
    if base and (base in NON_ORIGIN_DIRECTORY_BASE_DOMAINS or base in NON_ORIGIN_ASSET_BASE_DOMAINS):
        return True

    host = (urlparse(url).hostname or "").lower() if "://" in (url or "") else ""
    if host.startswith("radio.") or host.startswith("www.radio.") or ".radio." in host:
        return True
    return False


def normalize_for_token_search(value: str) -> str:
    text = (value or "").lower()
    return TOKEN_SEPARATOR_RE.sub(" ", text).strip()


def split_search_tokens(value: str) -> list[str]:
    cleaned = normalize_for_token_search(value)
    if not cleaned:
        return []
    return [token for token in cleaned.split() if token]


def has_unicode_letter(value: str) -> bool:
    return bool(UNICODE_LETTER_RE.search(value or ""))


def is_mixed_alnum_token(token: str, min_length: int = 2) -> bool:
    token = (token or "").strip()
    if len(token) < min_length:
        return False
    has_digit = any(char.isdigit() for char in token)
    has_alpha = any(char.isalpha() for char in token)
    return has_digit and has_alpha


def repair_mojibake_text(value: str) -> str:
    text = str(value or "")
    if not text or not MOJIBAKE_HINT_RE.search(text):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired or text


def extract_charset_from_content_type(content_type: str) -> str:
    text = str(content_type or "").strip()
    if not text:
        return ""
    match = CHARSET_RE.search(text)
    if not match:
        return ""
    return match.group("charset").strip().strip("'\"").lower()


def decode_text_bytes(
    payload: bytes,
    *,
    content_type: str = "",
    fallback_encodings: tuple[str, ...] = ("utf-8", "cp1252", "latin-1"),
    apply_mojibake_repair: bool = True,
) -> str:
    data = payload or b""
    if not data:
        return ""

    tried = set()
    encodings: list[str] = []
    declared_charset = extract_charset_from_content_type(content_type)
    if declared_charset:
        encodings.append(declared_charset)
    encodings.extend(fallback_encodings)

    decoded = ""
    for encoding in encodings:
        normalized = str(encoding or "").strip().lower()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            decoded = data.decode(normalized)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    else:
        decoded = data.decode("utf-8", errors="replace")

    if apply_mojibake_repair:
        decoded = repair_mojibake_text(decoded)
    return decoded


def read_text_file_with_fallbacks(
    path: Path,
    *,
    fallback_encodings: tuple[str, ...] = ("utf-8", "cp1252", "latin-1"),
    apply_mojibake_repair: bool = False,
) -> str:
    return decode_text_bytes(
        path.read_bytes(),
        fallback_encodings=fallback_encodings,
        apply_mojibake_repair=apply_mojibake_repair,
    )
