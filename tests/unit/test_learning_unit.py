"""Unit tests for phase 7 — learning loop, staged memory, delegation.

All hermetic. The laws under test (D2/D3/D6/D7): the kernel PROPOSES and
the human disposes; a delegate inherits policy but STRICTER (it can never
spawn another delegate); delegate output is evidence, never commands;
staged memory changes apply only through the subject's own write path.
"""

import time

import pytest

from jarvis.journal import Journal
from jarvis.kernel.adapter import KernelError
from jarvis.learning import LearningLoop, parse_proposals
from jarvis.memory.facts import FactStore
from jarvis.memory.staging import StagedMemory
from jarvis.skills import SkillBook

RITUAL_REPLY = """I reviewed the session quietly.
```skill-proposal
---
name: journal-digest
description: Summarize the day's journal into a morning brief.
lane: fg
tools: [read]
---
1. read the journal
2. group entries by kind
```
```memory-proposal
remember: The subject prefers concise answers.
forget: stale old preference text
```
Done reviewing."""


# -------------------------------------------------------------- parsing

def test_parse_proposals_extracts_both_kinds():
    drafts, memory, malformed = parse_proposals(RITUAL_REPLY)
    assert malformed == 0
    assert [d["name"] for d in drafts] == ["journal-digest"]
    assert drafts[0]["description"].startswith("Summarize")
    assert drafts[0]["tools"] == ["read"] and drafts[0]["lane"] == "fg"
    assert "group entries by kind" in drafts[0]["body"]
    assert ("remember", "The subject prefers concise answers.") in memory
    assert ("forget", "stale old preference text") in memory


def test_parse_proposals_counts_malformed_and_drops():
    text = """
```skill-proposal
---
name: _stealth
---
no description, no body
```
```memory-proposal
nonsense line without a verb:
```
no fences at all here
"""
    drafts, memory, malformed = parse_proposals(text)
    assert drafts == [] and memory == [] and malformed == 2


def test_parse_proposals_empty_reply():
    assert parse_proposals("") == ([], [], 0)
    assert parse_proposals("just a normal answer, nothing staged") == ([], [], 0)


# --------------------------------------------------------- staged memory

def test_staged_memory_lifecycle(tmp_path):
    st = StagedMemory(tmp_path / "mp.jsonl")
    a = st.stage("remember", "likes tea")
    b = st.stage("forget", "old text")
    assert st.unresolved_count() == 2
    with pytest.raises(ValueError):
        st.stage("purge", "not a verb")
    with pytest.raises(ValueError):
        st.stage("remember", "   ")
    assert st.resolve(a["id"], True)["resolved"]["accepted"] is True
    assert st.resolve(b["id"], False)["resolved"]["accepted"] is False
    assert st.list() == []
    assert st.resolve(a["id"], True) is None          # already consumed
    entries = st._read()                              # audit trail survives
    assert len(entries) == 2 and all(e["resolved"] for e in entries)


# --------------------------------------------------------- learning loop

def make_loop(tmp_path, reply, **cfg):
    journal = Journal(tmp_path / "journal.jsonl")
    skills = SkillBook(tmp_path / "skills")
    staging = StagedMemory(tmp_path / "mp.jsonl")
    launches = []

    loop = LearningLoop(journal, skills, staging,
                        cfg={"review_hours": 0, **cfg},
                        launch=lambda prompt: (launches.append(prompt),
                                               loop.begin_watch("ses_c", 0),
                                               True)[2],
                        replay=lambda sid, start: reply,
                        recent_turns=lambda n: ["turn one"],
                        activity_counts=lambda: {"turn-metrics": 1},
                        emit=lambda line: None)
    return loop, launches, journal


def test_threshold_dispatches_and_stages_everything(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY, every_n_turns=2)
    assert loop.note_turn() is None                      # 1 < 2
    out = loop.note_turn()                               # threshold reached
    assert out and "ritual dispatched" in out
    assert len(launches) == 1 and "learning ritual" in launches[0]
    assert "Session evidence" in launches[0]
    loop.note_event({"type": "session.idle", "data": {"sessionID": "ses_c"}})
    assert [p.name for p in loop.skills.proposals()] == ["journal-digest"]
    assert loop.staging.unresolved_count() == 2
    result = journal.tail(10, kind="learning-ritual")[-1]
    assert result["stage"] == "result" and result["skills"] == ["journal-digest"]
    assert result["memory"] == 2 and result["malformed"] == 0


def test_no_watch_no_staging_from_stray_idles(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY, every_n_turns=2)
    loop.note_event({"type": "session.idle", "data": {"sessionID": "ses_c"}})
    assert loop.skills.proposals() == []                 # nothing armed yet
    loop.note_turn(); loop.note_turn()                   # dispatch, watch armed
    loop.note_event({"type": "session.idle", "data": {"sessionID": "ses_other"}})
    assert loop.staging.unresolved_count() == 0          # other sid: ignored, watch intact
    loop.note_event({"type": "session.idle", "data": {"sessionID": "ses_c"}})
    assert loop.staging.unresolved_count() == 2          # armed sid consumed
    results = [e for e in journal.tail(10, kind="learning-ritual")
               if e["stage"] == "result"]
    assert len(results) == 1                             # consumed exactly once
    loop.note_event({"type": "session.idle", "data": {"sessionID": "ses_c"}})
    results = [e for e in journal.tail(10, kind="learning-ritual")
               if e["stage"] == "result"]
    assert len(results) == 1                             # disarmed: idle ignored
    assert loop.staging.unresolved_count() == 2          # nothing re-staged


def test_failed_ritual_turn_stages_nothing(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY, every_n_turns=1)
    loop.note_turn()
    loop.note_event({"type": "session.execution.failed",
                     "data": {"sessionID": "ses_c"}})
    assert loop.skills.proposals() == [] and loop.staging.list() == []
    result = journal.tail(10, kind="learning-ritual")[-1]
    assert "failed" in result["outcome"]


def test_review_window_prevents_bursts(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY,
                                        every_n_turns=1, review_hours=20)
    loop.note_turn()
    assert len(launches) == 1
    # still in flight (no completion event yet): the one-at-a-time guard fires
    assert "already in flight" in loop.note_turn()
    # ritual completes; the window guard then blocks the next attempt
    loop.note_event({"type": "session.idle", "data": {"sessionID": "ses_c"}})
    out = loop.note_turn()
    assert "reviewed recently" in out
    assert len(launches) == 1


def test_max_staged_pauses_proposing(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY,
                                        every_n_turns=1, max_staged=2)
    loop.skills.stage_proposal("p1", "One.", "a")
    loop.skills.stage_proposal("p2", "Two.", "b")
    out = loop.note_turn()
    assert "await review" in out and launches == []
    assert journal.tail(10, kind="learning-ritual")[-1]["reason"].startswith("2")


def test_disabled_loop_never_fires(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY,
                                        every_n_turns=1, enabled=False)
    assert loop.note_turn() is None
    assert launches == [] and journal.tail(10, kind="learning-ritual") == []


def test_compaction_triggers_review(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY, every_n_turns=0)
    assert "after-compaction" in (loop.note_compaction() or "")
    assert len(launches) == 1


def test_kernel_unreachable_skips_cleanly(tmp_path):
    loop, launches, journal = make_loop(tmp_path, RITUAL_REPLY, every_n_turns=1)
    loop.launch = lambda prompt: False
    out = loop.note_turn()
    assert "kernel unreachable" in out
    assert journal.tail(10, kind="learning-ritual")[-1]["stage"] == "skipped"


# ------------------------------------------------------------- delegation

@pytest.fixture
def jc(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PASSWORD", "unit-test")
    from jarvis.console import JarvisConsole
    console = JarvisConsole(
        journal_path=tmp_path / "journal.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        pending_root=tmp_path / "pending",
        routines_path=str(tmp_path / "routines.toml"),
        skills_dir=tmp_path / "skills",
    )
    yield console
    console.close()


def fake_child(jc, reply):
    jc.kernel.create_session = lambda **kw: {"id": "ses_child", "title": kw.get("title", "delegate")}

    def prompt(sid, text, **kw):
        jc.events.append({"type": "session.text.delta",
                          "data": {"sessionID": sid, "text": reply},
                          "created": time.time()})
        return {"id": "msg_1"}

    jc.kernel.prompt = prompt
    jc.kernel.wait = lambda sid, timeout=5.0: {}


def test_delegate_fg_child_is_stricter_and_returns_evidence(jc):
    fake_child(jc, "findings: the largest file is /tmp/a (2MB)")
    out = jc.handle("delegate find the largest file in tmp")
    assert out.startswith("findings:")
    assert jc.worker._trust["ses_child"] == "delegated"
    assert jc.registry.owns("ses_child")
    started = jc.journal.tail(10, kind="delegate-started")[-1]
    assert started["child"] == "ses_child" and started["parent"] is None
    result = jc.journal.tail(10, kind="delegate-result")[-1]
    assert result["child"] == "ses_child" and result["chars"] > 0
    turn = jc.journal.tail(10, kind="turn-started")[-1]
    assert turn["delegated"] is True


def test_delegate_prompt_carries_frame_not_authority(jc):
    captured = []
    jc.kernel.create_session = lambda **kw: {"id": "ses_child"}
    jc.kernel.prompt = lambda sid, text, **kw: (captured.append(text), {"id": "m"})[1]
    jc.kernel.wait = lambda sid, timeout=5.0: {}
    jc.handle("delegate summarize the repo")
    assert "[delegation" in captured[0]
    assert "EVIDENCE" in captured[0] and "may not delegate further" in captured[0]
    assert "Task: summarize the repo" in captured[0]


def test_delegate_bg_announces_completion(jc):
    fake_child(jc, "bg evidence")
    out = jc.handle("delegatebg scan the logs")
    assert "background" in out
    assert "ses_child" in jc.announcer.announce_sids
    lines = []
    jc.announcer.on_line = lines.append                 # capture narration
    jc.announcer.note_event({"type": "session.idle",
                             "data": {"sessionID": "ses_child"}})
    assert any("delegate finished" in ln for ln in lines)
    assert "ses_child" not in jc.announcer.announce_sids   # consumed once


def test_delegate_kernel_refusal_surfaces(jc):
    def boom(**kw):
        raise KernelError("nope", status=0)
    jc.kernel.create_session = boom
    assert "kernel refused the delegate session" in jc.handle("delegate x")
    assert jc.handle("delegate") == "usage: delegate TASK"


def test_delegates_listing_and_evidence_readback(jc):
    assert "no delegations yet" in jc.handle("delegates")
    fake_child(jc, "the evidence text")
    jc.handle("delegate do the thing")
    listing = jc.handle("delegates")
    assert "ses_child" in listing and "done," in listing
    read = jc.handle("delegate-read ses_child")
    assert "never executed" in read and "the evidence text" in read
    assert "no captured output" in jc.handle("delegate-read ses_missing")


# ----------------------------------------------------------- gate policy

def test_policy_delegated_cannot_spawn_delegates():
    from jarvis.policy import Policy
    pol = Policy()                                     # shipped policy.toml
    v = pol.decide("task", ["anything"], "delegated")
    assert v.decision == "reject" and v.rule == "trust.delegated.deny_actions"
    # a delegate's ordinary mutating call stays on the human's desk:
    assert pol.decide("shell", ["touch /tmp/x"], "delegated").decision == "confirm"
    # the console was never denied the task action:
    assert pol.decide("task", ["anything"], "console").decision == "confirm"


# ------------------------------------------------------- console staging

def test_console_skill_proposal_flow(jc, tmp_path):
    assert "nothing staged" in jc.handle("skill proposals")
    pdir = tmp_path / "skills" / "_proposals"
    pdir.mkdir(parents=True)
    (pdir / "digest.md").write_text(
        "---\nname: digest\ndescription: Daily brief.\n---\n1. read\n",
        encoding="utf-8")
    out = jc.handle("skill proposals")
    assert "1 skill proposal(s)" in out and "digest" in out
    assert "installed" in jc.handle("skill approve digest")
    assert "digest" in jc.handle("skill")
    assert "refused" in jc.handle("skill approve digest")   # already installed


def test_console_fact_proposal_flow(jc):
    e = jc.staging.stage("remember", "I prefer dark mode")
    assert e["id"] in jc.handle("fact proposals")
    assert "remembered" in jc.handle("fact approve " + e["id"])
    assert any("dark mode" in f["text"] for f in jc.facts.facts())
    e2 = jc.staging.stage("forget", "dark mode")
    assert "refused" in jc.handle("fact reject " + e2["id"])
    assert jc.staging.list() == []                          # both consumed
    assert "no open proposal" in jc.handle("fact approve " + e["id"])
    assert "usage:" in jc.handle("fact bogus")
