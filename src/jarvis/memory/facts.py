"""Core memory: subject-written durable facts, append-only JSONL.

The v0 law lives at the store level: only the subject (console) may write
facts -- FactStore.add refuses every other source, not just the UI.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Callable

from jarvis.memory.common import FACT_SOURCES, _now


class FactStore:
    """Core memory: subject-written durable facts, append-only JSONL.

    Each line is {"op": "add"|"forget", ...}; state = replay of the log.
    ids are short human-typeable codes (f1a2b3c4) so 'forget f1a2b3c4'
    works at a console. Re-adding identical text refreshes the existing
    fact instead of duplicating it.
    """

    def __init__(self, path: str | Path, *, max_facts: int = 512,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_facts = max_facts
        self._clock = clock

    # ------------------------------------------------------------- storage

    def _append(self, entry: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")

    def facts(self) -> list[dict]:
        """Live facts = replay of the log, oldest first. Crash-safe."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out: dict[str, dict] = {}
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue                      # torn final write — ignore
            op = e.get("op")
            fid = str(e.get("id", ""))
            if not fid:
                continue
            if op == "add":
                if fid in out:                # refresh (dedupe re-add)
                    out[fid].update(text=str(e.get("text", "")),
                                    category=str(e.get("category", "fact")),
                                    ts=float(e.get("ts", 0.0)))
                else:
                    ts = float(e.get("ts", 0.0))
                    out[fid] = {"id": fid, "text": str(e.get("text", "")),
                                "category": str(e.get("category", "fact")),
                                "ts": ts, "created": ts}
            elif op == "forget":
                out.pop(fid, None)            # tombstone; history preserved
        return sorted(out.values(), key=lambda f: f["created"])

    def _new_id(self, taken: set[str]) -> str:
        while True:
            fid = "f" + secrets.token_hex(2)
            if fid not in taken:
                return fid

    # -------------------------------------------------------------- writes

    def add(self, text: str, category: str = "fact", source: str = "console") -> dict:
        """Write one fact. Only the subject (console) may write — ever."""
        if source not in FACT_SOURCES:
            raise ValueError(f"facts are subject-written only; source '{source}' refused")
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("empty fact")
        category = (category or "fact").strip() or "fact"
        live = self.facts()
        norm = text.lower()
        for f in live:
            if f["text"].lower() == norm:     # dedupe: refresh, not duplicate
                self._append({"op": "add", "id": f["id"], "text": text,
                              "category": category, "ts": _now(), "source": source})
                f.update(text=text, category=category, ts=self._clock())
                return f
        if len(live) >= self.max_facts:
            raise ValueError(f"memory full ({self.max_facts} facts) — forget something first")
        fid = self._new_id({f["id"] for f in live})
        entry = {"op": "add", "id": fid, "text": text, "category": category,
                 "ts": _now(), "source": source}
        self._append(entry)
        return {"id": fid, "text": text, "category": category,
                "ts": entry["ts"], "created": entry["ts"]}

    def forget(self, id_or_text: str) -> int:
        """Forget by exact id first, else by case-insensitive substring."""
        needle = (id_or_text or "").strip()
        if not needle:
            return 0
        live = self.facts()
        targets = [f for f in live if f["id"] == needle]
        if not targets:
            low = needle.lower()
            targets = [f for f in live if low in f["text"].lower()]
        for f in targets:
            self._append({"op": "forget", "id": f["id"], "ts": _now()})
        return len(targets)

    # --------------------------------------------------------------- reads

    def get(self, fid: str) -> dict | None:
        for f in self.facts():
            if f["id"] == fid:
                return f
        return None
