"""Shared song parity and lifecycle policy used by GUI and Kodi bridge."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SongParityConfig:
    enabled: bool = True
    hold_seconds: float = 0.0
    no_hit_confirm: int = 2
    empty_confirm: int = 2
    stale_feed_drop_seconds: float = 180.0
    reappear_block_seconds: float = 0.0
    pending_feed_confirm_without_history: bool = False


@dataclass
class SongParityOutcome:
    result: dict[str, Any]
    action: str
    debug: dict[str, Any] = field(default_factory=dict)


class SongParityPolicy:
    def __init__(
        self,
        *,
        state: dict[str, Any] | None = None,
        config: SongParityConfig | None = None,
    ) -> None:
        self.state = state if state is not None else {}
        self.config = config or SongParityConfig()

    @staticmethod
    def build_pair_fingerprint(artist="", title="", source="", source_url="") -> str:
        artist_value = str(artist or "").strip().lower()
        title_value = str(title or "").strip().lower()
        source_value = str(source or "").strip().lower()
        source_url_value = str(source_url or "").strip().lower()
        if not artist_value or not title_value:
            return ""
        return "|".join((artist_value, title_value, source_value, source_url_value))

    @classmethod
    def build_result_pair_fingerprint(cls, result: dict[str, Any] | None) -> str:
        result_obj = result if isinstance(result, dict) else {}
        meta = result_obj.get("meta") or {}
        return cls.build_pair_fingerprint(
            artist=result_obj.get("artist") or "",
            title=result_obj.get("title") or "",
            source=result_obj.get("source") or "",
            source_url=meta.get("source_url") or "",
        )

    def _normalized_result(self, result: dict[str, Any] | None) -> dict[str, Any]:
        result_obj = result if isinstance(result, dict) else {}
        return {
            "status": str(result_obj.get("status") or ""),
            "artist": str(result_obj.get("artist") or ""),
            "title": str(result_obj.get("title") or ""),
            "source": str(result_obj.get("source") or ""),
            "reason": str(result_obj.get("reason") or ""),
            "meta": dict(result_obj.get("meta") or {}),
        }

    def _clear_pending_hit(self) -> None:
        self.state["pending_hit_key"] = ""
        self.state["pending_hit_count"] = 0
        self.state["pending_hit_ts"] = 0.0

    def _clear_last_hit_state(self) -> None:
        self.state["last_artist"] = ""
        self.state["last_title"] = ""
        self.state["last_source"] = ""
        self.state["last_reason"] = ""
        self.state["last_meta"] = {}
        self.state["last_pair_fingerprint"] = ""
        self.state["last_pair_first_seen_ts"] = 0.0
        self.state["last_hit_ts"] = 0.0
        self.state["last_strong_hit_ts"] = 0.0

    def _remember_recently_cleared_pair(self, now_ts: float) -> None:
        pair_fingerprint = str(self.state.get("last_pair_fingerprint") or "").strip()
        if not pair_fingerprint:
            pair_fingerprint = self.build_pair_fingerprint(
                artist=self.state.get("last_artist") or "",
                title=self.state.get("last_title") or "",
                source=self.state.get("last_source") or "",
                source_url=(self.state.get("last_meta") or {}).get("source_url") or "",
            )
        if not pair_fingerprint:
            return
        self.state["recently_cleared_pair_fingerprint"] = pair_fingerprint
        self.state["recently_cleared_pair_ts"] = float(now_ts or 0.0)

    def _clear_recently_cleared_pair(self) -> None:
        self.state["recently_cleared_pair_fingerprint"] = ""
        self.state["recently_cleared_pair_ts"] = 0.0

    def _get_recently_cleared_reappearance_block(
        self,
        result: dict[str, Any],
        now_ts: float,
    ) -> tuple[bool, float]:
        block_seconds = max(0.0, float(self.config.reappear_block_seconds or 0.0))
        if block_seconds <= 0.0:
            return False, 0.0
        pair_fingerprint = self.build_result_pair_fingerprint(result)
        if not pair_fingerprint:
            return False, 0.0
        cleared_fingerprint = str(self.state.get("recently_cleared_pair_fingerprint") or "").strip()
        cleared_ts = float(self.state.get("recently_cleared_pair_ts") or 0.0)
        if not cleared_fingerprint or cleared_ts <= 0.0:
            return False, 0.0
        age = max(0.0, float(now_ts or 0.0) - cleared_ts)
        if age > block_seconds:
            self._clear_recently_cleared_pair()
            return False, 0.0
        if pair_fingerprint != cleared_fingerprint:
            return False, 0.0
        return True, max(0.0, block_seconds - age)

    def apply(
        self,
        result: dict[str, Any] | None,
        *,
        now_ts: float | None = None,
    ) -> SongParityOutcome:
        now = float(now_ts or time.time())
        self.state["updated_ts"] = now

        normalized = self._normalized_result(result)
        status = normalized["status"]
        reason = normalized["reason"]
        artist = normalized["artist"]
        title = normalized["title"]
        source = normalized["source"]
        meta = normalized["meta"]

        hold_seconds = max(0.0, float(self.config.hold_seconds or 0.0))
        stale_feed_drop_seconds = max(0.0, float(self.config.stale_feed_drop_seconds or 0.0))
        parity_enabled = bool(self.config.enabled)
        pending_bypassed = False

        if status == "hit" and artist and title:
            blocked_reappearance, block_remaining = self._get_recently_cleared_reappearance_block(
                normalized,
                now,
            )
            if blocked_reappearance:
                meta = {
                    **meta,
                    "reappeared_recently_cleared_pair": True,
                    "reappear_block_seconds": round(float(self.config.reappear_block_seconds), 3),
                    "reappear_block_remaining": round(block_remaining, 3),
                }
                normalized = {
                    "status": "no_hit",
                    "artist": "",
                    "title": "",
                    "source": "",
                    "reason": "reappeared_recently_cleared_pair",
                    "meta": meta,
                }
                status = normalized["status"]
                reason = normalized["reason"]
                artist = ""
                title = ""
                source = ""

        if status == "hit" and artist and title:
            has_last_pair_before = bool(self.state.get("last_artist") and self.state.get("last_title"))
            stream_pair_state = str(meta.get("stream_pair_state") or "")
            is_feed_hit = str(source).startswith("web_feed_")
            weak_stream_signal = stream_pair_state in {"", "no_candidate", "missing_field"}
            need_pending_confirmation = bool(self.config.pending_feed_confirm_without_history)

            if (
                parity_enabled
                and is_feed_hit
                and weak_stream_signal
                and not has_last_pair_before
                and need_pending_confirmation
            ):
                pending_key = f"{artist.lower()}|{title.lower()}|{source}"
                previous_key = str(self.state.get("pending_hit_key") or "")
                previous_count = int(self.state.get("pending_hit_count") or 0)
                if pending_key == previous_key and previous_count > 0:
                    self.state["pending_hit_count"] = previous_count + 1
                else:
                    self.state["pending_hit_key"] = pending_key
                    self.state["pending_hit_count"] = 1
                self.state["pending_hit_ts"] = now

                if int(self.state.get("pending_hit_count") or 0) < 2:
                    pending_result = {
                        "status": "no_hit",
                        "artist": "",
                        "title": "",
                        "source": "",
                        "reason": "pending_feed_confirmation",
                        "meta": {
                            **meta,
                            "pending_hit": True,
                            "pending_hit_count": self.state.get("pending_hit_count") or 0,
                            "pending_pair": f"{artist} - {title}",
                        },
                    }
                    return SongParityOutcome(
                        result=pending_result,
                        action="pending_hit",
                        debug={"hold_remaining": 0.0},
                    )
                self._clear_pending_hit()
            else:
                pending_bypassed = bool(
                    parity_enabled
                    and is_feed_hit
                    and weak_stream_signal
                    and not has_last_pair_before
                    and not need_pending_confirmation
                )
                if pending_bypassed:
                    meta = {**meta, "pending_bypassed": True}
                    normalized = {
                        "status": status,
                        "artist": artist,
                        "title": title,
                        "source": source,
                        "reason": reason,
                        "meta": meta,
                    }
                self._clear_pending_hit()

            if status == "hit" and is_feed_hit and weak_stream_signal and has_last_pair_before:
                same_pair = (
                    artist.strip().lower() == str(self.state.get("last_artist") or "").strip().lower()
                    and title.strip().lower() == str(self.state.get("last_title") or "").strip().lower()
                )
                reference_ts = float(self.state.get("last_strong_hit_ts") or 0.0)
                if reference_ts <= 0.0:
                    reference_ts = float(self.state.get("last_pair_first_seen_ts") or 0.0)
                weak_age = (now - reference_ts) if reference_ts > 0.0 else 0.0
                if same_pair and reference_ts > 0.0 and weak_age > stale_feed_drop_seconds:
                    meta = {
                        **meta,
                        "stale_feed_only": True,
                        "stale_feed_age": round(weak_age, 3),
                        "stale_feed_drop_seconds": round(stale_feed_drop_seconds, 3),
                    }
                    normalized = {
                        "status": "no_hit",
                        "artist": "",
                        "title": "",
                        "source": "",
                        "reason": "generic_or_non_song",
                        "meta": meta,
                    }
                    status = normalized["status"]
                    reason = normalized["reason"]
                    artist = ""
                    title = ""
                    source = ""

        if status == "hit" and artist and title:
            stream_pair_state = str(meta.get("stream_pair_state") or "")
            is_feed_hit = str(source).startswith("web_feed_")
            weak_stream_signal = stream_pair_state in {"", "no_candidate", "missing_field"}
            pair_fingerprint = self.build_result_pair_fingerprint(normalized)
            if pair_fingerprint and pair_fingerprint != str(self.state.get("last_pair_fingerprint") or ""):
                self.state["last_pair_fingerprint"] = pair_fingerprint
                self.state["last_pair_first_seen_ts"] = now
            elif pair_fingerprint and float(self.state.get("last_pair_first_seen_ts") or 0.0) <= 0.0:
                self.state["last_pair_first_seen_ts"] = now

            self.state["last_hit_ts"] = now
            if not (is_feed_hit and weak_stream_signal):
                self.state["last_strong_hit_ts"] = now
            self.state["last_artist"] = artist
            self.state["last_title"] = title
            self.state["last_source"] = source
            self.state["last_reason"] = reason
            self.state["last_no_hit_reason"] = ""
            self.state["last_meta"] = dict(meta)
            self.state["no_hit_streak"] = 0
            self.state["empty_streak"] = 0
            if pair_fingerprint and pair_fingerprint != str(self.state.get("recently_cleared_pair_fingerprint") or ""):
                self._clear_recently_cleared_pair()
            return SongParityOutcome(
                result=normalized,
                action="accept_hit",
                debug={
                    "last_hit_age": 0.0,
                    "hold_remaining": round(hold_seconds, 3),
                    "pending_bypassed": pending_bypassed,
                },
            )

        if status != "no_hit" or not parity_enabled:
            if status == "no_hit":
                self.state["last_no_hit_reason"] = reason
                self.state["no_hit_streak"] = int(self.state.get("no_hit_streak") or 0) + 1
            return SongParityOutcome(result=normalized, action="passthrough")

        self.state["last_no_hit_reason"] = reason
        self.state["no_hit_streak"] = int(self.state.get("no_hit_streak") or 0) + 1

        feed_pair_state = str(meta.get("feed_pair_state") or "")
        stream_pair_state = str(meta.get("stream_pair_state") or "")
        empty_signals = {"missing_field", "no_candidate"}
        is_empty_signal = reason == "generic_or_non_song" and (
            feed_pair_state in empty_signals or stream_pair_state in empty_signals
        )
        if is_empty_signal:
            self.state["empty_streak"] = int(self.state.get("empty_streak") or 0) + 1
        else:
            self.state["empty_streak"] = 0

        last_hit_ts = float(self.state.get("last_hit_ts") or 0.0)
        has_last_pair = bool(self.state.get("last_artist") and self.state.get("last_title"))
        last_hit_age = (now - last_hit_ts) if last_hit_ts > 0 else float("inf")
        hold_remaining = max(0.0, hold_seconds - last_hit_age) if has_last_pair else 0.0
        hold_active = has_last_pair and hold_remaining > 0

        no_hit_confirmed = int(self.state.get("no_hit_streak") or 0) >= max(1, int(self.config.no_hit_confirm))
        empty_confirmed = int(self.state.get("empty_streak") or 0) >= max(1, int(self.config.empty_confirm))

        if no_hit_confirmed or empty_confirmed:
            self._remember_recently_cleared_pair(now)
            self._clear_last_hit_state()
            self.state["no_hit_streak"] = 0
            self.state["empty_streak"] = 0
            self._clear_pending_hit()
            return SongParityOutcome(
                result=normalized,
                action="confirm_no_hit",
                debug={
                    "last_hit_age": round(last_hit_age, 3) if last_hit_age != float("inf") else "",
                    "hold_remaining": round(hold_remaining, 3),
                    "no_hit_confirmed": no_hit_confirmed,
                    "empty_confirmed": empty_confirmed,
                },
            )

        if hold_active:
            hold_result = {
                "status": "hit",
                "artist": self.state.get("last_artist") or "",
                "title": self.state.get("last_title") or "",
                "source": self.state.get("last_source") or "asm-qf_hold",
                "reason": "hold_last_song",
                "meta": {
                    **(self.state.get("last_meta") or {}),
                    "hold": True,
                    "hold_remaining": round(hold_remaining, 3),
                    "hold_seconds": round(hold_seconds, 3),
                    "no_hit_reason": reason,
                    "no_hit_streak": self.state.get("no_hit_streak") or 0,
                    "empty_streak": self.state.get("empty_streak") or 0,
                    "feed_pair_state": feed_pair_state,
                    "stream_pair_state": stream_pair_state,
                },
            }
            return SongParityOutcome(
                result=hold_result,
                action="hold_last_song",
                debug={
                    "last_hit_age": round(last_hit_age, 3),
                    "hold_remaining": round(hold_remaining, 3),
                },
            )

        return SongParityOutcome(
            result=normalized,
            action="soft_no_hit",
            debug={
                "last_hit_age": round(last_hit_age, 3) if last_hit_age != float("inf") else "",
                "hold_remaining": 0.0,
            },
        )
