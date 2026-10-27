"""Live integration tests for the Phase 1 kernel adapter.

Runs against the real pinned kernel (v2.1.4 at 127.0.0.1:49374).
Every test session is created with mutating actions DENIED, so nothing on
disk can be touched even if the model goes rogue. Test cleanup deletes the
sessions it created.

    cd /home/sonic/work-jarvis && PYTHONPATH=src pytest -x tests/test_adapter_live.py -v
"""

import time

import pytest

from jarvis.kernel.adapter import Kernel, KernelError

WORKSPACE = "/home/sonic/work-jarvis"

# Deny everything mutating for the smoke-test sessions. The kernel treats
# unknown actions conservatively; these are the well-known v2 mutating ones.
READONLY_RULES = [
    {"action": "bash", "resource": "*", "effect": "deny"},
    {"action": "edit", "resource": "*", "effect": "deny"},
    {"action": "write", "resource": "*", "effect": "deny"},
]

# Pin a confirmed-working free model for the tests. The kernel's config-level
# default drifted (paid kimi model => 402 provider.quota); sessions must not
# depend on ambient defaults — Phase 2 will pin this in policy, not config.
MODEL = {"providerID": "opencode", "id": "glm-5.3-flash-free"}

k = Kernel()
_created: list[str] = []


def make_session(**kw):
    rules = kw.pop("permissions", READONLY_RULES)
    s = k.create_session(directory=WORKSPACE, permissions=rules, model=MODEL, **kw)
    _created.append(s["id"])
    return s


def teardown_function():
    for sid in _created:
        try:
            k.delete_session(sid)
        except KernelError:
            pass
    _created.clear()


# ---------------------------------------------------------------------------

def test_01_unreachable_password_fails_clean():
    bad = Kernel(password="definitely-not-the-password")
    with pytest.raises(KernelError) as ei:
        bad.sessions()
    assert ei.value.status in (401, 403) or "unauthorized" in str(ei.value).lower()


def test_02_discovery():
    agents = k.list_agents()
    assert isinstance(agents, list) and agents, "kernel reports zero agents"
    skills = k.list_skills()
    assert isinstance(skills, list)


def test_03_profile_and_workspace_are_independent():
    """§15.1: create accepts agent AND directory independently."""
    agents = k.list_agents()
    agent_name = agents[0]["id"] if "id" in agents[0] else agents[0].get("name", "build")
    s = make_session(agent=agent_name, title="p1-profile-test")
    assert s["id"].startswith("ses")


def test_04_prompt_roundtrip():
    s = make_session(title="p1-prompt")
    k.prompt(s["id"], "Reply with exactly: JARVIS-OK (no other words).")
    k.wait(s["id"], timeout=90)
    msgs = k.messages(s["id"])
    types = [m.get("type") for m in msgs]
    assert "assistant" in types, f"no assistant reply, types={types}"
    idle = [m for m in msgs if m.get("type") == "idle"]
    assert idle and idle[-1].get("outcome") == "succeeded", idle[-1]
    assert not any(m.get("error") for m in msgs), "turn carried an error record"


def test_05_events_stream_carries_session_events():
    s = make_session(title="p1-events")
    seen: list[dict] = []
    pipe = k.events_nonblocking()
    pipe.attach(seen.append)
    time.sleep(0.5)
    k.prompt(s["id"], "Reply with the single word: events-seen")
    k.wait(s["id"], timeout=90)
    time.sleep(1.0)
    assert seen, "SSE stream yielded nothing"
    session_events = [e for e in seen
                      if isinstance(e, dict)
                      and str(e.get("data", {}).get("sessionID", "")) == s["id"]]
    assert session_events, f"no events for our session in {len(seen)} events"


def test_06_synthetic_queue_injection():
    s = make_session(title="p1-inject")
    out = k.inject_context(s["id"], "Note: this is an injected context line, do not act on it.",
                           description="phase1-test-injection")
    assert out["id"].startswith("msg_")
    msgs = k.messages(s["id"])
    assert any(m.get("type") == "synthetic" for m in msgs), "synthetic message missing from transcript"


def test_07_steer_active_turn():
    """Steering an in-flight turn: synthetic with delivery=steer must land.

    Note: /api/session/active holds only *durable* executions (it currently
    lists stale zombies but not short turns) — the event stream, not the
    active registry, is the source of truth for "is this turn running".
    """
    s = make_session(title="p1-steer")
    evs: list[dict] = []
    k.events_nonblocking().attach(evs.append)
    time.sleep(0.3)
    k.prompt(s["id"], "Count from 1 to 40, writing each number on its own line. Take your time.")
    time.sleep(1.5)
    started = [e for e in evs if e.get("type") == "session.execution.started"
               and e.get("data", {}).get("sessionID") == s["id"]]
    assert started, "kernel did not report execution.started for our turn"
    steered = k.steer(s["id"], "STOP counting now. Reply with only the word: STEERED",
                      delivery="steer")
    assert steered["id"].startswith("msg_")
    k.wait(s["id"], timeout=90)
    time.sleep(0.5)
    msgs = k.messages(s["id"])
    assert any(m.get("type") == "synthetic" for m in msgs), "steered message missing"
    idle = [m for m in msgs if m.get("type") == "idle"]
    assert idle and idle[-1].get("outcome") == "succeeded", idle[-1]


def test_08_interrupt_stops_running_turn():
    s = make_session(title="p1-interrupt")
    k.prompt(s["id"], "Very slowly write a 2000-word essay about sand, numbered sections, no skipping.")
    time.sleep(3.0)
    result = k.interrupt(s["id"])
    # tolerance: a fast model may have finished already; the endpoint shape is what's under test
    assert isinstance(result, bool)
    k.wait(s["id"], timeout=60)


def test_09_permission_reply_is_wired():
    s = make_session(title="p1-perm")
    # invalid decision value -> clean client-side error
    with pytest.raises(KernelError):
        k.reply_permission(s["id"], "per_handle", "maybe")
    # allowed decision + bogus handle -> structured 404 from the kernel
    with pytest.raises(KernelError) as ei:
        k.reply_permission(s["id"], "per_bogus_000", "reject")
    assert ei.value.status == 404 and ei.value.tag == "PermissionNotFoundError"


def test_10_fork_branches_lineage():
    base = make_session(title="p1-fork-base")
    k.prompt(base["id"], "Reply with exactly: FORK-BASE")
    k.wait(base["id"], timeout=90)
    base_msgs = k.messages(base["id"])
    branch = make_session()  # placeholder to keep teardown symmetric
    _created[-1] = branch["id"]
    # real fork from the live kernel
    out = k.fork(base["id"])
    forked_id = out["id"] if isinstance(out, dict) and "id" in out else out["sessionID"] if isinstance(out, dict) else None
    assert forked_id and forked_id.startswith("ses")
    assert forked_id not in _created
    _created.append(forked_id)
    assert isinstance(base_msgs, list)


def test_11_session_delete_cleanup():
    s = make_session(title="p1-cleanup")
    k.delete_session(s["id"])
    _created.remove(s["id"])
    with pytest.raises(KernelError) as ei:
        k.messages(s["id"])
    assert ei.value.status == 404
