"""Shared centralized pre-check for song candidate fields."""

from __future__ import annotations

import re

NUMERIC_ID_RE = re.compile(r"^\d{6,}$")
NUMERIC_PAIR_PART_RE = re.compile(r"^\d{3,}$")
PHONE_BLOCK_RE = re.compile(r"\b(?:0\d{2,4}[\s\-]?\d{2,}[\s\-]?\d{1,})\b")

NON_SONG_TEXT_KEYWORDS = {
    "anruf",
    "hotline",
    "verkehr",
    "studio",
    "nachrichten",
}


def normalize_station_compare_text(text: str) -> str:
    value = str(text or "").strip().lower()
    if not value:
        return ""
    value = value.replace("&", " and ")
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def build_station_hints(raw_values: list[str] | tuple[str, ...]) -> list[str]:
    hints: list[str] = []
    seen = set()
    for candidate in list(raw_values or []):
        val = str(candidate or "").strip()
        if not val:
            continue
        variants = [val]
        if "-" in val or "_" in val:
            variants.append(val.replace("-", " ").replace("_", " "))
        for raw in variants:
            norm = normalize_station_compare_text(raw)
            if norm and norm not in seen:
                seen.add(norm)
                hints.append(norm)
    return hints


def is_station_name_match_pair(
    pair: tuple[str, str],
    station_hints: list[str] | tuple[str, ...],
    min_len: int = 5,
) -> bool:
    a, t = pair
    if not a or not t:
        return False
    pair_text = normalize_station_compare_text(f"{a} {t}")
    if not pair_text:
        return False
    for hint in list(station_hints or []):
        h = str(hint or "").strip().lower()
        if len(h) < int(min_len):
            continue
        if h in pair_text:
            return True
    return False


def is_obvious_non_song_text(text: str, extra_keywords: list[str] | tuple[str, ...] = ()) -> bool:
    value = str(text or "").strip().lower()
    if not value:
        return False
    all_tokens = set(NON_SONG_TEXT_KEYWORDS)
    all_tokens.update(str(k or "").strip().lower() for k in list(extra_keywords or []) if str(k or "").strip())
    if any(tok in value for tok in all_tokens):
        return True
    return bool(PHONE_BLOCK_RE.search(value))


def is_generic_metadata_text(
    text: str,
    station_name: str = "",
    extra_keywords: list[str] | tuple[str, ...] = (),
) -> bool:
    text_l = str(text or "").strip().lower()
    if not text_l:
        return False
    station_l = str(station_name or "").strip().lower()
    if station_l and station_l in text_l:
        return True
    return any(str(tok or "").lower() in text_l for tok in list(extra_keywords or []))


def is_generic_song_pair(
    pair: tuple[str, str],
    station_name: str = "",
    extra_keywords: list[str] | tuple[str, ...] = (),
) -> bool:
    a, t = pair
    if not a or not t:
        return False
    return (
        is_generic_metadata_text(a, station_name, extra_keywords)
        or is_generic_metadata_text(t, station_name, extra_keywords)
        or is_generic_metadata_text(f"{a} - {t}", station_name, extra_keywords)
    )


def prefilter_pair(
    artist: str,
    title: str,
    *,
    source: str,
    station_name: str = "",
    invalid_values: list[str] | tuple[str, ...] = (),
    extra_keywords: list[str] | tuple[str, ...] = (),
    station_hint_values: list[str] | tuple[str, ...] = (),
    station_match_min_len: int = 5,
) -> tuple[str, str, str]:
    a = str(artist or "").strip()
    t = str(title or "").strip()

    if not a or not t:
        return ("", "", "missing_field")

    invalid = {str(v) for v in list(invalid_values or [])}
    if a in invalid or t in invalid:
        return ("", "", "invalid_value")

    if NUMERIC_PAIR_PART_RE.match(a) and NUMERIC_PAIR_PART_RE.match(t):
        return ("", "", "numeric_pair")
    if NUMERIC_ID_RE.match(a) or NUMERIC_ID_RE.match(t):
        return ("", "", "numeric_id")

    if is_generic_song_pair((a, t), station_name, extra_keywords):
        return ("", "", "generic_pair")

    src = str(source or "").strip().lower()
    reject_station_match = (
        src.startswith("api")
        or src.startswith("icy")
        or src.startswith("asm-qf")
        or src in ("stream", "")
    )
    if reject_station_match:
        hints = build_station_hints(station_hint_values)
        if is_station_name_match_pair((a, t), hints, min_len=station_match_min_len):
            return ("", "", "station_overlap")

    if is_obvious_non_song_text(f"{a} - {t}", extra_keywords=extra_keywords):
        return ("", "", "obvious_non_song")

    return (a, t, "ok")


def is_valid_song_candidate(artist: str, title: str, station_name: str = "") -> bool:
    _, _, status = prefilter_pair(
        artist,
        title,
        source="stream",
        station_name=station_name,
        invalid_values=(station_name,),
        station_hint_values=(station_name,),
    )
    return status == "ok"
