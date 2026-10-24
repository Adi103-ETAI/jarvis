"""jarvisd console -- the first text front-end of the assistant.

Wires the whole spine into one object:

    user line -> Gate A (console = direct intent)
              -> kernel turn on a JARVIS-owned session
              -> Gate B doorkeeper answers benign calls, parks the rest
              -> pending confirmations surface as one-time codes:
                     yes CODE   approve
                     no  CODE   refuse

Everything asked, decided, approved and refused lands in the journal and
the gate audit log.
"""

from __future__ import annotations

import datetime as _dt

from jarvis.audit import GateLog
from jarvis.confirm import ConfirmationBroker
from jarvis.gates.intent import classify
from jarvis.gates.tool import ToolGate
from jarvis.gates.worker import PermissionWorker
from jarvis.journal import Journal
from jarvis.kernel.adapter import KernelError
from jarvis.policy import Policy
from jarvis.registry import OwnedKernel, SessionRegistry

DEFAULT_WORKSPACE = "/home/sonic"
DEFAULT_MODEL = {"providerID": "opencode", "id": "glm-5.3-flash-free"}

HELP = """commands:
  (any text)        ask JARVIS to do something (goes through both gates)
  pending           show confirmations waiting for your code
  yes CODE          approve a pending action (code is one-time)
  no  CODE          refuse a pending action
  status            kernel, session, model, queue summary
  help              this text
  quit              shut the console down cleanly
"""


class JarvisConsole:
    def __init__(self, *, workspace=DEFAULT_WORKSPACE, model=None,
                 policy_path=None, journal_path=None, audit_path=None):
        self.workspace = workspace
        self.model = model or dict(DEFAULT_MODEL)
        self.audit = GateLog(audit_path)
        self.journal = Journal(journal_path)
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
        self.kernel.events_nonblocking().attach(self.events.append)

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

    def ask(self, text):
        """Console input -> Gate A -> kernel turn (Gate B watches the tools)."""
        verdict, reason = classify(text, "console")
        self.journal.add("gate-a", verdict=verdict, reason=reason,
                         text=text[:280], source="console")
        if verdict != "act":
            return "(" + reason + ") -- not launching a turn."

        sid = self.ensure_session()["id"]
        self.worker.set_trust(sid, "console")
        try:
            self.kernel.prompt(sid, text)
        except KernelError as e:
            return "kernel refused the turn: " + e.message
        self.journal.add("turn-started", session_id=sid, text=text[:280])
        return "turn launched."

    # --------------------------------------------------------- text output

    def replay_text(self, session_id):
        """Best-effort assistant text assembled from the event stream."""
        parts = []
        for e in self.events:
            if e.get("type") == "session.text.delta" and \
                    e.get("data", {}).get("sessionID") == session_id:
                t = e.get("data", {}).get("text") or ""
                if t:
                    parts.append(t)
        return "".join(parts).strip()

    def wait_turn(self, timeout=120.0):
        """Block until the current turn idles; return assistant text (or empty)."""
        if self.session is None:
            return ""
        sid = self.session["id"]
        try:
            self.kernel.wait(sid, timeout=timeout)
        except KernelError:
            return "(turn still running -- check back with status)"
        return self.replay_text(sid)

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
            "awaiting  : " + str(len(self.worker.pending_list())) + " confirmation(s)",
        ])

    def _pending(self):
        pend = self.worker.pending_list()
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
        return "no pending confirmation for code '" + code + "'."

    # ---------------------------------------------------------------- exit

    def close(self):
        self.worker.stop()
        for sid in self.registry.active_ids():
            try:
                self.kernel.delete_session(sid)
            except KernelError:
                pass
                

def main():
    """Interactive console loop: python3 -m jarvis"""
    import sys
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
        if len(sys.argv) > 1 and sys.argv[1] == "ask":
            pass
    finally:
        jc.close()
    return 0
