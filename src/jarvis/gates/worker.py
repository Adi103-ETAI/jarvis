"""PermissionWorker — the doorkeeper thread.

Polls the session-scoped permission queue for JARVIS-owned sessions and,
for every pending request: consults the ToolGate, then either answers
immediately (benign -> once, forbidden/untrusted -> reject) or parks the
request and opens a one-time confirmation code for the human.

Pending confirmations never time out into approval. A request left
pending simply blocks that tool call — sometimes forever. That is the
-policy-.
"""

from __future__ import annotations

import threading

from jarvis.audit import GateLog
from jarvis.confirm import ConfirmationBroker
from jarvis.gates.tool import ToolGate
from jarvis.registry import SessionRegistry
from jarvis.kernel.adapter import Kernel, KernelError


class PermissionWorker:
    def __init__(self, kernel: Kernel, registry: SessionRegistry, gate: ToolGate,
                 broker: ConfirmationBroker, log: GateLog | None = None,
                 poll_seconds: float = 0.4):
        self.kernel = kernel
        self.registry = registry
        self.gate = gate
        self.broker = broker
        self.log = log or GateLog()
        self.poll_seconds = poll_seconds

        self.pending: dict[str, dict] = {}     # code -> {sessionID, requestID, description}
        self._seen_request_ids: set[str] = set()
        self._trust: dict[str, str] = {}       # sessionID -> trust of the live prompt
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ trust source

    def set_trust(self, session_id: str, trust: str) -> None:
        """Declare what caused the current turn in this session.

        'console'  — typed by the human at JARVIS's interface
        'ambient'  — parsed from environment speech; may never mutate
        'peer'     — arrived through a peer channel; never auto-acts
        'scheduled' — a standing job opened the turn
        """
        self._trust[session_id] = trust

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-permission-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_seconds: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — the doorkeeper must never die silently
                self.log.record("worker", "error", "tick raised; continuing")
            self._stop.wait(self.poll_seconds)

    def _tick(self) -> None:
        for session_id in self.registry.active_ids():
            try:
                requests = self.kernel.session_permission_requests(session_id)
            except KernelError:
                continue
            for request in requests:
                self._handle(session_id, request)

    # --------------------------------------------------------------- decision

    def _handle(self, session_id: str, request: dict) -> None:
        rid = request.get("id") or ""
        if not rid or rid in self._seen_request_ids:
            return
        self._seen_request_ids.add(rid)

        trust = self._trust.get(session_id, "console")
        verdict = self.gate.decide(request, trust)
        action = request.get("action", "?")
        resources = request.get("resources") or []

        if verdict.decision == "once":
            self.kernel.reply_permission(session_id, rid, "once")
            self.log.record("permission", "executed", verdict.note,
                            request_id=rid, action=action, resources=resources,
                            rule=verdict.rule, trust=trust)
        elif verdict.decision == "reject":
            self.kernel.reply_permission(session_id, rid, "reject")
            self.log.record("permission", "rejected", verdict.note,
                            request_id=rid, action=action, resources=resources,
                            rule=verdict.rule, trust=trust)
        else:  # confirm — park with a one-time code; stays pending until the human responds
            description = f"{action}: {' ; '.join(resources)}"
            code = self.broker.open(description)
            self.pending[code] = {"sessionID": session_id, "requestID": rid,
                                  "description": description}
            self.log.record("permission", "awaiting-human", verdict.note,
                            request_id=rid, action=action, resources=resources,
                            rule=verdict.rule, trust=trust, code=code)

    # --------------------------------------------------------- human channel

    def pending_list(self) -> list[dict]:
        return [{"code": code, **info} for code, info in sorted(self.pending.items())]

    def resolve(self, code: str, accept: bool, reply_trust: str = "console") -> bool:
        """Human typed a code (yes) or refused. Consumes exactly once."""
        item = self.pending.pop(code, None)
        if item is None:
            return False
        decision = "once" if accept else "reject"
        self.kernel.reply_permission(item["sessionID"], item["requestID"], decision)
        self.log.record("permission", "approved-by-human" if accept else "refused-by-human",
                        f"code={code} reply_trust={reply_trust}",
                        request_id=item["requestID"], action=item["description"])
        return True
