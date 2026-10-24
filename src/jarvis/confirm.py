"""One-time confirmation codes — the human-in-the-loop channel.

A pending dangerous action produces a short code. The human types the code
(through whatever UI exists — console today, voice/bus later) to allow it,
or types 'no' to refuse. Solving consumes the code exactly once; codes
expire unanswered. Nothing here ever auto-solves.
"""

from __future__ import annotations

import secrets
import threading
import time


class ConfirmationBroker:
    def __init__(self, ttl_seconds: float = 900.0):
        self.ttl = ttl_seconds
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def open(self, description: str) -> str:
        """Create a pending confirmation. Returns the human-visible code."""
        with self._lock:
            self._gc()
            code = secrets.token_hex(3)  # 6 hex chars, one-time
            self._items[code] = {"description": description, "opened": time.time()}
            return code

    def solve(self, code: str) -> dict | None:
        """Consume a code. Returns its description, or None (unknown/expired)."""
        with self._lock:
            self._gc()
            return self._items.pop(code, None)

    def peek(self) -> list[dict]:
        with self._lock:
            self._gc()
            now = time.time()
            return [{"code": c, "age_s": round(now - v["opened"], 1),
                     "description": v["description"]}
                    for c, v in sorted(self._items.items())]

    def _gc(self) -> None:
        now = time.time()
        for code in [c for c, v in self._items.items() if now - v["opened"] > self.ttl]:
            del self._items[code]
