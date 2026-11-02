"""Learning loop — the kernel proposes, the human authorizes (phase 7).

Hermes shapes, adapted through our laws:

    Hermes /loop   cadence + runaway guards (max_ticks, idle-only
                   wakeups, user precedence). Ours: the ritual fires only
                   AFTER a completed console turn (never mid-work), at
                   most once per review window, paused when too many
                   proposals are already waiting for the human, off
                   entirely with [learning] enabled=false — the analog
                   of max_ticks, set by the human in policy.toml.
    Hermes curator proposals are staged, never auto-applied; the worst
                   outcome is recoverable (reject archives the draft).
                   Ours: skills/_proposals/ + staged memory changes, and
                   `skill approve NAME` / `fact approve ID` are the only
                   paths from staging to live state.
    Refused        Hermes' model-managed skill lifecycle (usage counters,
                   auto-stale, LLM consolidation) — our lifecycle is:
                   human writes it, human approves a proposal, or it
                   does not exist. Also refused: the /loop judge (a
                   model deciding when to stop) — our guards are rules.

The ritual itself is ONE kernel turn, dispatched background-lane on the
console session after the trigger condition is met. Its reply is parsed
deterministically for fenced proposal blocks; the active layer never
acts on any other part of the reply. Nothing here executes anything.
"""

from __future__ import annotations

import re
import time

from jarvis.skills import parse_frontmatter, sanitize_name

SKILL_FENCE = re.compile(r"```skill-proposal\s*\n(.*?)```", re.DOTALL)
MEMORY_FENCE = re.compile(r"```memory-proposal\s*\n(.*?)```", re.DOTALL)

DEFAULTS = {"enabled": True, "every_n_turns": 20, "max_staged": 3,
            "review_hours": 20}

RITUAL_TEMPLATE = """[learning ritual — internal maintenance turn. Context, not user chat.]
Review the session evidence below and decide whether anything reusable is worth proposing to the subject. Output ONLY fenced proposal blocks in the exact formats shown, or nothing at all if nothing is worth proposing. Do not narrate, do not summarize, do not ask questions.

Session evidence:

{evidence}

Constraints:
- Installed skills (do not propose duplicates): {installed}
- Already waiting for review: {waiting} proposal(s) — the subject pauses you at {max_staged}
- Propose a skill only for a procedure that recurred or clearly will recur; steps must be concrete and safe to read.
- Propose memory only for durable corrections or preferences observed in the session, never one-off task details.

Output formats (exact):

```skill-proposal
---
name: kebab-case-name
description: one line
lane: fg
tools: [tool-names-the-procedure-needs]
---
numbered procedure steps
```

```memory-proposal
remember: durable fact worth keeping
forget: exact text or id of a now-wrong fact
```
"""


def parse_proposals(text: str) -> tuple[list[dict], list[tuple[str, str]], int]:
    """Deterministically extract proposals from a ritual reply.

    Returns (skill_drafts, memory_lines, malformed) where skill_drafts
    are {name, description, lane, tools, body} dicts and memory_lines are
    (action, text) pairs. Anything unparseable counts in `malformed` and
    is otherwise dropped — a bad draft never reaches staging half-formed.
    """
    drafts: list[dict] = []
    memory: list[tuple[str, str]] = []
    malformed = 0
    for capture in SKILL_FENCE.findall(text or ""):
        try:
            meta, body = parse_frontmatter(capture)
            name = sanitize_name(str(meta.get("name") or ""))
            description = str(meta.get("description") or "").strip()
            if not description or not body.strip():
                raise ValueError("incomplete proposal")
            raw_tools = meta.get("tools")
            if isinstance(raw_tools, str):
                raw_tools = [t.strip() for t in raw_tools.split(",") if t.strip()]
            drafts.append({"name": name, "description": description,
                           "body": body.strip(),
                           "lane": meta.get("lane") if meta.get("lane") in ("fg", "bg") else None,
                           "tools": raw_tools if isinstance(raw_tools, list) else []})
        except ValueError:
            malformed += 1
    for capture in MEMORY_FENCE.findall(text or ""):
        for raw in capture.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition(":")
            action = key.strip().lower()
            val = val.strip()
            if action in ("remember", "forget") and val:
                memory.append((action, val))
            elif line:
                malformed += 1
    return drafts, memory, malformed


class LearningLoop:
    """Trigger guards + one-ritual-at-a-time + deterministic staging."""

    def __init__(self, journal, skills, staging, *, cfg, launch, replay,
                 recent_turns, activity_counts, emit):
        self.journal = journal
        self.skills = skills
        self.staging = staging
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.launch = launch               # prompt -> dispatched? (console)
        self.replay = replay               # (sid, start_index) -> reply text
        self.recent_turns = recent_turns   # n -> list[str] (archive excerpts)
        self.activity_counts = activity_counts  # -> {journal kind: count}
        self.emit = emit                   # announcer nudge
        self.turns_since = 0
        self._watch: dict | None = None    # {sid, start} while a ritual turn runs

    # -------------------------------------------------------------- triggers

    def note_turn(self) -> str | None:
        """A console fg turn completed. Returns the review outcome (or
        None). Threshold resets even when guards skip — one missed window
        must not convert into a burst later."""
        self.turns_since += 1
        n = int(self.cfg.get("every_n_turns", DEFAULTS["every_n_turns"]))
        if n <= 0 or self.turns_since < n:
            return None
        self.turns_since = 0
        return self.review("every-%d-turns" % n)

    def note_compaction(self) -> str | None:
        """Compaction just ran — the transcript was summarized, which is
        exactly when reusable procedures surface."""
        return self.review("after-compaction")

    # ---------------------------------------------------------------- review

    def review(self, trigger: str) -> str | None:
        if not self.cfg.get("enabled", True):
            return None
        if self._watch is not None:
            return self._skip(trigger, "ritual already in flight")
        last = self._last_ritual_ts()
        if last is not None:
            if time.time() - last < float(self.cfg.get("review_hours", 20)) * 3600.0:
                return self._skip(trigger, "reviewed recently")
        waiting = len(self.skills.proposals()) + self.staging.unresolved_count()
        max_staged = int(self.cfg.get("max_staged", DEFAULTS["max_staged"]))
        if waiting >= max_staged:
            return self._skip(trigger,
                              "%d proposal(s) already await review" % waiting)
        prompt = RITUAL_TEMPLATE.format(
            evidence=self._evidence(),
            installed=", ".join(s.name for s in self.skills.list()) or "(none)",
            waiting=waiting, max_staged=max_staged)
        if not self.launch(prompt):
            return self._skip(trigger, "kernel unreachable — ritual not dispatched")
        self.journal.add("learning-ritual", stage="dispatched", trigger=trigger)
        return "ritual dispatched (" + trigger + ")"

    def _skip(self, trigger: str, reason: str) -> str:
        self.journal.add("learning-ritual", stage="skipped", trigger=trigger,
                         reason=reason)
        return "ritual skipped (" + reason + ")"

    def _last_ritual_ts(self) -> float | None:
        entries = self.journal.tail(200, kind="learning-ritual")
        return entries[-1]["ts"] if entries else None

    def _evidence(self) -> str:
        parts = []
        turns = self.recent_turns(8)
        if turns:
            parts.append("Recent archived turns (oldest first, trimmed):"
                         + "\n".join("- " + t for t in turns))
        counts = self.activity_counts()
        if counts:
            pretty = ", ".join(k + "=" + str(c) for k, c in sorted(counts.items()))
            parts.append("Recent journal activity: " + pretty)
        return "\n\n".join(parts) or "(no evidence yet)"

    # ----------------------------------------------------------- result path

    def begin_watch(self, session_id: str, start_index: int) -> None:
        """Arm the one-shot idle watcher for the dispatched ritual turn."""
        self._watch = {"sid": session_id, "start": start_index}

    def note_event(self, event: dict) -> None:
        """EventPipe subscriber: consume the ritual turn's completion."""
        if self._watch is None:
            return
        etype = str(event.get("type", ""))
        data = event.get("data") or event.get("properties") or {}
        if str(data.get("sessionID", "")) != self._watch["sid"]:
            return
        if not (etype == "session.idle" or etype.endswith(".idle")
                or etype == "session.execution.failed"):
            return
        watch, self._watch = self._watch, None
        failed = etype == "session.execution.failed"
        text = self.replay(watch["sid"], watch["start"]) if not failed else ""
        drafts, memory, malformed = parse_proposals(text)
        staged_skills: list[str] = []
        refused: list[str] = []
        for d in drafts:
            try:
                self.skills.stage_proposal(d["name"], d["description"], d["body"],
                                           lane=d["lane"], tools=d["tools"])
                staged_skills.append(d["name"])
            except ValueError as e:
                refused.append(str(e))
        staged_memory = 0
        for action, val in memory:
            try:
                self.staging.stage(action, val)
                staged_memory += 1
            except ValueError:
                refused.append("bad memory line dropped")
        outcome = ("staged %d skill(s), %d memory change(s)" %
                   (len(staged_skills), staged_memory)) if (staged_skills or staged_memory) \
            else ("kernel turn failed — nothing parsed" if failed
                  else "nothing worth proposing")
        self.journal.add("learning-ritual", stage="result", outcome=outcome,
                         skills=staged_skills, memory=staged_memory,
                         malformed=malformed, refused=refused)
        if staged_skills or staged_memory:
            detail = ", ".join(staged_skills) if staged_skills \
                else "%d memory change(s)" % staged_memory
            self.emit("[jarvis] learning ritual staged: " + detail
                      + "  ->  'skill proposals' / 'fact proposals' to review")
