"""Unit tests for the Phase 3 tail: live announcer, cancel, lanes, latency.

All tests are hermetic -- no kernel, no network. The console is exercised
with monkeypatched kernel methods and tmp_path-backed stores/logs.
"""

import time

import pytest

from jarvis.announcer import Announcer
from jarvis.console import JarvisConsole, first_token_seconds
from jarvis.journal import Journal


# ------------------------------------------------------------------ fakes

class FakeWorker:
    """PermissionWorker stand-in: only pending_list() is needed."""

    def __init__(self, items):
        self.items = items

    def pending_list(self):
        return self.items


class FakeStore:
    """PendingStore stand-in: only list() is needed."""

    def __init__(self, items):
        self.items = items

    def list(self):
        return self.items


# -------------------------------------------------------------- announcer

def test_announcer_announces_new_codes_exactly_once():
    a = Announcer(FakeWorker([{"code": "aaa111", "description": "shell: touch /tmp/x"}]),
                  FakeStore([{"code": "bbb222", "description": "mcp: write file"}]))
    first = a.scan()
    assert len(first) == 2
    assert any("aaa111" in m and "touch" in m for m in first)
    assert any("bbb222" in m for m in first)
    assert a.scan() == []                     # no re-announce on steady state


def test_announcer_reports_resolution():
    a = Announcer(FakeWorker([{"code": "aaa111", "description": "x"}]), FakeStore([]))
    a.scan()
    a.worker.items = []                       # code resolved elsewhere
    resolved = a.scan()
    assert len(resolved) == 1 and "aaa111" in resolved[0] and "resolved" in resolved[0]
    assert a.scan() == []                     # resolution announced once, too


def test_announcer_broken_source_never_fatal():
    class Boom:
        def pending_list(self):
            raise RuntimeError("doorkeeper hiccup")

    a = Announcer(Boom(), FakeStore([{"code": "ccc333", "description": "ok"}]))
    lines = a.scan()
    assert len(lines) == 1 and "ccc333" in lines[0]


def test_announcer_narrates_turn_idle_once():
    lines = []
    a = Announcer(on_line=lines.append)
    a.watch_sid = "ses_1"
    ev = {"type": "session.idle", "data": {"sessionID": "ses_1"}}
    a.note_event(ev)
    a.note_event(ev)                          # duplicate idle event
    a.note_event({"type": "session.idle", "data": {"sessionID": "ses_other"}})
    assert len(lines) == 1 and "replay" in lines[0]


def test_announcer_silent_without_watch():
    lines = []
    a = Announcer(on_line=lines.append)       # watch_sid is None
    a.note_event({"type": "session.idle", "data": {"sessionID": "ses_1"}})
    assert lines == []


# ---------------------------------------------------------- latency math

def test_first_token_from_epoch_millis():
    t0 = 1_770_000_000.0                            # realistic epoch seconds
    events = [{"type": "session.text.delta", "data": {"sessionID": "s"},
               "created": 1_770_000_000_250}]       # ms -> +0.25 s
    assert first_token_seconds(events, "s", t0) == pytest.approx(0.25)


def test_first_token_from_epoch_seconds():
    t0 = 1_770_000_000.0
    events = [{"type": "session.text.delta", "data": {"sessionID": "s"},
               "created": 1_770_000_000.5}]
    assert first_token_seconds(events, "s", t0) == pytest.approx(0.5)


def test_first_token_none_cases():
    t0 = 1_770_000_000.0
    other = [{"type": "session.text.delta", "data": {"sessionID": "other"},
              "created": 1_770_000_000.5}]
    undated = [{"type": "session.text.delta", "data": {"sessionID": "s"}}]
    old = [{"type": "session.text.delta", "data": {"sessionID": "s"},
            "created": 5.0}]                  # before the turn: clock skew
    later = [{"type": "session.text.delta", "data": {"sessionID": "s"},
              "created": 1_770_000_001.0}]
    assert first_token_seconds(other, "s", t0) is None
    assert first_token_seconds(undated, "s", t0) is None
    assert first_token_seconds(old, "s", t0) is None
    assert first_token_seconds(later, "s", t0, start_index=1) is None  # sliced out
    assert first_token_seconds(later, "s", t0) == pytest.approx(1.0)


# ---------------------------------------------------------------- console

@pytest.fixture
def jc(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PASSWORD", "unit-test")  # hermetic kernel auth
    console = JarvisConsole(
        journal_path=tmp_path / "journal.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        pending_root=tmp_path / "pending",
        routines_path=str(tmp_path / "routines.toml"),  # missing -> zero routines:
        # tests must never fire a real routine against a live kernel
    )
    yield console
    console.close()


def test_console_boots_hermetically(jc):
    assert jc.session is None
    assert jc.announcer._thread.is_alive()
    assert jc.worker._thread.is_alive()


def test_cancel_without_session(jc):
    assert jc.cancel() == "no session to cancel."


def test_cancel_success_is_journalled(jc):
    jc.registry.add("ses_x")
    jc.session = {"id": "ses_x"}
    jc.kernel.interrupt = lambda sid: True    # stand-in for the live kernel
    assert jc.cancel() == "execution stopped."
    entry = jc.journal.tail(5, kind="turn-cancelled")[-1]
    assert entry["session_id"] == "ses_x" and entry["stopped"] is True


def test_cancel_kernel_refusal_surfaces(jc):
    jc.registry.add("ses_x")
    jc.session = {"id": "ses_x"}

    def boom(sid):
        from jarvis.kernel.adapter import KernelError
        raise KernelError("kernel unreachable", status=0)

    jc.kernel.interrupt = boom
    assert jc.cancel().startswith("cancel refused:")


def test_wait_turn_pauses_for_human_decision(jc):
    jc.registry.add("ses_x")
    jc.session = {"id": "ses_x"}
    jc.worker.pending["abc123"] = {"sessionID": "ses_x", "requestID": "per_1",
                                   "description": "shell: touch /tmp/x"}
    out = jc.wait_turn(timeout=5.0)
    assert out.startswith("(paused") and "abc123" in out


def test_bg_lane_returns_immediately(jc, monkeypatch):
    jc.kernel.create_session = lambda **kw: {"id": "ses_test"}
    jc.kernel.prompt = lambda sid, text, **kw: {"id": "msg_1"}
    out = jc.handle("bg count the files in tmp")
    assert "background" in out
    assert jc.journal.tail(5, kind="turn-started")[-1]["lane"] == "bg"


def test_fg_lane_returns_assistant_text_and_metrics(jc):
    jc.kernel.create_session = lambda **kw: {"id": "ses_test"}
    jc.kernel.prompt = lambda sid, text, **kw: {"id": "msg_1"}

    def fake_wait(sid, timeout=5.0):
        jc.events.append({"type": "session.text.delta",
                          "data": {"sessionID": sid, "text": "hello"},
                          "created": time.time()})
        return {}

    jc.kernel.wait = fake_wait
    out = jc.ask("say hello", lane="fg")
    assert out == "hello"
    metrics = jc.journal.tail(10, kind="turn-metrics")[-1]
    assert metrics["lane"] == "fg"
    assert metrics["first_token_s"] is not None and metrics["first_token_s"] >= 0
    assert metrics["total_s"] >= 0


def test_fg_lane_pauses_then_replay_reads_turn(jc):
    jc.kernel.create_session = lambda **kw: {"id": "ses_test"}
    jc.kernel.prompt = lambda sid, text, **kw: {"id": "msg_1"}
    jc.worker.pending["def456"] = {"sessionID": "ses_test", "requestID": "per_2",
                                   "description": "edit: /etc/hosts"}
    out = jc.ask("edit the hosts file", lane="fg")
    assert out.startswith("(paused") and "def456" in out
    # human approves, turn completes, replay returns the turn's text only
    jc.events.append({"type": "session.text.delta",
                      "data": {"sessionID": "ses_test", "text": "done"},
                      "created": time.time()})
    assert jc.handle("replay") == "done"


def test_routine_commands_route_and_report(jc):
    assert "no routines defined" in jc.handle("routines")
    assert "no routine named" in jc.handle("routine ghost off")
    assert jc.handle("routine") == "usage: routine NAME off|on"


def test_shipped_routines_config_parses():
    from pathlib import Path
    from jarvis.scheduler import load_routines
    cfg = Path(__file__).resolve().parents[2] / "config" / "routines.toml"
    if not cfg.exists():
        pytest.skip("shipped config absent")
    rs = load_routines(str(cfg))
    by = {r.name: r for r in rs}
    assert "morning-briefing" in by
    mb = by["morning-briefing"]
    assert mb.enabled and mb.schedule == "daily 08:00"
    assert "journal" in mb.task.lower()
