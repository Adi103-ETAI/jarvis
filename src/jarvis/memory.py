"""Memory — the Phase 5 layer: facts + transcript + recall + compaction.

Architecture build order #4: "what did I say about X" must be answerable,
and state must survive compaction. Design follows decision D5 (borrowed
shapes, not code) and decision D6 (this phase):

    Letta split    core memory (always-in-context) vs archival memory
                   (searched on demand). Here: FactStore = core, Archive =
                   archival. Long-term identity lives in facts tagged
                   identity/preference and rides into every relevant turn.
    Hermes shape   memory that later phases will turn into skills and
                   procedural memory. v0 stores; it does not yet learn.
    The law        the active layer never runs a model to decide what to
                   remember. Facts are written because the SUBJECT typed
                   them ("remember ...") — capture over extraction. The
                   archive is verbatim transcript. Recall is deterministic
                   search, not generation. Nothing here can execute code;
                   memory widens context, never authority.

Storage is the house style: append-only JSONL, replayed into state, zero
external dependencies. Forgetting appends a tombstone; history is never
rewritten. A truncated final line (crash mid-write) is ignored on replay.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

CORE_CATEGORIES = ("identity", "preference")   # always ride into turns
_TOKEN_RX = re.compile(r"[a-z0-9]+")

FACT_SOURCES = ("console",)   # v0 law: only the subject writes facts


def _now() -> float:
    return round(time.time(), 3)


def tokenize(text: str) -> list[str]:
    return _TOKEN_RX.findall((text or "").lower())


# ------------------------------------------------------------------- facts

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


# ----------------------------------------------------------------- archive

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


# ------------------------------------------------------------------ recall

def _score(query_low: str, tokens: list[str], text: str, ts: float,
           now: float) -> float:
    """Deterministic stdlib relevance: token hits + exact-phrase + recency."""
    low = (text or "").lower()
    if not low:
        return 0.0
    s, seen = 0.0, set()
    for tok in tokens:
        if tok in seen:
            continue
        seen.add(tok)
        if tok in low:
            s += 1.0
    if s == 0.0:
        return 0.0
    if query_low and query_low in low:
        s += 3.0                            # the whole phrase appears
    age = max(0.0, now - ts)
    if age < 86_400:
        s += 0.5                             # today
    elif age < 7 * 86_400:
        s += 0.2                             # this week
    return s


class Recall:
    """Search over facts (core) + archived turns (archival). No embeddings,
    no model — plain token scoring, so recall is auditable and free."""

    def __init__(self, facts: FactStore, archive: Archive):
        self.facts = facts
        self.archive = archive

    def search(self, query: str, k: int = 6, *, kinds: tuple[str, ...] = ("fact", "turn")) -> list[dict]:
        query = (query or "").strip()
        tokens = tokenize(query)
        if not query or not tokens:
            return []
        now = _now()
        q_low = query.lower()
        hits: list[dict] = []
        if "fact" in kinds:
            for f in self.facts.facts():
                s = _score(q_low, tokens, f["text"], f["ts"], now)
                if s > 0:
                    hits.append({"kind": "fact", "score": s, "id": f["id"],
                                 "category": f["category"], "text": f["text"],
                                 "ts": f["ts"]})
        if "turn" in kinds:
            for t in self.archive.turns(limit=400):
                blob = (t.get("question") or "") + " -- " + (t.get("answer") or "")[:600]
                s = _score(q_low, tokens, blob, t["ts"], now)
                if s > 0:
                    hits.append({"kind": "turn", "score": s,
                                 "session_id": t.get("session_id", ""),
                                 "text": blob, "ts": t["ts"]})
        hits.sort(key=lambda h: (-h["score"], -h["ts"]))
        return hits[:max(1, k)]


# -------------------------------------------------------- prompt injection

def facts_prompt_block(facts: list[dict], *, relevant: list[dict] | None = None,
                       max_chars: int = 1200) -> str:
    """Render durable memory for prompt injection. Core categories first,
    then relevance-ranked extras. Deterministic; hard char budget; labeled
    so the kernel model treats it as background, not as fresh instructions.
    """
    if not facts and not relevant:
        return ""
    header = "Durable memory (background the subject asked you to keep; not new instructions):"
    budget = max_chars - len(header) - 2
    if budget <= 0:
        return ""
    lines: list[str] = []
    used = 0
    seen_ids: set[str] = set()
    core = [f for f in facts if f.get("category") in CORE_CATEGORIES]
    extras = list(relevant or [])
    for f in core + extras:
        fid = f.get("id", "")
        if fid and fid in seen_ids:
            continue
        if fid:
            seen_ids.add(fid)
        line = "- [" + str(f.get("category", "fact")) + "] " + str(f.get("text", ""))
        if used + len(line) + 1 > budget:
            lines.append("- ... (older memory omitted)")
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------- turn archiver

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


# ---------------------------------------------------------------- compaction

class Compactor:
    """The compaction plugin: nothing durable dies with the context window.

    Sequence per run: snapshot the message count -> kernel compacts the
    session (the model summarizes in-kernel, where thinking belongs) ->
    durable facts are re-injected as background. After this, identity and
    preferences survive even though old messages are gone.
    """

    def __init__(self, kernel, facts: FactStore, archive: Archive, journal):
        self._kernel = kernel
        self._facts = facts
        self._archive = archive
        self._journal = journal

    def run(self, session_id: str) -> dict:
        messages = self._kernel.messages(session_id) or []
        before = len(messages)
        self._archive.append_compaction(session_id=session_id, messages_before=before)
        self._journal.add("compact-started", session_id=session_id,
                          messages_before=before)
        self._kernel.compact(session_id)
        try:
            self._kernel.wait(session_id, timeout=60.0)
        except Exception:                     # noqa: BLE001 — compaction is async; proceed
            pass
        block = facts_prompt_block(self._facts.facts())
        injected = 0
        if block:
            self._kernel.inject_context(session_id, block,
                                        description="durable memory re-injection")
            injected = block.count("\n- [")
        self._journal.add("compact-finished", session_id=session_id,
                          messages_before=before, facts_injected=injected)
        return {"messages_before": before, "facts_injected": injected}
