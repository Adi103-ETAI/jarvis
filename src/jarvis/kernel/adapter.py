"""JARVIS kernel adapter — the stable ABI between the active layer and OpenCode.

Every method maps to exactly ONE operation in docs/opencode-api.lock.json
(OpenCode v2.1.4, experimental HTTP surface). No policy lives here: this is a
dumb, auditable pipe. Decisions belong to Gate A / Gate B (Phase 2).

Auth: Basic "opencode:<password>" — password read from
~/.config/opencode/service.json (the same file the CLI itself uses).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

KERNEL_VERSION = "2.1.4"          # pin; bumping re-locks the API (docs/opencode-api.lock.json)
DEFAULT_BASE = "http://127.0.0.1:49374"
SERVICE_JSON = Path.home() / ".config/opencode/service.json"


class KernelError(RuntimeError):
    """A structured error returned by, or about, the kernel."""

    def __init__(self, message: str, *, status: int = 0, tag: str = ""):
        super().__init__(message)
        self.status = status
        self.tag = tag


class Kernel:
    """One connection to one OpenCode service instance."""

    def __init__(self, base_url: Optional[str] = None, password: Optional[str] = None):
        self.base_url = (base_url or os.environ.get("OPENCODE_URL") or DEFAULT_BASE).rstrip("/")
        pw = password or os.environ.get("OPENCODE_PASSWORD")
        if pw is None:
            try:
                pw = json.loads(SERVICE_JSON.read_text())["password"]
            except Exception as e:  # noqa: BLE001 - surface the real cause
                raise KernelError(f"cannot obtain kernel password: {e}") from e
        token = base64.b64encode(f"opencode:{pw}".encode()).decode()
        self._auth = f"Basic {token}"

    # ------------------------------------------------------------------ core

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        timeout: float = 30.0,
        stream: bool = False,
    ):
        """One HTTP call to one locked operation. Returns parsed JSON, or None on 204."""
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Authorization": self._auth, "Accept": "text/event-stream" if stream else "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            payload = e.read()
            tag, message = "", e.reason
            try:
                parsed = json.loads(payload)
                tag = parsed.get("_tag", "")
                message = parsed.get("message", message)
            except Exception:  # noqa: BLE001
                parsed = {}
            raise KernelError(message or f"HTTP {e.code}", status=e.code, tag=tag) from None
        except urllib.error.URLError as e:
            raise KernelError(f"kernel unreachable at {self.base_url}: {e.reason}", status=0) from None

        if stream:
            return response  # caller owns the file-like SSE stream
        if response.status == 204 or not response.readable():
            return None
        raw = response.read()
        if not raw.strip():
            return None
        return json.loads(raw)

    @staticmethod
    def _data(payload: Any) -> Any:
        """Kernel envelopes success as {data: ...}. Unwrap it once, here, everywhere."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

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
        """GET /api/event — SSE. Yields parsed event dicts until the stream breaks.

        Run this in a dedicated thread; it blocks. Each event carries
        {type, properties{...}} per the kernel's event vocabulary — consumers
        (Phase 5 turn-runner) subscribe; the adapter only translates bytes.
        """
        resp = self._request("GET", "/api/event", timeout=None, stream=True)  # type: ignore[arg-type]
        data_lines: list[str] = []
        try:
            for line in resp:
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    if data_lines:
                        blob = "".join(data_lines)
                        data_lines = []
                        try:
                            yield json.loads(blob)
                        except json.JSONDecodeError:
                            continue
                    continue
                if text.startswith("data:"):
                    data_lines.append(text[len("data:"):].lstrip())
        finally:
            resp.close()

    def events_nonblocking(self) -> "EventPipe":
        """events() plus a background thread; .attach(callback) from any thread."""
        return EventPipe(self)


class EventPipe:
    """Owns the SSE thread so callers never think about blocking."""

    def __init__(self, kernel: Kernel):
        self._kernel = kernel
        self._subs: list[Any] = []  # callables(event: dict)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def attach(self, callback) -> None:
        with self._lock:
            self._subs.append(callback)
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def run():
            for event in self._kernel.events():
                with self._lock:
                    for cb in list(self._subs):
                        try:
                            cb(event)
                        except Exception:  # noqa: BLE001 - a bad subscriber must not kill the pipe
                            pass

        self._thread = threading.Thread(target=run, name="jarvis-eventpipe", daemon=True)
        self._thread.start()
