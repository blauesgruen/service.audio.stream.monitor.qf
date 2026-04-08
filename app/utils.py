"""Shared utility helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .config import NON_ORIGIN_ASSET_BASE_DOMAINS, NON_ORIGIN_DIRECTORY_BASE_DOMAINS


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
