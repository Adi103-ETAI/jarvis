"""JARVIS kernel adapter -- the stable ABI between the active layer and OpenCode.

Every method maps to exactly ONE operation in docs/opencode-api.lock.json
(OpenCode v2.1.4, experimental HTTP surface). No policy lives here: this is a
dumb, auditable pipe. Decisions belong to Gate A / Gate B (Phase 2).

Package layout: config.py (where/who), http.py (transport + SSE parse),
events.py (EventPipe), errors.py (KernelError). This module is the API
surface itself: one flat method per locked operation, inherited transport.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from jarvis.kernel.config import DEFAULT_BASE, KERNEL_VERSION, SERVICE_JSON  # noqa: F401 (re-exported)
from jarvis.kernel.errors import KernelError  # noqa: F401 (re-exported)
from jarvis.kernel.events import EventPipe  # noqa: F401 (re-exported)
from jarvis.kernel.http import KernelTransport

__all__ = ["Kernel", "KernelError", "EventPipe",
           "KERNEL_VERSION", "DEFAULT_BASE", "SERVICE_JSON"]


class Kernel(KernelTransport):
    """One connection to one OpenCode service instance.

    One method per locked operation; the class is deliberately flat so
    each method reads as the ABI record of one endpoint.
    """

    # ------------------------------------------------------------- lifecycle

    def create_session(
        self,
        *,
        directory: str,
        agent: Optional[str] = None,
        title: Optional[str] = None,
        parent: Optional[str] = None,
        permissions: Optional[list[dict]] = None,  # [{action, resource, effect: allow|deny|ask}]
        model: Optional[dict] = None,              # {"id": ..., "providerID": ..., "variant"?}
        metadata: Optional[dict] = None,
    ) -> dict:
        """POST /api/session — profile (agent) and workspace (directory) are independent."""
        body: dict = {"location": {"directory": directory}}
        if agent is not None:
            body["agent"] = agent
        if title is not None:
            body["title"] = title
        if parent is not None:
            body["parentID"] = parent
        if permissions is not None:
            body["permissions"] = permissions
        if model is not None:
            body["model"] = model
        if metadata is not None:
            body["metadata"] = metadata
        return self._data(self._request("POST", "/api/session", body))

    def sessions(self) -> list[dict]:
        """GET /api/session"""
        return self._data(self._request("GET", "/api/session")) or []

    def active_sessions(self) -> dict:
        """GET /api/session/active — returns {sessionID: {type: running|...}}.

        DANGER: includes sessions the active layer did not create (e.g. an
        interactive TUI session). JARVIS must NEVER interrupt or delete a
        session it did not spawn; a stray interrupt here aborts someone
        else's running turn — including, in principle, our own launcher.
        """
        return self._data(self._request("GET", "/api/session/active")) or {}

    def delete_session(self, session_id: str) -> None:
        """DELETE /api/session/{sid} — only ever call on sessions JARVIS created."""
        self._request("DELETE", f"/api/session/{session_id}")

    # ----------------------------------------------------------------- turns

    def prompt(self, session_id: str, text: str, *, files: Optional[list[dict]] = None,
               agents: Optional[list[dict]] = None, skills: Optional[list[dict]] = None) -> dict:
        """POST /api/session/{sid}/prompt — returns the enqueued USER message, not the reply."""
        body: dict = {"text": text}
        if files:
            body["files"] = files
        if agents:
            body["agents"] = agents
        if skills:
            body["skills"] = skills
        return self._data(self._request("POST", f"/api/session/{session_id}/prompt", body))

    def steer(self, session_id: str, text: str, *, delivery: str = "steer",
              description: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
        """POST /api/session/{sid}/synthetic — inject a message mid/next turn.

        delivery="steer" hijacks the current turn; delivery="queue" waits for idle.
        This is also the (safe, write-only) way to inject context into a session.
        """
        if delivery not in ("steer", "queue"):
            raise KernelError("delivery must be 'steer' or 'queue'", tag="InvalidRequestError")
        body: dict = {"text": text, "delivery": delivery}
        if description is not None:
            body["description"] = description
        if metadata is not None:
            body["metadata"] = metadata
        return self._data(self._request("POST", f"/api/session/{session_id}/synthetic", body))

    # Alias for architecture naming; same locked operation (delivery="queue").
    def inject_context(self, session_id: str, text: str, *, description: Optional[str] = None) -> dict:
        return self.steer(session_id, text, delivery="queue", description=description)

    def interrupt(self, session_id: str) -> bool:
        """POST /api/session/{sid}/interrupt — True if an execution was actually stopped."""
        out = self._data(self._request("POST", f"/api/session/{session_id}/interrupt", {}))
        return bool(out.get("interrupted", False)) if out else False

    def wait(self, session_id: str, *, timeout: float = 120.0) -> Any:
        """POST /api/experimental/session/{sid}/wait — blocks server-side until idle."""
        return self._data(self._request("POST", f"/api/experimental/session/{session_id}/wait",
                                        {}, timeout=timeout))

    # ------------------------------------------------------------ permission

    def permission_requests(self, session_id: Optional[str] = None) -> list[dict]:
        """GET /api/permission/request (global) — or filtered to one session."""
        out = self._data(self._request("GET", "/api/permission/request")) or []
        if session_id is not None:
            out = [r for r in out if r.get("sessionID") == session_id]
        return out

    def session_permission_requests(self, session_id: str) -> list[dict]:
        """GET /api/session/{sid}/permission — THE live permission queue (2.1.4).

        In kernel 2.1.4 the global endpoint stays empty; pending permission
        requests surface here, per session. Action name is the runtime one
        ("shell"), and resources contain the actual command. save[] lists
        patterns the user may blanket-store ("always").
        """
        return self._data(self._request("GET", f"/api/session/{session_id}/permission")) or []

    def reply_permission(self, session_id: str, request_id: str,
                         decision: str, *, message: Optional[str] = None) -> None:
        """POST /api/session/{sid}/permission/{rid}/reply — decision: once|always|reject.

        NOTE: the adapter deliberately does not choose. JARVIS Gate B (Phase 2) calls
        this on the user's behalf; default policy is reject-if-nobody-decided.
        """
        if decision not in ("once", "always", "reject"):
            raise KernelError("decision must be once|always|reject", tag="InvalidRequestError")
        body: dict = {"decision": decision}
        if message is not None:
            body["message"] = message
        self._request("POST", f"/api/session/{session_id}/permission/{request_id}/reply", body)

    # ------------------------------------------------------------- structure

    def fork(self, session_id: str, *, before: Optional[str] = None) -> dict:
        """POST /api/session/{sid}/fork — branch; optionally up to (not incl.) message `before`."""
        body = {"before": before} if before is not None else {}
        return self._data(self._request("POST", f"/api/session/{session_id}/fork", body))

    def compact(self, session_id: str, *, delivery: str = "queue") -> dict:
        """POST /api/session/{sid}/compact"""
        return self._data(self._request("POST", f"/api/session/{session_id}/compact",
                                        {"delivery": delivery}))

    def messages(self, session_id: str) -> list[dict]:
        """GET /api/session/{sid}/message — full transcript."""
        return self._data(self._request("GET", f"/api/session/{session_id}/message")) or []

    # ------------------------------------------------------------- discovery

    def list_agents(self) -> list[dict]:
        """GET /api/agent"""
        return self._data(self._request("GET", "/api/agent")) or []

    def list_models(self) -> list[dict]:
        """GET /api/model — full model catalog (cost, capabilities, limits)."""
        return self._data(self._request("GET", "/api/model")) or []

    def default_model(self) -> dict:
        """GET /api/model/default — what a session uses when none is given."""
        return self._data(self._request("GET", "/api/model/default"))

    def switch_model(self, session_id: str, provider_id: str, model_id: str) -> dict:
        """POST /api/session/{sid}/model — redirects the rest of this session."""
        return self._data(self._request("POST", f"/api/session/{session_id}/model",
                                        {"model": {"providerID": provider_id, "id": model_id}}))

    def list_skills(self) -> list[dict]:
        """GET /api/skill"""
        return self._data(self._request("GET", "/api/skill")) or []

    # ---------------------------------------------------------------- events

    def events(self) -> Iterator[dict]:
        """GET /api/event -- SSE. Yields parsed event dicts until the stream breaks.

        Run this in a dedicated thread; it blocks. Each event carries
        {type, properties{...}} per the kernel's event vocabulary -- consumers
        (Phase 5 turn-runner) subscribe; the adapter only translates bytes.
        """
        resp = self._request("GET", "/api/event", timeout=None, stream=True)  # type: ignore[arg-type]
        try:
            yield from self.iter_sse(resp)
        finally:
            resp.close()

    def events_nonblocking(self) -> "EventPipe":
        """events() plus a background thread; .attach(callback) from any thread."""
        return EventPipe(self)
