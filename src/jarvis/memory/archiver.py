"""TurnArchiver -- EventPipe subscriber that captures finished turns.

Fires on session.idle for JARVIS-OWNED sessions only (the kernel event
stream is global; the owns-filter keeps foreign transcripts out). Covers
fg, bg and scheduled (routine) turns through the same path.
"""

from __future__ import annotations

from typing import Callable

from jarvis.memory.archive import Archive


class TurnArchiver:
    """EventPipe subscriber: captures finished turns into the Archive.

    Fires on session.idle for JARVIS-OWNED sessions only (the kernel event
    stream is global — a foreign TUI session's transcript must never leak
    into JARVIS memory). One archive entry per turn: question from the
    journal's turn-started entry, answer replayed from the captured deltas.
    Covers fg, bg and scheduled (routine) turns through the same path.
    """

    def __init__(self, events: list, journal, archive: Archive, owns: Callable[[str], bool]):
        self._events = events
        self._journal = journal
        self._archive = archive
        self._owns = owns
        self._cursor: dict[str, int] = {}
        self._last_turn_ts: dict[str, float | None] = {}   # sid -> turn-started ts

    def note_event(self, event: dict) -> None:
        etype = str(event.get("type", ""))
        if not (etype == "session.idle" or etype.endswith(".idle")):
            return
        data = event.get("data") or event.get("properties") or {}
        sid = str(data.get("sessionID", ""))
        if not sid or not self._owns(sid):
            return
        start = self._cursor.get(sid, 0)
        self._cursor[sid] = len(self._events)
        events_slice = self._events[start:] if start < len(self._events) else []
        answer = "".join(
            e.get("data", {}).get("text") or ""
            for e in events_slice
            if e.get("type") == "session.text.delta"
            and e.get("data", {}).get("sessionID") == sid
        ).strip()
        question, lane, q_ts = self._last_question(sid)
        if not answer:
            # No new deltas: archive only if this is a turn-started we have
            # never seen archived (spurious duplicate idles archive nothing).
            if q_ts is None or q_ts == self._last_turn_ts.get(sid):
                return
        self._last_turn_ts[sid] = q_ts
        self._archive.append_turn(session_id=sid, question=question,
                                  answer=answer, lane=lane)

    def _last_question(self, sid: str) -> tuple[str, str | None, float | None]:
        for entry in reversed(self._journal.tail(50, kind="turn-started")):
            if entry.get("session_id") == sid:
                return (str(entry.get("text", "")), entry.get("lane"),
                        entry.get("ts"))
        return "", None, None
