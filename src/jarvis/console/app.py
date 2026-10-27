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

Package layout: app.py (this file) is the core loop -- wiring, ask() with
model failover, wait/cancel, command dispatch, main(). commands.py is the
command surface (status/pending/memory/model verbs) as a mixin class.
turns.py holds pure turn-scan helpers (latency, failure, quota constants).
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

from jarvis.console.commands import ConsoleCommands, HELP, parse_model_spec
from jarvis.console.turns import (QUOTA_STATUSES, _model_str,
                                  first_token_seconds, turn_failed)

DEFAULT_WORKSPACE = "/home/sonic"
DEFAULT_MODEL = {"providerID": "opencode", "id": "glm-5.3-flash-free"}


class JarvisConsole(ConsoleCommands):
    """The whole spine wired into one object; command surface in commands.py."""

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
        # Model failover chain (phase 5.1): read once from policy at boot.
        # The list is written by the human (same law as routines.toml -- the
        # file is the approval); runtime never widens it.
        model_cfg = (getattr(self.policy, "raw_extra", {}) or {}).get("model") or {}
        self.failover_chain = []
        for spec in (model_cfg.get("failover") or []):
            m = parse_model_spec(spec) if isinstance(spec, str) else None
            if m and m not in self.failover_chain:
                self.failover_chain.append(m)
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
        # Durable memory rides in as background context (core facts always,
        # plus relevance-ranked extras). The ORIGINAL text stays what Gate A
        # classified and what the journal records; the prefix is labeled so
        # the kernel treats it as memory, not as fresh instructions.
        mem_block = self._memory_prefix(text) if source in ("console", "scheduled") else ""
        prompt_text = (mem_block + "\n\n" + text) if mem_block else text
        # Model failover (phase 5.1): try the current model, then the policy
        # chain, on rate-limit refusals (dispatch) or mid-turn model death
        # (execution.failed). Bounded by chain length; the winner becomes
        # the sticky default. Deterministic end to end -- no judgement here,
        # just the ordered list the human wrote in policy.toml.
        chain = [dict(self.model)] + [m for m in self.failover_chain if m != self.model]
        original_model = dict(self.model)
        failover_reason = None
        attempt = 0
        reply = None
        while True:
            model = chain[attempt]
            if attempt > 0:
                try:
                    self.kernel.switch_model(sid, model["providerID"], model["id"])
                except KernelError as e:
                    self.journal.add("model-failover", stage="switch-refused",
                                     model=_model_str(model), error=e.message)
                    if attempt + 1 < len(chain):
                        attempt += 1
                        continue
                    return "no failover model could be activated (last error: " + e.message + ")"
            self._turn_start = len(self.events)
            try:
                self.kernel.prompt(sid, prompt_text)
            except KernelError as e:
                if e.status in QUOTA_STATUSES and attempt + 1 < len(chain):
                    nxt = chain[attempt + 1]
                    self._note_failover(model, nxt, "rate-limited", status=e.status)
                    self.announcer.nudge("[jarvis] " + _model_str(model)
                                         + " hit its limit -- failing over to "
                                         + _model_str(nxt))
                    failover_reason = "rate-limited"
                    attempt += 1
                    continue
                return "kernel refused the turn: " + e.message
            self.journal.add("turn-started", session_id=sid, lane=lane, text=text[:280],
                             memory_chars=len(mem_block))
            if lane == "bg":
                self._stick_model(chain, attempt, original_model, failover_reason)
                return "launched in background -- announcer reports when it idles ('replay' reads it)."
            reply = self.wait_turn()
            if turn_failed(self.events, sid, self._turn_start):
                if attempt + 1 < len(chain):
                    nxt = chain[attempt + 1]
                    self._note_failover(model, nxt, "turn-failed")
                    self.announcer.nudge("[jarvis] " + _model_str(model)
                                         + " died mid-turn -- retrying on "
                                         + _model_str(nxt))
                    failover_reason = "turn-failed"
                    attempt += 1
                    continue        # re-prompt the same text on the next model
                self.journal.add("turn-failed", session_id=sid,
                                 note="execution failed; no failover models left")
            break
        self._stick_model(chain, attempt, original_model, failover_reason)
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

    def _note_failover(self, from_m, to_m, reason, **extra):
        self.journal.add("model-failover",
                         **{"from": _model_str(from_m), "to": _model_str(to_m),
                            "reason": reason, **extra})

    def _stick_model(self, chain, attempt, original_model, reason):
        """The failover winner becomes the default for future turns, so the
        next dispatch does not repeat the dead model's refusal."""
        if chain[attempt] != original_model:
            self.model = dict(chain[attempt])
            self._note_failover(original_model, self.model,
                                reason or "failover", sticky=True)

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
