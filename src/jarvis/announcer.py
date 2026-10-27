"""Announcer -- the live presenter of pending confirmations and turn notes.

Phase 3 polish ("codes announced live as they appear"). Until now a parked
action only surfaced when the human happened to type 'pending'. One
background thread now watches both confirmation sources and the kernel
event stream, and pushes short lines to whatever front-end is attached
(console today, voice/bus later):

    broker  -- in-process kernel permission asks parked by the doorkeeper
    store   -- file-backed MCP-bridge confirmations (jarvis-mcp-gate)
    events  -- turn-idle notes for the watched session ("replay to read it")

Rules:
    - a code is announced exactly once; resolution is announced once
    - the announcer NEVER decides anything; it only describes
    - a failing source is skipped, never fatal (best-effort presenter)
"""

from __future__ import annotations

import threading

from jarvis.journal import Journal


def _short(text: str, limit: int = 100) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class Announcer:
    """Polls confirmation sources + kernel events; emits human-readable lines."""

    def __init__(self, worker=None, store=None, *, on_line=None,
                 journal: Journal | None = None, poll_seconds: float = 1.5):
        self.worker = worker          # PermissionWorker-like: .pending_list()
        self.store = store            # PendingStore-like: .list()
        self.on_line = on_line or (lambda line: print(line, flush=True))
        self.journal = journal
        self.poll_seconds = poll_seconds

        self.watch_sid: str | None = None   # session whose turn-completion is narrated
        self._open: dict[str, str] = {}     # code -> description, as last announced
        self._idle_for: str | None = None   # watch_sid we already reported idle
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ----------------------------------------------------------------- scan

    def scan(self) -> list[str]:
        """Diff current pending set against the last one. Pure; safe to test.

        Returns one line per NEW code and one line per RESOLVED code.
        Sources that raise are skipped -- the presenter must never take the
        console down.
        """
        open_now: dict[str, str] = {}
        if self.worker is not None:
            try:
                for item in self.worker.pending_list():
                    code = str(item.get("code", ""))
                    if code:
                        open_now[code] = str(item.get("description", ""))
            except Exception:  # noqa: BLE001 -- presenter stays alive
                pass
        if self.store is not None:
            try:
                for item in self.store.list():
                    code = str(item.get("code", ""))
                    if code:
                        open_now[code] = str(item.get("description", ""))
            except Exception:  # noqa: BLE001
                pass

        messages: list[str] = []
        for code in sorted(set(open_now) - set(self._open)):
            desc = _short(open_now[code])
            messages.append(f"awaiting you  {code}  {desc}   ->  yes {code} / no {code}")
        for code in sorted(set(self._open) - set(open_now)):
            messages.append(f"({code} resolved or expired)")
        self._open = open_now
        return messages

    # ---------------------------------------------------------------- events

    def note_event(self, event: dict) -> None:
        """EventPipe subscriber: narrate turn completion for the watched session.

        Event vocabulary is best-effort by design (phase 1 finding: the
        kernel's event names are not an API we control); anything that looks
        like an idle marker on the watched session triggers one note.
        """
        if self.watch_sid is None:
            return
        etype = str(event.get("type", ""))
        data = event.get("data") or event.get("properties") or {}
        if str(data.get("sessionID", "")) != self.watch_sid:
            return
        if etype == "session.idle" or etype.endswith(".idle") or etype == "session.execution.failed":
            if self._idle_for != self.watch_sid:
                self._idle_for = self.watch_sid
                self._emit(f"[jarvis] turn finished -- 'replay' shows the assistant text")

    def nudge(self, line: str) -> None:
        """One-off line from the console (lane hints, pauses, ...)."""
        self._emit(line)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-announcer",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_seconds: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_seconds)

    # -------------------------------------------------------------- internals

    def _run(self) -> None:
        while not self._stop.is_set():
            for line in self.scan():
                self._emit("[jarvis] " + line)
            self._stop.wait(self.poll_seconds)

    def _emit(self, line: str) -> None:
        try:
            if self.journal is not None:
                self.journal.add("announced", text=line)
        except Exception:  # noqa: BLE001 -- journal failure must not silence the presenter
            pass
        self.on_line(line)
