"""Archival memory: verbatim transcript turns + compaction snapshots.

Turns are stored as spoken, never summarized -- summaries can be rebuilt
any time; the original cannot. Crash-safe replay, tombstone-friendly.
"""

from __future__ import annotations

import json
from pathlib import Path

from jarvis.memory.common import _now


class Archive:
    """Archival memory: verbatim transcript turns + compaction snapshots."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, entry: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")

    def append_turn(self, *, session_id: str, question: str, answer: str,
                    lane: str | None = None) -> dict:
        entry = {"kind": "turn", "session_id": session_id,
                 "question": question or "", "answer": answer or "",
                 "lane": lane, "ts": _now()}
        self._append(entry)
        return entry

    def append_compaction(self, *, session_id: str, messages_before: int) -> dict:
        entry = {"kind": "compaction", "session_id": session_id,
                 "messages_before": messages_before, "ts": _now()}
        self._append(entry)
        return entry

    def _read(self, kind: str | None, session_id: str | None,
              limit: int | None) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines:
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if kind is not None and e.get("kind") != kind:
                continue
            if session_id is not None and e.get("session_id") != session_id:
                continue
            out.append(e)
        if limit is not None:
            out = out[-limit:]
        return out

    def turns(self, limit: int | None = None, session_id: str | None = None) -> list[dict]:
        return self._read("turn", session_id, limit)

    def compactions(self, limit: int | None = None) -> list[dict]:
        return self._read("compaction", None, limit)
