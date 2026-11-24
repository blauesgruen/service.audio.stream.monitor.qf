"""Per-playback station selection state (pure logic, thread-safe, no Kodi).

States per source key: pending (dialog requested/open), chosen, cancelled.
A "playback session" ends when requests for a different station arrive; then all
non-persisted decisions of the previous station are dropped and its dialog is stale.
"""

from __future__ import annotations

import threading

PENDING = "pending"
CHOSEN = "chosen"
CANCELLED = "cancelled"


class SelectionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_key = ""
        self._states: dict[str, dict] = {}

    def on_request(self, key: str) -> str:
        """Register the station of an incoming request.

        Returns the previous key if the station changed (its state is discarded and any
        open dialog for it is stale), else "".
        """
        key = str(key or "")
        with self._lock:
            previous = self._active_key
            if key == previous:
                return ""
            self._active_key = key
            if previous:
                self._states.pop(previous, None)
            return previous

    def get(self, key: str) -> dict | None:
        with self._lock:
            state = self._states.get(key)
            return dict(state) if state else None

    def begin_pending(self, key: str, candidates: list[dict]) -> bool:
        """True only for the first caller; polling must not reopen the dialog."""
        with self._lock:
            if key != self._active_key or key in self._states:
                return False
            self._states[key] = {"state": PENDING, "candidates": list(candidates)}
            return True

    def force_pending(self, key: str, candidates: list[dict]) -> bool:
        """Manual assignment: (re)open the selection even if one was decided in this playback."""
        with self._lock:
            if key != self._active_key:
                return False
            if self._states.get(key, {}).get("state") == PENDING:
                return False  # dialog already open
            self._states[key] = {"state": PENDING, "candidates": list(candidates)}
            return True

    def choose(self, key: str, stationuuid: str, save: bool) -> bool:
        """Apply a user decision. Ignored (False) if the station changed meanwhile."""
        with self._lock:
            if key != self._active_key or self._states.get(key, {}).get("state") != PENDING:
                return False
            self._states[key] = {"state": CHOSEN, "stationuuid": str(stationuuid), "save": bool(save)}
            return True

    def cancel(self, key: str) -> bool:
        with self._lock:
            if key != self._active_key or self._states.get(key, {}).get("state") != PENDING:
                return False
            self._states[key] = {"state": CANCELLED}
            return True

    def clear(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)
