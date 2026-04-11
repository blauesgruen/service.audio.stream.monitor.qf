"""Thread-safe in-memory logger with queue integration for Tkinter."""

from __future__ import annotations

from datetime import datetime
from queue import Empty, Queue
from typing import Callable


class LiveLogger:
    def __init__(self) -> None:
        self._queue: Queue[str] = Queue()

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._queue.put(f"[{timestamp}] {message}")

    def drain(self, on_line: Callable[[str], None]) -> None:
        while True:
            try:
                line = self._queue.get_nowait()
            except Empty:
                break
            on_line(line)
