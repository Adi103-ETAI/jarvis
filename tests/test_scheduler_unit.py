"""Unit tests for Phase 4: the T1 scheduler and its cycle guards.

Hermetic — no kernel, no network, injectable clock. The scheduler is the
first component that acts without a person present, so these tests focus
on the guards: one fire per day, no overlap, budgets, failure auto-disable,
runtime toggles that may only tighten, and DND that never touches
confirmations.
"""

import datetime as _dt
import time
from types import SimpleNamespace

import pytest

from jarvis.journal import Journal
from jarvis.kernel.adapter import KernelError
from jarvis.registry import SessionRegistry
from jarvis.scheduler import (Routine, Scheduler, in_dnd, load_routines,
                              parse_daily_hhmm)


# ------------------------------------------------------------- fakes

class TrustRecorder:
    def __init__(self):
        self.trust = {}

    def set_trust(self, sid, trust):
        self.trust[sid] = trust


class FakeConsole:
    """Everything Scheduler touches on the console, nothing more."""

    def __init__(self, tmp_path):
        self.registry = SessionRegistry()
        self.worker = TrustRecorder()
        self.nudges = []
        self.announcer = SimpleNamespace(nudge=self.nudges.append)
        self.journal = Journal(tmp_path / "journal.jsonl")
        self.workspace = str(tmp_path)
        self.model = {"providerID": "opencode", "id": "test-model"}
        self.kernel = SimpleNamespace()
        self.asked = []

    def ask(self, text, lane="fg", source="console", session_id=None):
        self.asked.append({"text": text, "lane": lane, "source": source,
                           "session_id": session_id})
        return "launched"


@pytest.fixture
def env(tmp_path):
    console = FakeConsole(tmp_path)
    sched = Scheduler(console, routines_path=str(tmp_path / "none.toml"),
                      journal=console.journal, poll_seconds=999.0,
                      dnd=None, clock=lambda: _dt.datetime(2026, 10, 28, 8, 5))
    console.kernel.create_session = lambda **kw: {"id": "ses_r"}
    return console, sched


# --------------------------------------------------------- schedule math

def test_parse_daily_hhmm():
    assert parse_daily_hhmm("daily 08:00") == _dt.time(8, 0)
    assert parse_daily_hhmm("  Daily 23:59 ") == _dt.time(23, 59)
    assert parse_daily_hhmm("hourly 08:00") is None
    assert parse_daily_hhmm("daily 8:00pm") is None
    assert parse_daily_hhmm("daily 25:00") is None
    assert parse_daily_hhmm("") is None


def test_in_dnd_windows():
    t = _dt.time
    assert in_dnd(t(23, 30), "23:00-07:00")
    assert in_dnd(t(3, 0), "23:00-07:00")
    assert not in_dnd(t(12, 0), "23:00-07:00")
    assert in_dnd(t(14, 0), "13:00-15:00")
    assert not in_dnd(t(15, 0), "13:00-15:00")
    assert not in_dnd(t(12, 0), None)
    assert not in_dnd(t(12, 0), "garbage")
    assert not in_dnd(t(12, 0), "12:00-12:00")   # zero-length window


# ----------------------------------------------------------- config load

def test_load_routines_defaults_and_missing_file(tmp_path):
    assert load_routines(str(tmp_path / "missing.toml")) == []
    cfg = tmp_path / "r.toml"
    cfg.write_text("""
[defaults]
max_run_minutes = 7

[[routine]]
name = "a"
schedule = "daily 08:00"
task = "do a"

[[routine]]
name = "b"
schedule = "daily 09:00"
task = "do b"
enabled = false
max_run_minutes = 2
""")
    rs = load_routines(str(cfg))
    by = {r.name: r for r in rs}
    assert by["a"].max_run_minutes == 7 and by["a"].enabled
    assert by["b"].max_run_minutes == 2 and not by["b"].enabled


def test_broken_config_arms_nothing(tmp_path):
    cfg = tmp_path / "broken.toml"
    cfg.write_text("not [ valid toml {{{")
    assert load_routines(str(cfg)) == []


# ------------------------------------------------------------- due logic

def test_routine_fires_once_after_schedule_time(env):
    console, sched = env
    sched.routines = [Routine("brief", "daily 08:00", "task")]
    r = sched.routines[0]
    assert sched._due(r) is True                 # 08:05 >= 08:00, not fired
    sched._launch(r, reason="scheduled")
    assert sched._due(r) is False                # same day: never twice
    # next day: due again (turn finished, cooldown elapsed, journal empty)
    sched._finish(r, ok=True, note="done")
    r.last_started = time.time() - 3601          # > cooldown_minutes ago
    sched.clock = lambda: _dt.datetime(2026, 10, 29, 8, 5)
    assert sched._due(r) is True


def test_routine_silent_before_schedule_time(env):
    console, sched = env
    sched.clock = lambda: _dt.datetime(2026, 10, 28, 7, 59)
    r = Routine("brief", "daily 08:00", "task")
    sched.routines = [r]
    assert sched._due(r) is False


def test_journal_dedupe_survives_restart(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task")
    sched.routines = [r]
    today = sched.clock().strftime("%Y-%m-%d")
    console.journal.add("routine-started", routine="brief", date=today)
    assert sched._due(r) is False                # fired today per journal
    # a fresh Scheduler (console restart) reads the same journal
    sched2 = Scheduler(console, routines_path="none.toml", journal=console.journal,
                       clock=sched.clock)
    sched2.routines = [r]
    assert sched2._due(r) is False


def test_due_respects_disabled_cooldown_and_failures(env):
    console, sched = env
    r = Routine("x", "daily 08:00", "task")
    sched.routines = [r]
    r.enabled = False
    assert sched._due(r) is False
    r.enabled = True
    r.last_started = time.time() - 10            # cooldown 60min default
    assert sched._due(r) is False
    r.last_started = None
    r.consecutive_failures = r.max_consecutive_failures
    assert sched._due(r) is False


# ---------------------------------------------------------------- launch

def test_launch_uses_dedicated_session_and_scheduled_trust(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "read the journal")
    sched.routines = [r]
    sched._launch(r, reason="scheduled")
    assert sched.session_id == "ses_r"
    assert console.registry.owns("ses_r")
    assert console.worker.trust["ses_r"] == "scheduled"
    call = console.asked[-1]
    assert call["lane"] == "bg" and call["source"] == "scheduled"
    assert call["session_id"] == "ses_r"
    assert "routine-started" in [e["kind"] for e in console.journal.tail(10)]


def test_manual_run_counts_as_daily_fire(env):
    console, sched = env
    sched.routines = [Routine("brief", "daily 08:00", "task")]
    out = sched.run_now("brief")
    assert "launched" in out
    assert len(console.asked) == 1
    assert sched.run_now("brief").startswith("brief: launched") or "satisfied" in out
    # busy or fired: the clock must not double-fire today
    r = sched.routines[0]
    assert sched._due(r) is False


def test_run_now_refuses_inactive(env):
    console, sched = env
    sched.routines = [Routine("off-one", "daily 08:00", "task", enabled=False)]
    assert "not active" in sched.run_now("off-one")
    assert console.asked == []
    assert "no routine" in sched.run_now("ghost")


# ------------------------------------------------------- failure / guards

def test_consecutive_failures_auto_disable(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task", max_consecutive_failures=3)
    sched.routines = [r]
    for i in range(3):
        sched._finish(r, ok=False, note="boom")
        if i < 2:
            assert r.runtime_off is False
    assert r.runtime_off is True
    assert any("DISABLED" in n for n in console.nudges)


def test_success_resets_failure_counter(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task", max_consecutive_failures=3)
    sched.routines = [r]
    sched._finish(r, ok=False, note="x")
    sched._finish(r, ok=True, note="ok")
    assert r.consecutive_failures == 0


def test_watchdog_interrupts_runaway_turn(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task", max_run_minutes=10)
    sched.routines = [r]
    sched.session_id = "ses_r"
    console.registry.add("ses_r")
    interrupted = []
    console.kernel.interrupt = lambda sid: interrupted.append(sid) or True
    r.busy_since = time.time() - 11 * 60         # over budget
    sched._watchdog()
    assert interrupted == ["ses_r"]
    assert r.busy_since is None
    assert r.consecutive_failures == 1
    assert "routine-timeout" in [e["kind"] for e in console.journal.tail(10)]


def test_watchdog_ignores_young_turns(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task", max_run_minutes=10)
    sched.routines = [r]
    r.busy_since = time.time() - 60              # one minute in
    interrupted = []
    console.kernel.interrupt = lambda sid: interrupted.append(sid) or True
    sched._watchdog()
    assert interrupted == [] and r.busy_since is not None


# ------------------------------------------------------------- events

def test_note_event_failure_then_idle_counts_failure(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task", max_consecutive_failures=3)
    sched.routines = [r]
    sched.session_id = "ses_r"
    r.busy_since = time.time()
    sched.note_event({"type": "session.execution.failed", "data": {"sessionID": "ses_r"}})
    sched.note_event({"type": "session.idle", "data": {"sessionID": "ses_r"}})
    assert r.busy_since is None and r.consecutive_failures == 1


def test_note_event_plain_idle_is_success(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task")
    sched.routines = [r]
    sched.session_id = "ses_r"
    r.busy_since = time.time()
    sched.note_event({"type": "session.idle", "data": {"sessionID": "ses_r"}})
    assert r.consecutive_failures == 0 and r.busy_since is None
    assert any("finished" in n for n in console.nudges)


def test_note_event_ignores_other_sessions(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task")
    sched.routines = [r]
    sched.session_id = "ses_r"
    r.busy_since = time.time()
    sched.note_event({"type": "session.idle", "data": {"sessionID": "ses_other"}})
    assert r.busy_since is not None              # untouched


# ------------------------------------------------------------- DND notify

def test_dnd_suppresses_routine_notifications_only(env):
    console, sched = env
    sched.dnd = "23:00-07:00"
    sched.clock = lambda: _dt.datetime(2026, 10, 28, 23, 30)   # inside DND
    r = Routine("brief", "daily 23:30", "task")
    sched.routines = [r]
    sched.session_id = "ses_r"
    sched._notify(r, "[jarvis] routine launched")
    assert console.nudges == []                  # silenced
    sched.clock = lambda: _dt.datetime(2026, 10, 28, 12, 0)    # daytime
    sched._notify(r, "[jarvis] routine finished")
    assert console.nudges == ["[jarvis] routine finished"]
    # confirmations are NOT routed through _notify: the announcer/worker
    # path is untouched by DND by construction (see announcer, worker).


def test_notify_false_routine_stays_silent(env):
    console, sched = env
    r = Routine("brief", "daily 08:00", "task", notify=False)
    sched.routines = [r]
    sched.session_id = "ses_r"
    sched._notify(r, "anything")
    assert console.nudges == []


# ------------------------------------------------------- runtime toggles

def test_runtime_toggle_may_only_tighten(env):
    console, sched = env
    file_on = Routine("a", "daily 08:00", "t", enabled=True)
    file_off = Routine("b", "daily 09:00", "t", enabled=False)
    sched.routines = [file_on, file_off]
    assert "off" in sched.runtime_toggle("a", on=False)
    assert file_on.runtime_off is True
    assert "refused" in sched.runtime_toggle("b", on=True)   # file-disabled
    assert "off" in sched.runtime_toggle("a", on=False)
    assert "on" in sched.runtime_toggle("a", on=True)        # reset ok
    assert file_on.runtime_off is False
    assert "no routine" in sched.runtime_toggle("ghost", on=False)


def test_auto_disabled_routine_cannot_reenable_at_runtime(env):
    console, sched = env
    r = Routine("a", "daily 08:00", "t", max_consecutive_failures=2)
    sched.routines = [r]
    r.consecutive_failures = 2
    assert "refused" in sched.runtime_toggle("a", on=True)


# --------------------------------------------------------------- listing

def test_list_lines_states(env):
    console, sched = env
    sched.routines = [Routine("a", "daily 08:00", "t"),
                      Routine("b", "daily 09:00", "t", enabled=False)]
    lines = sched.list_lines()
    assert any("a" in ln and "armed" in ln for ln in lines)
    assert any("b" in ln and "disabled-by-file" in ln for ln in lines)


def test_kernel_down_scheduler_ticks_survive(env):
    console, sched = env

    def kernel_down(**kw):
        raise KernelError("kernel unreachable", status=0)

    console.kernel.create_session = kernel_down
    sched.routines = [Routine("brief", "daily 08:00", "task")]
    sched._tick()                                 # must not raise
    assert console.asked == [] and sched.session_id is None
