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
from jarvis.pending_store import PendingStore
from jarvis.policy import Policy
from jarvis.registry import OwnedKernel, SessionRegistry

DEFAULT_WORKSPACE = "/home/sonic"
DEFAULT_MODEL = {"providerID": "opencode", "id": "glm-5.3-flash-free"}

HELP = """commands:
  (any text)        ask JARVIS (foreground: waits for the answer)
  bg TEXT           ask in the background lane; announcer reports completion
  pending           show confirmations waiting for your code
  yes CODE          approve a pending action (code is one-time)
  no  CODE          refuse a pending action
  cancel            interrupt the current turn (JARVIS-owned sessions only)
  replay            print the latest assistant reply
  status            kernel, session, model, queue summary
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
                 pending_root=None):
        self.workspace = workspace
        self.model = model or dict(DEFAULT_MODEL)
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
        # ONE event pipe, two subscribers: transcript capture + live announcer.
        # (events_nonblocking() builds a fresh pipe per call -- attach both
        # subscribers to the same pipe or the SSE stream would be duplicated.)
        self.announcer = Announcer(self.worker, self.store, on_line=self._emit,
                                   journal=self.journal)
        pipe = self.kernel.events_nonblocking()
        pipe.attach(self.events.append)
        pipe.attach(self.announcer.note_event)
        self.announcer.start()
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

    def ask(self, text, lane="fg"):
        """Console input -> Gate A -> kernel turn (Gate B watches the tools).

        lane="fg": wait for the turn and return the assistant text (or the
        pause notice when a human decision is needed mid-turn).
        lane="bg": fire and forget; the announcer reports completion.
        """
        verdict, reason = classify(text, "console")
        self.journal.add("gate-a", verdict=verdict, reason=reason,
                         text=text[:280], source="console")
        if verdict != "act":
            return "(" + reason + ") -- not launching a turn."

        sid = self.ensure_session()["id"]
        self.announcer.watch_sid = sid
        self.worker.set_trust(sid, "console")
        started_wall = time.time()
        self._turn_start = len(self.events)
        try:
            self.kernel.prompt(sid, text)
        except KernelError as e:
            return "kernel refused the turn: " + e.message
        self.journal.add("turn-started", session_id=sid, lane=lane, text=text[:280])
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
        if low.startswith("bg "):
            return self.ask(line[3:].strip(), lane="bg")
        if low.startswith("yes ") or low == "yes":
            return self._resolve(line[4:].strip(), True)
        if low.startswith("no ") or low == "no":
            return self._resolve(line[3:].strip(), False)
        return self.ask(line)

    def _status(self):
        sids = self.registry.active_ids()
        return "\n".join([
            "kernel    : " + self.kernel.base_url,
            "workspace : " + self.workspace,
            "model     : " + str(self.model.get("providerID", "?")) + "/" + str(self.model.get("id", "?")),
            "session   : " + (self.session["id"] if self.session else "(none yet)"),
            "owned     : " + str(len(sids)) + " session(s)",
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

    # ---------------------------------------------------------------- exit

    def close(self):
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
