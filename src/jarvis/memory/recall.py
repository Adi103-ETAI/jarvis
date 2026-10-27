"""Deterministic recall over facts + archived turns.

Token overlap + exact-phrase bonus + recency boost. No embeddings, no
model -- the same query returns the same ranking, auditable and free.
"""

from __future__ import annotations

from jarvis.memory.archive import Archive
from jarvis.memory.common import _now, tokenize
from jarvis.memory.facts import FactStore


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
