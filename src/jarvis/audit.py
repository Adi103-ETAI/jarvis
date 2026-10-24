"""Audit log — every gate verdict lands here, permanently, grep-able.

One JSON line per event in .jarvis/gate-log.jsonl (git-ignored). If this
file cannot be written, gates fail loudly: an unlogged decision is not a
decision.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


class GateLog:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path(".jarvis/gate-log.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, gate: str, verdict: str, reason: str = "", **fields: Any) -> dict:
        entry = {"ts": round(time.time(), 3), "gate": gate, "verdict": verdict,
                 "reason": reason, **fields}
        line = json.dumps(entry, ensure_ascii=True, sort_keys=True)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as e:
            print(f"AUDIT-FAILURE: {e}", file=sys.stderr, flush=True)
            raise
        return entry

    def tail(self, n: int = 20) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out
