"""Shared station-name normalization and lookup helpers."""

from __future__ import annotations

import re
from typing import Callable


def normalize_station_name(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_station_id(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"\[/?COLOR[^\]]*\]", " ", text, flags=re.IGNORECASE)
    text = text.replace("•", " ")
    text = " ".join(text.strip().lower().split())
    if text.startswith("stationid:"):
        text = text[len("stationid:") :].strip()
    return text


def sanitize_station_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"\[/?COLOR[^\]]*\]", " ", text, flags=re.IGNORECASE)
    text = text.replace("•", " ")
    text = " ".join(text.strip().split())
    return text


def compact_station_text(value: str) -> str:
    text = sanitize_station_text(value).lower()
    if not text:
        return ""
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", "", text)


def build_station_lookup_variants(value: str) -> list[str]:
    raw = sanitize_station_text(value)
    if not raw:
        return []

    variants: list[str] = []
    seen = set()

    def add(candidate: str) -> None:
        clean = " ".join(str(candidate or "").strip().split())
        if not clean:
            return
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        variants.append(clean)

    add(raw)
    add(re.sub(r"[-_./|]+", " ", raw))
    compact = compact_station_text(raw)
    if compact and len(compact) >= 3:
        add(compact)

    return variants


def find_station_by_name_with_fallback(
    lookup_service,
    station_input: str,
    *,
    station_id: str = "",
    on_variant_failed: Callable[[str, Exception], None] | None = None,
    on_variant_selected: Callable[[str, object], None] | None = None,
):
    station_id_norm = normalize_station_id(station_id)
    variants = build_station_lookup_variants(station_input)
    if not variants:
        raise ValueError("Kein gueltiger Sendername fuer Lookup vorhanden.")

    last_error = None
    for idx, variant in enumerate(variants):
        try:
            if station_id_norm:
                station = lookup_service.find_best_match(variant, station_id=station_id_norm)
            else:
                station = lookup_service.find_best_match(variant)
            if idx > 0 and on_variant_selected:
                on_variant_selected(variant, station)
            return station
        except Exception as err:
            last_error = err
            if on_variant_failed:
                on_variant_failed(variant, err)

    raise last_error if last_error else ValueError("Kein passender Sender gefunden.")


def build_station_key(station_name: str, station_id: str = "") -> str:
    station_id_norm = normalize_station_id(station_id)
    if station_id_norm:
        return f"stationid:{station_id_norm}"
    name_norm = normalize_station_name(station_name)
    if not name_norm:
        return ""
    return f"name:{name_norm}"
