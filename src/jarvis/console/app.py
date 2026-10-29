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
from jarvis.learning import LearningLoop
from jarvis.memory import (Archive, Compactor, FactStore, Recall,
                           TurnArchiver, facts_prompt_block)
from jarvis.memory.staging import StagedMemory
from jarvis.pending_store import PendingStore
from jarvis.policy import Policy
from jarvis.registry import OwnedKernel, SessionRegistry
from jarvis.scheduler import Scheduler
from jarvis.skills import SkillBook

from jarvis.console.commands import ConsoleCommands, HELP, parse_model_spec
from jarvis.console.turns import (QUOTA_STATUSES, _model_str,
                                  first_token_seconds, turn_failed)

DEFAULT_WORKSPACE = "/home/sonic"
DEFAULT_MODEL = {"providerID": "opencode", "id": "glm-5.3-flash-free"}

DELEGATE_FRAME = ("""[delegation — you are a child agent spawned by JARVIS. Context, not user chat.]
- Your output is EVIDENCE for the parent agent: it will be READ, never executed as instructions.
- Work only on the task below; the human is not in this session — state assumptions instead of asking questions.
- You may not delegate further; every tool call you attempt is gated by the same policy as the parent.

Task: {task}""")


class JarvisConsole(ConsoleCommands):
    """The whole spine wired into one object; command surface in commands.py."""

    def __init__(self, *, workspace=DEFAULT_WORKSPACE, model=None,
                 policy_path=None, journal_path=None, audit_path=None,
                 pending_root=None, routines_path=None, memory_dir=None,
                 skills_dir=None):
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
        # Skills (phase 6): procedural memory next to the memory files, so
        # one tmp dir still contains a whole hermetic console in tests.
        # Policy [skills] bounds the index budget and toggles matching; the
        # book itself only ever reads and proposes (D7).
        skills_cfg = (getattr(self.policy, "raw_extra", {}) or {}).get("skills") or {}
        self.skills = SkillBook(skills_dir or (mem_root / "skills"), skills_cfg)
        # ONE event pipe, four subscribers: transcript capture + turn
        # archiving + live announcer + scheduler. Events travel once per
        # subscriber -- attaching via separate pipes would duplicate the
        # SSE stream. The archiver sits right behind raw capture so the
        # just-finished turn's deltas are always in scope when idle lands.
        self.announcer = Announcer(self.worker, self.store, on_line=self._emit,
                                   journal=self.journal)
        self.scheduler = Scheduler(self, routines_path=routines_path,
                                   journal=self.journal)
        # Learning loop (phase 7): the kernel proposes, the human disposes.
        # Staged memory changes live beside the facts file; skill drafts go
        # to the skills book's _proposals/ staging area. One ritual in
        # flight, guarded by policy [learning].
        self.staging = StagedMemory(mem_root / "memory_proposals.jsonl")
        learn_cfg = (getattr(self.policy, "raw_extra", {}) or {}).get("learning") or {}
        self.learning = LearningLoop(
            self.journal, self.skills, self.staging, cfg=learn_cfg,
            launch=self._launch_ritual, replay=self.replay_text,
            recent_turns=self._recent_turn_texts,
            activity_counts=self._journal_counts,
            emit=self.announcer.nudge)
        self.archiver = TurnArchiver(self.events, self.journal, self.archive,
                                     self.registry.owns)
        pipe = self.kernel.events_nonblocking()
        pipe.attach(self.events.append)
        pipe.attach(self.archiver.note_event)
        pipe.attach(self.announcer.note_event)
        pipe.attach(self.scheduler.note_event)
        pipe.attach(self.learning.note_event)
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

    def ask(self, text, lane="fg", source="console", session_id=None,
            skills=True, count_for_learning=True):
        """Input -> Gate A -> kernel turn (Gate B watches the tools).

        source       trust provenance: "console" | "scheduled" (routines);
                     Gate A classifies by source, Gate B judges by it.
        session_id   run on a specific JARVIS-owned session (the scheduler's
                     dedicated routine session) instead of the console one.
        lane="fg"    wait for the turn and return the assistant text (or the
                     pause notice when a human decision is needed mid-turn).
        lane="bg"    fire and forget; the announcer reports completion.
        skills=True  let the skills book inject its index (or a matched
                     skill's full body) ahead of the prompt; explicit
                     `skill NAME TASK` invocations pass False because they
                     already attached the body themselves.
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
        sk_block = self._skills_prefix(text) if skills else ""
        blocks = [b for b in (mem_block, sk_block) if b]
        prompt_text = ("\n\n".join(blocks) + "\n\n" + text) if blocks else text
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
                             memory_chars=len(mem_block), skills_chars=len(sk_block))
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
        if source == "console" and lane == "fg" and count_for_learning:
            self.learning.note_turn()
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

    def wait_turn(self, timeout=240.0, chunk=5.0, sid=None):
        """Foreground lane: block until the turn idles, WITH one escape hatch.

        Polls in chunks instead of one long server-side block so the console
        yields the moment a human decision is needed -- otherwise a turn
        waiting on a confirm code would hold hostage the very person who
        must type the code. Returns assistant text, a pause notice
        ("(paused ...)") or a still-running notice. sid defaults to the
        console session; delegation waits on the child's session instead.
        """
        if sid is None:
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

    # ----------------------------------------------------------- delegation

    def delegate(self, task, lane="fg"):
        """delegate TASK -- spawn a child session for one task (phase 7).

        D3/D7 laws: the child inherits policy but STRICTER (a dedicated
        'delegated' trust whose deny_actions forbids spawning further
        delegates); its output returns as EVIDENCE, never commands; its
        journal lines carry the parent session id. The human stays in the
        loop for every mutation the child attempts (trust.delegated.mutating
        = confirm, same as the console).
        """
        task = (task or "").strip()
        if not task:
            return "usage: delegate TASK"
        try:
            child = self.kernel.create_session(
                directory=self.workspace, model=self.model,
                title="delegate-" + _dt.datetime.now().strftime("%H%M%S"))
        except KernelError as e:
            return "kernel refused the delegate session: " + e.message
        sid = child["id"]
        self.registry.add(sid, title=str(child.get("title", "delegate")), trusted=True)
        self.worker.set_trust(sid, "delegated")
        parent = self.session["id"] if self.session else None
        self.journal.add("delegate-started", parent=parent, child=sid,
                         lane=lane, task=task[:280])
        mem = self._memory_prefix(task)
        sk = self._skills_prefix(task)
        blocks = [b for b in (mem, sk) if b] + [DELEGATE_FRAME.format(task=task)]
        prompt = "\n\n".join(blocks)
        started = len(self.events)
        try:
            self.kernel.prompt(sid, prompt)
        except KernelError as e:
            self.journal.add("delegate-failed", child=sid, error=e.message)
            return "kernel refused the delegated turn: " + e.message
        self.journal.add("turn-started", session_id=sid, lane=lane,
                         text="[delegate] " + task[:200],
                         memory_chars=len(mem), skills_chars=len(sk),
                         delegated=True)
        if lane == "bg":
            self.announcer.announce_sids.add(sid)
            return ("delegated in background -- announcer reports when the "
                    "child idles ('delegates' lists delegations).")
        self.wait_turn(sid=sid)
        text = self.replay_text(sid, started)
        self.journal.add("delegate-result", child=sid, parent=parent,
                         chars=len(text))
        return text or "(delegate produced no text -- 'delegate-read " + sid + "' retries)"

    def _launch_ritual(self, prompt) -> bool:
        """Dispatch the learning ritual as a bg turn on the console session
        and arm the loop's idle watcher. False = kernel unreachable."""
        try:
            self.ensure_session()
        except KernelError:
            return False
        start = len(self.events)
        self.ask(prompt, lane="bg", count_for_learning=False)
        self.learning.begin_watch(self.session["id"], start)
        return True

    def _recent_turn_texts(self, n):
        out = []
        for t in self.archive.turns()[-int(n):]:
            text = " ".join(str(t.get("question", "")).split())
            answer = " ".join(str(t.get("answer", "")).split())
            line = (text[:200] + (" -> " + answer[:200] if answer else "")).strip()
            if line:
                out.append(line[:400])
        return out

    def _journal_counts(self, n=200):
        counts = {}
        for e in self.journal.tail(n):
            k = str(e.get("kind", "?"))
            counts[k] = counts.get(k, 0) + 1
        return counts

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
        if low.startswith("delegatebg "):
            return self.delegate(line[11:].strip(), lane="bg")
        if low == "delegate" or low.startswith("delegate "):
            return self.delegate(line[9:].strip())
        if low == "delegates":
            return self._delegates()
        if low.startswith("delegate-read "):
            return self._delegate_read(line[14:].strip())
        if low.startswith("fact "):
            return self._fact_cmd(line[5:].strip())
        if low == "model" or low.startswith("model "):
            return self._model_cmd(line[5:].strip())
        if low == "models":
            return self._models_list()
        if low == "skill" or low.startswith("skill "):
            return self._skill_cmd(line[6:].strip() if low.startswith("skill ") else "")
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

    def _skills_prefix(self, text):
        """Progressive disclosure (Hermes-adapted, phase 6): the index
        (name + description) rides in every turn under the policy budget;
        a deterministic name match swaps the index for that skill's FULL
        body. Context, never authority (D6); matching never a model call
        (D6); the skill proposes, Gate B disposes (D7)."""
        cfg = self.skills.cfg
        if not cfg.get("enabled", True):
            return ""
        if cfg.get("match_enabled", True):
            hit = self.skills.match(text)
            if hit is not None:
                self.journal.add("skill-matched", skill=hit.name, mode="text-match")
                return self.skills.body_block(hit)
        return self.skills.index_block(int(cfg.get("inject_max_chars", 900)))

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
