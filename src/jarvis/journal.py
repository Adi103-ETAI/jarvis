"""Journal — JARVIS's append-only memory of what happened.

One JSON line per entry in .jarvis/journal.jsonl (git-ignored). The journal
is deliberately dumber than a database: it is the raw stream that later
phases summarize into briefings and weave into context. It never edits the
past; corrections are new entries.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path(".jarvis/journal.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, kind: str, **fields: Any) -> dict:
        entry = {"ts": round(time.time(), 3), "kind": kind, **fields}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
        except OSError as e:
            print(f"JOURNAL-FAILURE: {e}", file=sys.stderr, flush=True)
            raise
        return entry

    def tail(self, n: int = 50, kind: str | None = None) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines:
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if kind is None or e.get("kind") == kind:
                out.append(e)
        return out[-n:]
