"""EventPipe -- the kernel SSE stream on a background thread.

Owns the streaming thread so callers never think about blocking; one bad
subscriber must never kill the pipe. The SSE stream is the pipe's life:
if the kernel goes down or restarts, the pipe ends QUIETLY (recovery is
a service-lifecycle concern; callers re-attach by rebuilding the console).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # typing only, avoids an import cycle
    from jarvis.kernel.adapter import Kernel


class EventPipe:
    """Owns the SSE thread so callers never think about blocking."""

    def __init__(self, kernel: Kernel):
        self._kernel = kernel
        self._subs: list[Any] = []  # callables(event: dict)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def attach(self, callback) -> None:
        with self._lock:
            self._subs.append(callback)
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def run():
            # The SSE stream is the pipe's life. If the kernel goes down or
            # restarts, the pipe ends QUIETLY -- reconnect/recovery is a
            # service-lifecycle concern (later phase); a noisy thread death
            # helps nobody. Callers re-attach by rebuilding the console.
            try:
                for event in self._kernel.events():
                    with self._lock:
                        for cb in list(self._subs):
                            try:
                                cb(event)
                            except Exception:  # noqa: BLE001 - a bad subscriber must not kill the pipe
                                pass
            except Exception:  # noqa: BLE001 -- kernel unreachable/stream broke
                pass

        self._thread = threading.Thread(target=run, name="jarvis-eventpipe", daemon=True)
        self._thread.start()
