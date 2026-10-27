"""Live E2E for the jarvisd console wiring.

One path, straight through the real user journey:
  typed request -> Gate A -> kernel turn -> Gate B auto-approves benign
  -> tool executes -> assistant text lands -> journal records it all.
"""

import time

import pytest

from jarvis.console import JarvisConsole

WORKSPACE = "/home/sonic/work-jarvis"


@pytest.fixture()
def jc(tmp_path):
    console = JarvisConsole(workspace=WORKSPACE,
                            journal_path=tmp_path / "journal.jsonl",
                            audit_path=tmp_path / "audit.jsonl",
                            pending_root=tmp_path / "pending")
    yield console
    console.close()


def test_console_turn_end_to_end(jc):
    reply = jc.handle("run the shell command: echo jarvisd-live (use the shell tool)")
    assert reply == "turn launched.", reply

    sid = jc.session["id"]
    deadline = time.time() + 60
    text = ""
    while time.time() < deadline:
        hits = [e for e in jc.events
                if e.get("type") == "session.tool.success"
                and e.get("data", {}).get("sessionID") == sid]
        if hits:
            text = "".join(c.get("text", "")
                           for c in hits[-1].get("data", {}).get("content", []))
            if "jarvisd-live" in text:
                break
        time.sleep(0.5)
    assert "jarvisd-live" in text, "benign command did not auto-run via jarvisd"

    assert jc.handle("pending") == "no confirmations waiting."
    status = jc.handle("status")
    assert "kernel" in status and sid in status

    kinds = {e["kind"] for e in jc.journal.tail(50)}
    assert {"gate-a", "turn-started", "session-opened"} <= kinds, kinds


def test_console_quit_and_help(jc):
    assert jc.handle("help").startswith("commands:")
    assert jc.handle("") == ""
    assert jc.handle("quit") == "__quit__"
