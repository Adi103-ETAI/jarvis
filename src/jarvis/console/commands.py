"""Console command surface -- status, confirmations, memory and model verbs.

Split from the core loop so the REPL wiring (app.py) stays readable.
ConsoleCommands is a mixin: JarvisConsole inherits it, so every method
keeps its self.* body exactly as before the split. parse_model_spec and
HELP live here too -- they are part of the command surface, not the loop.
"""

from __future__ import annotations

import datetime as _dt
import time

from jarvis.kernel.adapter import KernelError


def parse_model_spec(spec):
    """'provider/model-id' -> {providerID, id}; bare id -> opencode/<id>.
    Anything malformed -> None (caller keeps the current model)."""
    s = (spec or "").strip()
    if not s:
        return None
    if "/" in s:
        provider, _, mid = s.partition("/")
        provider, mid = provider.strip(), mid.strip()
        if not provider or not mid or "/" in mid:
            return None
        return {"providerID": provider, "id": mid}
    return {"providerID": "opencode", "id": s}

HELP = """commands:
  (any text)        ask JARVIS (foreground: waits for the answer)
  bg TEXT           ask in the background lane; announcer reports completion
  pending           show confirmations waiting for your code
  yes CODE          approve a pending action (code is one-time)
  no  CODE          refuse a pending action
  cancel            interrupt the current turn (JARVIS-owned sessions only)
  replay            print the latest assistant reply
  routines          list standing routines and their state
  run NAME          launch a routine now (today's scheduled fire is satisfied)
  routine NAME off|on   runtime toggle: may only tighten, never widen
  remember TEXT     store a durable fact (subject-written core memory)
  forget ID|TEXT    remove matching fact(s) from memory
  facts             show stored facts (newest first)
  recall QUERY      search facts + archived turns
  memory            memory stats (facts, archived turns, compactions)
  compact           compact this session; re-inject durable facts after
  model [P/M]       show or switch the kernel model (also JARVIS_MODEL env)
  models            list models the kernel offers
  skill             list installed skills (procedural memory)
  skill NAME        show a skill's full procedure
  skill NAME TASK   run TASK following that skill (body rides as context;
                    its tools still pass Gate B like any other turn)
  skill proposals   show staged skill drafts (learning-ritual output)
  skill approve NAME  install a staged draft (the only path to live skills)
  skill reject NAME   archive a staged draft (recoverable, never deleted)
  fact proposals    show staged memory changes (kernel-proposed)
  fact approve ID   apply a staged memory change (your approval IS the write)
  fact reject ID    refuse a staged memory change
  delegate TASK     child session for TASK; output returns as evidence
                    (never executed) and it cannot delegate further
  delegatebg TASK   same, background lane (announcer reports completion)
  delegates         list delegations and their state
  delegate-read SID print a delegation's captured output (evidence)
  status            kernel, session, model, memory, skills, queue summary
  help              this text
  quit              shut the console down cleanly
"""


class ConsoleCommands:
    """The command surface, mixed into JarvisConsole (see app.py)."""

    def _status(self):
        sids = self.registry.active_ids()
        armed = sum(1 for r in self.scheduler.routines if r.active)
        return "\n".join([
            "kernel    : " + self.kernel.base_url,
            "workspace : " + self.workspace,
            "model     : " + str(self.model.get("providerID", "?")) + "/" + str(self.model.get("id", "?")),
            "session   : " + (self.session["id"] if self.session else "(none yet)"),
            "owned     : " + str(len(sids)) + " session(s)",
            "routines  : " + str(armed) + "/" + str(len(self.scheduler.routines)) + " armed",
            "memory    : " + str(len(self.facts.facts())) + " facts, "
                          + str(len(self.archive.turns())) + " turns archived",
            "skills    : " + str(len(self.skills.list())) + " installed"
                          + " (" + str(self.skills.root) + ")",
            "awaiting  : " + str(len(self.worker.pending_list()) + len(self.store.list()))
            + " confirmation(s)",
        ])

    def _pending(self):
        now = time.time()
        pend = list(self.worker.pending_list())
        for p in self.store.list():
            pend.append({"code": p["code"],
                         "age_s": round(now - p.get("ts", now), 1),
                         "description": p.get("description", "(no description)")})
        if not pend:
            return "no confirmations waiting."
        return "\n".join(p["code"] + "  (" + str(p["age_s"]) + "s ago)  " + p["description"]
                         for p in pend)

    def _resolve(self, code, accept):
        if not code:
            return "usage: yes CODE / no CODE"
        if self.worker.resolve(code, accept):
            self.journal.add("confirmation", code=code,
                             outcome="approved" if accept else "refused")
            return "approved." if accept else "refused -- the kernel continues accordingly."
        if self.store.decide(code, accept):
            self.journal.add("confirmation", code=code,
                             outcome="approved" if accept else "refused",
                             channel="mcp-bridge")
            return ("approved -- the MCP tool call continues."
                    if accept else
                    "refused -- the MCP tool call will be told no.")
        return "no pending confirmation for code '" + code + "'."

    def _categorize(self, text):
        """Deterministic category hint from the subject's own wording."""
        low = text.lower()
        if low.startswith(("i prefer", "i like", "i hate", "i avoid", "i dislike")):
            return "preference"
        if low.startswith(("i am ", "my name", "i work", "i live", "my ")):
            return "identity"
        return "fact"

    def _remember(self, rest):
        if rest.startswith("that "):
            rest = rest[5:].strip()
        if not rest:
            return "usage: remember TEXT (e.g. remember that I prefer dark mode)"
        try:
            fact = self.facts.add(rest, self._categorize(rest), source="console")
        except ValueError as e:
            return "refused: " + str(e)
        self.journal.add("memory-op", op="add", id=fact["id"],
                         category=fact["category"], text=rest[:280])
        return "remembered (" + fact["id"] + " [" + fact["category"] + "]): " + fact["text"]

    def _forget(self, rest):
        if not rest:
            return "usage: forget ID | forget TEXT"
        n = self.facts.forget(rest)
        if n == 0:
            return "no matching fact in memory."
        self.journal.add("memory-op", op="forget", count=n, needle=rest[:280])
        return "forgot " + str(n) + " fact(s)."

    def _facts_list(self, limit=30):
        facts = list(reversed(self.facts.facts()))
        if not facts:
            return "memory is empty — teach me with: remember TEXT"
        shown = facts[:limit]
        lines = [str(len(facts)) + " fact(s) in memory:"]
        lines += [f["id"] + " [" + f["category"] + "] " + f["text"] for f in shown]
        if len(facts) > len(shown):
            lines.append("... (" + str(len(facts) - len(shown)) + " older; 'recall QUERY' searches everything)")
        return "\n".join(lines)

    def _recall_out(self, query):
        if not query:
            return "usage: recall QUERY"
        hits = self.memory.search(query, k=6)
        self.journal.add("memory-op", op="recall", query=query[:280], hits=len(hits))
        if not hits:
            return "nothing in memory about '" + query + "'."
        lines = []
        for h in hits:
            when = _dt.datetime.fromtimestamp(h["ts"]).strftime("%Y-%m-%d")
            body = h["text"][:220] + ("..." if len(h["text"]) > 220 else "")
            if h["kind"] == "fact":
                lines.append("[fact " + h["id"] + "] " + body)
            else:
                lines.append("[turn " + when + "] " + body)
        return "\n".join(lines)

    def _memory_status(self):
        facts = self.facts.facts()
        turns = self.archive.turns()
        compactions = self.archive.compactions()
        last_c = (_dt.datetime.fromtimestamp(compactions[-1]["ts"]).strftime("%Y-%m-%d %H:%M")
                  if compactions else "never")
        core = sum(1 for f in facts if f["category"] in ("identity", "preference"))
        return "\n".join([
            "facts     : " + str(len(facts)) + " (" + str(core) + " core: identity/preference)",
            "archived  : " + str(len(turns)) + " turns",
            "compacted : " + last_c,
        ])

    def _compact(self):
        if self.session is None:
            return "no session yet."
        sid = self.session["id"]
        if not self.registry.owns(sid):
            return "refused: not a JARVIS-owned session."
        try:
            out = self.compactor.run(sid)
        except KernelError as e:
            self.journal.add("compact-failed", session_id=sid, error=e.message)
            return "compaction failed: " + e.message
        out = ("compacted: snapshot of " + str(out["messages_before"])
               + " messages taken; " + str(out["facts_injected"])
               + " durable fact(s) re-injected.")
        ritual = self.learning.note_compaction()
        return out + ((" " + ritual) if ritual else "")

    # ---------------------------------------------------------------- model

    def _model_cmd(self, arg):
        if not arg:
            cur = str(self.model.get("providerID", "?")) + "/" + str(self.model.get("id", "?"))
            return ("model: " + cur
                    + "  (change: model provider/model-id, or JARVIS_MODEL env)")
        spec = parse_model_spec(arg)
        if spec is None:
            return "usage: model provider/model-id   (e.g. model opencode/qwen3-coder)"
        if self.session is not None and self.registry.owns(self.session["id"]):
            try:
                self.kernel.switch_model(self.session["id"],
                                         spec["providerID"], spec["id"])
            except KernelError as e:
                return "kernel refused the switch: " + e.message
        self.model = spec
        self.journal.add("model-switched", provider=spec["providerID"],
                         id=spec["id"],
                         session=self.session["id"] if self.session else None)
        return "model set to " + spec["providerID"] + "/" + spec["id"] \
            + " (this session and all future sessions)."

    # ---------------------------------------------------------------- skills

    def _skill_cmd(self, rest):
        """skill | skill NAME | skill NAME TASK | skill proposals |
        skill approve|reject NAME. Listing and showing are for the HUMAN;
        invoking composes the skill's body ahead of the task and launches
        a normal turn: Gate A classifies it, Gate B still judges every
        tool it tries, and the suggested lane is honored only if it is
        fg or bg. Approvals are the only path from staging to live."""
        parts = rest.split(None, 1)
        if parts and parts[0] == "proposals":
            return self._skill_proposals()
        if parts and parts[0] in ("approve", "reject"):
            if len(parts) < 2 or not parts[1].strip():
                return "usage: skill " + parts[0] + " NAME"
            return self._skill_review(parts[0], parts[1].strip())
        skills = self.skills.list()
        if not rest:
            if not skills:
                return ("no skills installed -- drop SKILL.md files into "
                        + str(self.skills.root)
                        + " (format: docs/phase6-skills.md)")
            lines = [str(len(skills)) + " skill(s) in " + str(self.skills.root) + ":"]
            for s in skills:
                tools = ", ".join(s.tools) if s.tools else "-"
                lines.append("- " + s.name + "  [lane " + (s.lane or "fg")
                             + "; tools " + tools + "]  " + s.description)
            return "\n".join(lines)
        parts = rest.split(None, 1)
        name, task = parts[0], (parts[1].strip() if len(parts) > 1 else "")
        s = self.skills.get(name)
        if s is None:
            return "no skill named '" + name + "'. ('skill' lists what is installed)"
        if not task:
            tools = ", ".join(s.tools) if s.tools else "-"
            head = (s.name + "  [lane " + (s.lane or "fg") + "; tools " + tools + "]"
                    + "\n" + s.path.as_posix()
                    + "\n" + ("-" * 48) + "\n" + s.description)
            return head + "\n\n" + s.body
        lane = s.lane if s.lane in ("fg", "bg") else "fg"
        self.journal.add("skill-invoked", skill=s.name, mode="explicit",
                         lane=lane, task=task[:280])
        return self.ask(self.skills.body_block(s) + "\n\nTask: " + task,
                        lane=lane, skills=False)

    def _skill_proposals(self):
        staged = self.skills.proposals()
        mem = self.staging.list()
        if not staged and not mem:
            return ("nothing staged — the learning ritual drafts here "
                    "after review windows ('skill proposals', 'fact proposals')")
        lines = [str(len(staged)) + " skill proposal(s), " + str(len(mem))
                 + " memory proposal(s) waiting:"]
        for s in staged:
            lines.append("- [skill] " + s.name + " — " + s.description
                         + "  ('skill approve " + s.name + "')")
        for e in mem:
            lines.append("- [" + e["action"] + "] " + e["id"] + ": "
                         + e["text"][:160] + "  ('fact approve " + e["id"] + "')")
        return "\n".join(lines)

    def _skill_review(self, verb, name):
        try:
            if verb == "approve":
                s = self.skills.approve_proposal(name)
                self.journal.add("skill-proposal", action="approve",
                                 skill=s.name, outcome="installed")
                return ("installed '" + s.name + "' — it is now visible to "
                        "the index and matching (Gate B still gates its tools).")
            path = self.skills.reject_proposal(name)
            self.journal.add("skill-proposal", action="reject", skill=name,
                             outcome="archived", path=path.as_posix())
            return ("archived '" + name + "' to " + path.as_posix()
                    + " (recoverable, never deleted).")
        except ValueError as e:
            return "refused: " + str(e)

    # ----------------------------------------------------------- fact staging

    def _fact_cmd(self, rest):
        """fact proposals | fact approve ID | fact reject ID — the human
        gate on the kernel's staged memory changes. The approval applies
        through the SAME subject-written path as 'remember' (D6: the
        kernel never writes facts; it proposes)."""
        parts = rest.split(None, 1)
        if not parts or parts[0] == "proposals":
            return self._memory_proposals()
        verb, pid = parts[0], (parts[1].strip() if len(parts) > 1 else "")
        if verb not in ("approve", "reject") or not pid:
            return "usage: fact proposals | fact approve ID | fact reject ID"
        entry = self.staging.resolve(pid, verb == "approve")
        if entry is None:
            return "no open proposal '" + pid + "'. ('fact proposals' lists them)"
        if verb == "approve":
            try:
                if entry["action"] == "remember":
                    f = self.facts.add(entry["text"], self._categorize(entry["text"]),
                                       source="console")
                    outcome = "remembered (" + f["id"] + ")"
                else:
                    n = self.facts.forget(entry["text"])
                    outcome = ("forgot " + str(n) + " fact(s)" if n
                               else "matched nothing (already gone)")
            except ValueError as e:
                outcome = "refused by the store: " + str(e)
        else:
            outcome = "refused — decision recorded"
        self.journal.add("memory-op", op="proposal-"
                         + ("approved" if verb == "approve" else "rejected"),
                         proposal=pid, action=entry["action"],
                         text=entry["text"][:280], outcome=outcome)
        return "proposal " + pid + " (" + entry["action"] + "): " + outcome

    def _memory_proposals(self):
        mem = self.staging.list()
        if not mem:
            return "no staged memory changes."
        return "\n".join("- " + e["id"] + " [" + e["action"] + "] " + e["text"][:160]
                         + "  ('fact approve " + e["id"] + "')" for e in mem)

    # -------------------------------------------------------------- delegates

    def _delegates(self):
        started = [e for e in self.journal.tail(200)
                   if e.get("kind") == "delegate-started"]
        if not started:
            return "no delegations yet ('delegate TASK' spawns one)."
        results = {e.get("child"): e for e in self.journal.tail(200)
                   if e.get("kind") == "delegate-result"}
        lines = []
        for e in reversed(started):
            sid = str(e.get("child", "?"))
            r = results.get(sid)
            status = ("done, " + str(r.get("chars", 0)) + " chars"
                      if r else "no result yet — 'delegate-read " + sid + "' tries")
            lines.append("- " + sid + "  " + str(e.get("task", ""))[:80]
                         + "  [" + status + "]")
        return "\n".join(lines)

    def _delegate_read(self, sid):
        sid = (sid or "").strip()
        if not sid:
            return "usage: delegate-read SESSION-ID  ('delegates' lists them)"
        text = self.replay_text(sid, 0)
        if not text:
            return ("no captured output for '" + sid + "' (still running, or "
                    "the console restarted since it ran).")
        return "-- delegate evidence (read-only; never executed) --\n" + text

    def _models_list(self):
        try:
            catalog = self.kernel.list_models()
        except KernelError as e:
            return "kernel refused: " + e.message
        names = sorted({str(m.get("providerID", "?")) + "/" + str(m.get("id", "?"))
                        for m in catalog or []})
        if not names:
            return "kernel returned no models."
        return str(len(names)) + " model(s):\n" + "\n".join(names[:40])
