"""Staged memory changes — the kernel's gated memory-write (phase 7).

Letta's memory tools let the agent edit its own memory blocks. That is
refused here (D6: facts are subject-written, capture over extraction).
What lands instead is the D7 pattern: the kernel PROPOSES memory changes
as text, the active layer stages them deterministically, and the subject
approves or refuses at the console. Nothing touches FactStore until a
human types the approval — the approval IS the subject's write, applied
by the console with source="console", provenance journalled.

Storage: append-only JSONL beside the facts file. `resolved` entries are
kept forever (audit); list() shows only unresolved ones.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

ACTIONS = ("remember", "forget")


class StagedMemory:
    """Proposals for durable-memory changes, pending human approval."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- storage

    def _read(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue                  # torn final write — ignore
        return out

    def _write_all(self, entries: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=True, sort_keys=True) + "\n")
        tmp.replace(self.path)

    # --------------------------------------------------------------- verbs

    def stage(self, action: str, text: str) -> dict:
        """Stage one proposal. Raises ValueError on junk input."""
        action = (action or "").strip().lower()
        text = " ".join((text or "").split())
        if action not in ACTIONS:
            raise ValueError("action must be remember|forget")
        if not text:
            raise ValueError("empty proposal text")
        entry = {"id": "mp" + secrets.token_hex(2), "action": action,
                 "text": text[:2000], "ts": round(time.time(), 3),
                 "resolved": None}
        entries = self._read()
        entries.append(entry)
        self._write_all(entries)
        return entry

    def list(self) -> list[dict]:
        """Unresolved proposals, oldest first."""
        return [e for e in self._read() if not e.get("resolved")]

    def resolve(self, pid: str, accept: bool) -> dict | None:
        """Mark one proposal approved/refused. Returns the resolved entry.
        Applying an approval stays the console's job (FactStore, source
        'console') — this store only records the decision."""
        entries = self._read()
        for e in entries:
            if e.get("id") == pid and not e.get("resolved"):
                e["resolved"] = {"ts": round(time.time(), 3),
                                 "accepted": bool(accept)}
                self._write_all(entries)
                return e
        return None

    def unresolved_count(self) -> int:
        return len(self.list())
