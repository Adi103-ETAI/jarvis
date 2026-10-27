"""jarvisd console -- the first text front-end of the assistant.

Wires the whole spine into one object:

    user line -> Gate A (console = direct intent)
              -> kernel turn on a JARVIS-owned session (fg or bg lane)
              -> Gate B doorkeeper answers benign calls, parks the rest
              -> announcer surfaces one-time codes live, as they appear:
                     yes CODE   approve
                     no  CODE   refuse
              -> cancel interrupts the current turn (own sessions only)

Everything asked, decided, approved and refused lands in the journal and
the gate audit log. Turn latency (first token, total) is journalled for
the Phase 3 acceptance measurement.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
import time

from jarvis.announcer import Announcer
from jarvis.audit import GateLog
from jarvis.confirm import ConfirmationBroker
from jarvis.gates.intent import classify
from jarvis.gates.tool import ToolGate
from jarvis.gates.worker import PermissionWorker
from jarvis.journal import Journal
from jarvis.kernel.adapter import KernelError
from jarvis.memory import (Archive, Compactor, FactStore, Recall,
                           TurnArchiver, facts_prompt_block)
from jarvis.pending_store import PendingStore
from jarvis.policy import Policy
from jarvis.registry import OwnedKernel, SessionRegistry
from jarvis.scheduler import Scheduler

DEFAULT_WORKSPACE = "/home/sonic"
DEFAULT_MODEL = {"providerID": "opencode", "id": "glm-5.3-flash-free"}


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
  status            kernel, session, model, memory, queue summary
  help              this text
  quit              shut the console down cleanly
"""


def first_token_seconds(events, session_id, started_wall, start_index=0):
    """Seconds from prompt dispatch to the first text delta of THIS turn.

    Pure helper over the captured event list (phase 3 gate: first-token
    latency measurement). Kernel timestamps arrive in epoch units --
    milliseconds or seconds depending on the route -- so both are handled;
    anything undatable returns None instead of a wrong number.
    """
    for e in events[start_index:]:
        if str(e.get("type", "")) != "session.text.delta":
            continue
        if (e.get("data") or {}).get("sessionID") != session_id:
            continue
        try:
            t = float(e.get("created"))
        except (TypeError, ValueError):
            return None
        if t > 1e12:              # epoch milliseconds
            t /= 1000.0
        if t < started_wall - 1:  # clock-skew guard: ignore nonsensical deltas
            return None
        return max(0.0, t - started_wall)
    return None


class JarvisConsole:
    def __init__(self, *, workspace=DEFAULT_WORKSPACE, model=None,
                 policy_path=None, journal_path=None, audit_path=None,
                 pending_root=None, routines_path=None, memory_dir=None):
        self.workspace = workspace
        # Model resolution order: explicit arg > JARVIS_MODEL env > pinned default.
        self.model = model or parse_model_spec(os.environ.get("JARVIS_MODEL")) \
            or dict(DEFAULT_MODEL)
        self.audit = GateLog(audit_path)
        self.journal = Journal(journal_path)
        # Resolution channel shared with the MCP bridge process: same files,
        # so a "yes CODE" typed here unlocks a call parked in jarvis-mcp-gate.
        self.store = PendingStore(pending_root)
        self.registry = SessionRegistry()
        self.kernel = OwnedKernel(self.registry)
        self.policy = Policy(policy_path)
        self.broker = ConfirmationBroker()
        self.gate = ToolGate(self.policy, self.audit)
        self.worker = PermissionWorker(self.kernel, self.registry, self.gate,
                                       self.broker, self.audit)
        self.worker.start()
        self.session = None
        self.events = []
        # Memory subsystem (phase 5): facts + archive + recall + compaction.
        # Memory files live beside the journal so one tmp dir hermetically
        # contains a whole console in tests.
        mem_cfg = (getattr(self.policy, "raw_extra", {}) or {}).get("memory") or {}
        mem_root = memory_dir or self.journal.path.parent
        self.facts = FactStore(mem_root / "facts.jsonl",
                               max_facts=int(mem_cfg.get("max_facts", 512)))
        self.archive = Archive(mem_root / "archive.jsonl")
        self.memory = Recall(self.facts, self.archive)
        self.compactor = Compactor(self.kernel, self.facts, self.archive,
                                   self.journal)
        # ONE event pipe, four subscribers: transcript capture + turn
        # archiving + live announcer + scheduler. Events travel once per
        # subscriber -- attaching via separate pipes would duplicate the
        # SSE stream. The archiver sits right behind raw capture so the
        # just-finished turn's deltas are always in scope when idle lands.
        self.announcer = Announcer(self.worker, self.store, on_line=self._emit,
                                   journal=self.journal)
        self.scheduler = Scheduler(self, routines_path=routines_path,
                                   journal=self.journal)
        self.archiver = TurnArchiver(self.events, self.journal, self.archive,
                                     self.registry.owns)
        pipe = self.kernel.events_nonblocking()
        pipe.attach(self.events.append)
        pipe.attach(self.archiver.note_event)
        pipe.attach(self.announcer.note_event)
        pipe.attach(self.scheduler.note_event)
        self.announcer.start()
        self.scheduler.start()
        self._turn_start = 0      # events index where the current turn begins
        self._print_lock = threading.Lock()

    # --------------------------------------------------------------- output

    def _emit(self, line):
        """Thread-safe line push from the announcer; redraw the prompt."""
        with self._print_lock:
            print("\n" + line, flush=True)
            print("j> ", end="", flush=True)

    # ------------------------------------------------------------- session

    def ensure_session(self):
        if self.session is None:
            title = "jarvisd-" + _dt.datetime.now().strftime("%Y%m%d")
            self.session = self.kernel.create_session(
                directory=self.workspace, model=self.model, title=title)
            self.registry.add(self.session["id"], title=title, trusted=True)
            self.journal.add("session-opened", session_id=self.session["id"],
                             workspace=self.workspace)
        return self.session

    # ---------------------------------------------------------- main entry

    def ask(self, text, lane="fg", source="console", session_id=None):
        """Input -> Gate A -> kernel turn (Gate B watches the tools).

        source       trust provenance: "console" | "scheduled" (routines);
                     Gate A classifies by source, Gate B judges by it.
        session_id   run on a specific JARVIS-owned session (the scheduler's
                     dedicated routine session) instead of the console one.
        lane="fg"    wait for the turn and return the assistant text (or the
                     pause notice when a human decision is needed mid-turn).
        lane="bg"    fire and forget; the announcer reports completion.
        """
        verdict, reason = classify(text, source)
        self.journal.add("gate-a", verdict=verdict, reason=reason,
                         text=text[:280], source=source)
        if verdict != "act":
            return "(" + reason + ") -- not launching a turn."

        if session_id is not None and self.registry.owns(session_id):
            sid = session_id                  # pre-ensured (scheduler session)
        else:
            try:
                sid = self.ensure_session()["id"]
            except KernelError as e:
                return "kernel unreachable -- cannot open a session: " + e.message
        if source == "console":
            self.announcer.watch_sid = sid    # routine turns narrate via scheduler
        self.worker.set_trust(sid, source)
        started_wall = time.time()
        self._turn_start = len(self.events)
        # Durable memory rides in as background context (core facts always,
        # plus relevance-ranked extras). The ORIGINAL text stays what Gate A
        # classified and what the journal records; the prefix is labeled so
        # the kernel treats it as memory, not as fresh instructions.
        mem_block = self._memory_prefix(text) if source in ("console", "scheduled") else ""
        prompt_text = (mem_block + "\n\n" + text) if mem_block else text
        try:
            self.kernel.prompt(sid, prompt_text)
        except KernelError as e:
            return "kernel refused the turn: " + e.message
        self.journal.add("turn-started", session_id=sid, lane=lane, text=text[:280],
                         memory_chars=len(mem_block))
        if lane == "bg":
            return "launched in background -- announcer reports when it idles ('replay' reads it)."
        reply = self.wait_turn()
        ft = first_token_seconds(self.events, sid, started_wall, self._turn_start)
        self.journal.add("turn-metrics", session_id=sid, lane="fg",
                         first_token_s=(round(ft, 3) if ft is not None else None),
                         total_s=round(time.time() - started_wall, 2))
        return reply or "(no assistant text captured -- try replay or see journal)"

    # --------------------------------------------------------- text output

    def replay_text(self, session_id, start_index=None):
        """Best-effort assistant text for one turn, from the event stream."""
        if start_index is None:
            start_index = self._turn_start
        parts = []
        for e in self.events[start_index:]:
            if e.get("type") == "session.text.delta" and \
                    e.get("data", {}).get("sessionID") == session_id:
                t = e.get("data", {}).get("text") or ""
                if t:
                    parts.append(t)
        return "".join(parts).strip()

    def wait_turn(self, timeout=240.0, chunk=5.0):
        """Foreground lane: block until the turn idles, WITH one escape hatch.

        Polls in chunks instead of one long server-side block so the console
        yields the moment a human decision is needed -- otherwise a turn
        waiting on a confirm code would hold hostage the very person who
        must type the code. Returns assistant text, a pause notice
        ("(paused ...)") or a still-running notice.
        """
        if self.session is None:
            return ""
        sid = self.session["id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pend = self.worker.pending_list() + [{"code": p["code"]}
                                                 for p in self.store.list()]
            if pend:
                codes = ", ".join(p["code"] for p in pend)
                return ("(paused -- awaiting your decision: " + codes
                        + "  ->  yes CODE / no CODE, then 'replay')")
            try:
                self.kernel.wait(sid, timeout=min(chunk, max(0.1, deadline - time.monotonic())))
                return self.replay_text(sid)   # server says: idle
            except KernelError:
                pass  # per-chunk timeout or transient error -- poll until deadline
        return "(turn still running -- check back with status)"

    # -------------------------------------------------------------- actions

    def cancel(self):
        """Interrupt the current turn -- subject rule applies (own sessions only)."""
        if self.session is None:
            return "no session to cancel."
        sid = self.session["id"]
        try:
            stopped = self.kernel.interrupt(sid)
        except KernelError as e:
            return "cancel refused: " + e.message
        self.journal.add("turn-cancelled", session_id=sid, stopped=stopped)
        return "execution stopped." if stopped else "nothing was running."

    # ------------------------------------------------------------ commands

    def handle(self, line):
        line = line.strip()
        low = line.lower()
        if not line:
            return ""
        if low in ("quit", "exit"):
            return "__quit__"
        if low in ("help", "?"):
            return HELP
        if low == "status":
            return self._status()
        if low == "pending":
            return self._pending()
        if low == "cancel":
            return self.cancel()
        if low == "replay":
            if self.session is None:
                return "no session yet."
            return self.replay_text(self.session["id"]) or "(no assistant text for the latest turn)"
        if low == "routines":
            return "\n".join(self.scheduler.list_lines())
        if low.startswith("run "):
            return self.scheduler.run_now(line[4:].strip())
        if low == "routine" or low.startswith("routine "):
            parts = line[8:].split() if low.startswith("routine ") else []
            if len(parts) == 2 and parts[1] in ("off", "on"):
                return self.scheduler.runtime_toggle(parts[0], parts[1] == "on")
            return "usage: routine NAME off|on"
        if low == "memory":
            return self._memory_status()
        if low.startswith("remember "):
            return self._remember(line[9:].strip())
        if low.startswith("forget "):
            return self._forget(line[7:].strip())
        if low == "facts":
            return self._facts_list()
        if low.startswith("recall "):
            return self._recall_out(line[7:].strip())
        if low.startswith("what did i say about "):
            return self._recall_out(line[21:].strip())
        if low.startswith("what do you know about "):
            return self._recall_out(line[23:].strip())
        if low.startswith("what do you remember about "):
            return self._recall_out(line[27:].strip())
        if low in ("what do you remember", "what do you remember?"):
            return self._facts_list(limit=10)
        if low == "compact":
            return self._compact()
        if low == "model" or low.startswith("model "):
            return self._model_cmd(line[5:].strip())
        if low == "models":
            return self._models_list()
        if low.startswith("bg "):
            return self.ask(line[3:].strip(), lane="bg")
        if low.startswith("yes ") or low == "yes":
            return self._resolve(line[4:].strip(), True)
        if low.startswith("no ") or low == "no":
            return self._resolve(line[3:].strip(), False)
        return self.ask(line)

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

    # --------------------------------------------------------------- memory

    def _memory_prefix(self, text):
        """Core facts always ride in; relevance-ranked extras join when the
        prompt actually touches them. Empty string when memory is empty or
        nothing matches — a turn never pays for memory it does not use."""
        facts = self.facts.facts()
        if not facts:
            return ""
        cfg = (getattr(self.policy, "raw_extra", {}) or {}).get("memory") or {}
        top_k = int(cfg.get("relevant_top_k", 3))
        max_chars = int(cfg.get("inject_max_chars", 1200))
        by_id = {f["id"]: f for f in facts}
        relevant = [by_id[h["id"]] for h in self.memory.search(text, k=top_k,
                                                               kinds=("fact",))
                    if h["id"] in by_id]
        return facts_prompt_block(facts, relevant=relevant, max_chars=max_chars)

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
        return ("compacted: snapshot of " + str(out["messages_before"])
                + " messages taken; " + str(out["facts_injected"])
                + " durable fact(s) re-injected.")

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

    # ---------------------------------------------------------------- exit

    def close(self):
        self.scheduler.stop()
        self.announcer.stop()
        self.worker.stop()
        for sid in self.registry.active_ids():
            try:
                self.kernel.delete_session(sid)
            except KernelError:
                pass
                

def main():
    """Interactive console loop: python3 -m jarvis"""
    jc = JarvisConsole()
    print("JARVIS console -- type help for commands.")
    try:
        while True:
            try:
                line = input("j> ")
            except EOFError:
                break
            out = jc.handle(line)
            if out == "__quit__":
                break
            if out:
                print(out)
    finally:
        jc.close()
    return 0
