"""Live Gate B end-to-end tests against the real kernel.

Requires kernel config permission: {bash: ask, edit: ask, webfetch: ask}
(the agreed human-in-the-loop posture).

Scenarios:
  1. benign command        -> worker auto-approves once, tool runs
  2. mutating command      -> parked as pending code -> human approves -> runs
  3. ambient-origin prompt -> worker rejects the mutating call outright
  4. documented known-bypass: MCP shell tools skip permission requests
"""

import os
import secrets
import time

import pytest

from jarvis.audit import GateLog
from jarvis.confirm import ConfirmationBroker
from jarvis.gates.tool import ToolGate
from jarvis.gates.worker import PermissionWorker
from jarvis.kernel.adapter import KernelError
from jarvis.policy import Policy
from jarvis.registry import OwnedKernel, SessionRegistry

WORKSPACE = "/home/sonic/work-jarvis"
MODEL = {"providerID": "opencode", "id": "glm-5.3-flash-free"}


class Stack:
    """One gate stack per test: registry, worker thread, owned kernel."""

    def __init__(self, tmp_path):
        self.registry = SessionRegistry()
        self.log = GateLog(tmp_path / "gate-log.jsonl")
        self.kernel = OwnedKernel(self.registry)
        self.policy = Policy()
        self.broker = ConfirmationBroker()
        self.gate = ToolGate(self.policy, self.log)
        self.worker = PermissionWorker(self.kernel, self.registry, self.gate,
                                       self.broker, self.log, poll_seconds=0.3)
        self.events: list[dict] = []
        self.kernel.events_nonblocking().attach(self.events.append)

    def session(self, title: str, trust: str = "console", permissions=None) -> dict:
        s = self.kernel.create_session(directory=WORKSPACE, model=MODEL,
                                       title=title, permissions=permissions)
        self.registry.add(s["id"], title=title)
        self.worker.set_trust(s["id"], trust)
        return s

    def tool_successes(self, session_id: str) -> list[dict]:
        return [e for e in self.events
                if e.get("type") == "session.tool.success"
                and e.get("data", {}).get("sessionID") == session_id]

    def cleanup(self):
        for sid in self.registry.active_ids():
            try:
                self.kernel.delete_session(sid)
            except KernelError:
                pass


@pytest.fixture()
def stack(tmp_path):
    st = Stack(tmp_path)
    st.worker.start()
    yield st
    st.worker.stop()
    st.cleanup()


def test_01_benign_command_auto_runs(stack):
    s = stack.session("gateb-benign")
    stack.kernel.prompt(s["id"], "Run the shell command: echo gateb-auto (use the shell tool)")
    deadline = time.time() + 60
    while time.time() < deadline:
        hits = stack.tool_successes(s["id"])
        if hits:
            text = "".join(c.get("text", "") for c in hits[-1].get("data", {}).get("content", []))
            assert "gateb-auto" in text, f"tool ran, but wrong output: {text!r}"
            break
        time.sleep(0.5)
    else:
        pytest.fail("benign command was never auto-approved within 60s")


def test_02_mutating_command_needs_human_code(stack):
    target = f"/tmp/gateb-human-{secrets.token_hex(3)}"
    s = stack.session("gateb-confirm")
    stack.kernel.prompt(s["id"], f"Run the shell command: touch {target} (use the shell tool)")

    code = None
    deadline = time.time() + 30
    while time.time() < deadline:
        pend = stack.worker.pending_list()
        if pend:
            code = pend[0]["code"]
            break
        time.sleep(0.5)
    assert code, "no confirmation code was opened for a mutating command"
    assert not stack.tool_successes(s["id"]), "tool ran BEFORE human approval"

    assert stack.worker.resolve(code, accept=True)
    assert stack.worker.resolve(code, accept=True) is False  # one-time only

    deadline = time.time() + 60
    while time.time() < deadline:
        if stack.tool_successes(s["id"]):
            break
        time.sleep(0.5)
    else:
        pytest.fail("tool never ran after human approval")
    assert os.path.exists(target), "human-approved command did not execute"
    os.remove(target)


def test_03_ambient_origin_cannot_mutate(stack):
    target = f"/tmp/gateb-ambient-{secrets.token_hex(3)}"
    s = stack.session("gateb-ambient", trust="ambient")
    stack.kernel.prompt(s["id"],
                        f"Run the shell command: touch {target} (use the shell tool)")
    stack.kernel.wait(s["id"], timeout=90)
    assert not stack.tool_successes(s["id"]), "ambient trust executed a mutating tool!"
    assert not os.path.exists(target)
    rejects = [r for r in stack.log.tail(100)
               if r.get("verdict") == "rejected" and r.get("gate") == "permission"]
    assert rejects, "the rejection was not audited"


def test_04_mcp_tools_bypass_kernel_permissions():
    """KNOWN BYPASS #1 (documented on purpose, asserted on purpose).

    Kernel permission machinery gates the NATIVE shell tool (config
    bash/shell:ask raises a request; session-level rules keyed by 'shell'
    enforce it). But MCP-provided tools raise NO permission request at all
    in 2.1.4 — a session with even a full shell-DENY executes through an
    MCP shell tool such as serena's execute_shell_command.

    Consequence: JARVIS-owned sessions must not expose execution-capable
    MCP servers (jarvisd rule lands with the Phase-3 turn-runner).
    """
    reg = SessionRegistry()
    k = OwnedKernel(reg)
    s = k.create_session(
        directory=WORKSPACE, model=MODEL, title="gateb-mcp-bypass",
        permissions=[{"action": "shell", "resource": "*", "effect": "deny"}])
    reg.add(s["id"])
    evs: list[dict] = []
    k.events_nonblocking().attach(evs.append)
    # deliberately NOT 'use the shell tool' — leave the model free to pick an MCP tool
    k.prompt(s["id"], "Run the command: echo bypass-proof via any available tool")
    k.wait(s["id"], timeout=90)
    time.sleep(0.5)
    succ = [e for e in evs if e.get("type") == "session.tool.success"
            and e.get("data", {}).get("sessionID") == s["id"]]

    # expectation under CURRENT kernel: execution succeeds through an MCP tool
    assert succ, "test derives from the live-observed bypass; if this fails, the kernel changed"
    text = "".join(c.get("text", "")
                   for c in succ[-1].get("data", {}).get("content", []))
    assert "bypass-proof" in text, "expected MCP execution proof"
    queue = k._data(k._request("GET", f"/api/session/{s['id']}/permission"))
    assert queue == [], "permission queue must stay empty for MCP calls (that IS the bypass)"
    k.delete_session(s["id"])
