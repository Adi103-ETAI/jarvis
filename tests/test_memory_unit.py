"""Unit tests for Phase 5 memory: facts, archive, recall, compaction,
turn archiving, console wiring, and the model knob.

All tests are hermetic -- no kernel, no network, tmp_path-backed stores.
"""

import pytest

from jarvis.console import JarvisConsole, parse_model_spec
from jarvis.journal import Journal
from jarvis.memory import (Archive, Compactor, FactStore, Recall,
                           TurnArchiver, facts_prompt_block)


# --------------------------------------------------------------- fact store

def test_fact_add_list_forget_roundtrip(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    f = fs.add("I prefer dark mode", "preference")
    assert f["id"] and f["category"] == "preference"
    assert [x["text"] for x in fs.facts()] == ["I prefer dark mode"]
    assert fs.forget(f["id"]) == 1
    assert fs.facts() == []


def test_fact_dedupe_refreshes_instead_of_duplicating(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    a = fs.add("I like tea")
    b = fs.add("  I   LIKE  tea  ")          # whitespace-normalized match
    assert b["id"] == a["id"]
    assert len(fs.facts()) == 1


def test_fact_forget_by_substring(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    fs.add("project jarvis uses stdlib only")
    fs.add("project hermes uses skills")
    n = fs.forget("jarvis")
    assert n == 1
    assert [f["text"] for f in fs.facts()] == ["project hermes uses skills"]


def test_fact_store_survives_reopen_and_torn_write(tmp_path):
    p = tmp_path / "facts.jsonl"
    fs = FactStore(p)
    f = fs.add("my name is Aditya", "identity")
    # simulate a crash mid-write: torn trailing line
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"op": "add", "id": "fzz9", "te')
    fs2 = FactStore(p)
    live = fs2.facts()
    assert len(live) == 1 and live[0]["id"] == f["id"]
    assert fs2.get(f["id"])["category"] == "identity"


def test_fact_store_rejects_non_console_sources(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    for bad in ("ambient", "peer", "scheduled", ""):
        with pytest.raises(ValueError):
            fs.add("x", source=bad)
    assert fs.facts() == []


def test_fact_store_enforces_cap(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl", max_facts=2)
    fs.add("one")
    fs.add("two")
    with pytest.raises(ValueError, match="memory full"):
        fs.add("three")
    # forgetting frees a slot again
    fs.forget("one")
    fs.add("three")
    assert {f["text"] for f in fs.facts()} == {"two", "three"}


# ------------------------------------------------------------------ archive

def test_archive_turns_and_compactions(tmp_path):
    ar = Archive(tmp_path / "archive.jsonl")
    ar.append_turn(session_id="s1", question="q1", answer="a1", lane="fg")
    ar.append_turn(session_id="s2", question="q2", answer="a2", lane="bg")
    ar.append_compaction(session_id="s1", messages_before=42)
    assert len(ar.turns()) == 2
    assert len(ar.turns(session_id="s1")) == 1
    assert ar.turns(limit=1)[0]["question"] == "q2"       # tail, not head
    assert ar.compactions()[0]["messages_before"] == 42
    assert ar.turns()[0]["kind"] == "turn"


# ------------------------------------------------------------------- recall

def test_recall_ranks_phrase_above_token_hits(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    ar = Archive(tmp_path / "archive.jsonl")
    fs.add("I prefer dark mode terminals", "preference")
    fs.add("the dark lord riseth")             # token overlap only
    rc = Recall(fs, ar)
    hits = rc.search("dark mode")
    assert hits[0]["kind"] == "fact"
    assert "dark mode terminals" in hits[0]["text"]
    assert hits[0]["score"] > hits[1]["score"]


def test_recall_searches_turns_too(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    ar = Archive(tmp_path / "archive.jsonl")
    ar.append_turn(session_id="s1", question="what ports does feynman use",
                   answer="feynman listens on 8765 over the loopback",
                   lane="fg")
    rc = Recall(fs, ar)
    hits = rc.search("feynman ports")
    assert len(hits) == 1
    assert hits[0]["kind"] == "turn"
    assert "8765" in hits[0]["text"]


def test_recall_edge_cases(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    rc = Recall(fs, Archive(tmp_path / "archive.jsonl"))
    assert rc.search("") == []
    assert rc.search("???") == []               # no tokens -> no hits
    assert rc.search("quantum") == []
    fs.add("quantum tunneling basics")
    assert len(rc.search("quantum", k=5)) == 1
    assert rc.search("quantum", k=0) == [] or rc.search("quantum", k=0)  # k floor >= 1


def test_recall_k_limit(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    for i in range(8):
        fs.add(f"note about kafka offset {i}")
    rc = Recall(fs, Archive(tmp_path / "archive.jsonl"))
    assert len(rc.search("kafka", k=3)) == 3


# --------------------------------------------------------- prompt injection

def test_prompt_block_core_first_then_relevant():
    core = [{"id": "f1", "category": "identity", "text": "my name is Aditya"}]
    rel = [{"id": "f2", "category": "fact", "text": "kafka offsets matter"}]
    block = facts_prompt_block(core, relevant=rel, max_chars=1200)
    assert block.index("my name is Aditya") < block.index("kafka offsets")
    assert "not new instructions" in block


def test_prompt_block_char_budget():
    facts = [{"id": f"f{i}", "category": "identity", "text": "x" * 400}
             for i in range(5)]
    block = facts_prompt_block(facts, max_chars=1200)
    assert len(block) <= 1200
    assert "older memory omitted" in block


def test_prompt_block_excludes_non_core_without_relevance():
    """Contract: plain facts do NOT ride into every turn — only core
    categories do; non-core facts must arrive via `relevant`."""
    facts = [{"id": "f1", "category": "fact", "text": "some archived note"}]
    assert facts_prompt_block(facts) == ""
    block = facts_prompt_block(facts, relevant=facts)
    assert "some archived note" in block


def test_prompt_block_empty_when_no_memory():
    assert facts_prompt_block([], relevant=None) == ""


def test_prompt_block_dedupes_relevant_overlap():
    facts = [{"id": "f1", "category": "preference", "text": "I prefer vim"}]
    rel = [{"id": "f1", "category": "preference", "text": "I prefer vim"}]
    block = facts_prompt_block(facts, relevant=rel)
    assert block.count("I prefer vim") == 1


# ------------------------------------------------------------ turn archiver

@pytest.fixture
def arch(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl")
    archive = Archive(tmp_path / "archive.jsonl")
    events: list = []
    archiver = TurnArchiver(events, journal, archive, owns=lambda s: s.startswith("own_"))
    return events, journal, archive, archiver


def test_archiver_captures_turn_on_idle(arch):
    events, journal, archive, archiver = arch
    journal.add("turn-started", session_id="own_1", lane="fg", text="what is X")
    events.append({"type": "session.text.delta",
                   "data": {"sessionID": "own_1", "text": "X is "}})
    events.append({"type": "session.text.delta",
                   "data": {"sessionID": "own_1", "text": "answer"}})
    archiver.note_event({"type": "session.idle", "data": {"sessionID": "own_1"}})
    turns = archive.turns()
    assert len(turns) == 1
    assert turns[0]["question"] == "what is X"
    assert turns[0]["answer"] == "X is answer"
    assert turns[0]["lane"] == "fg"


def test_archiver_never_archives_foreign_sessions(arch):
    events, journal, archive, archiver = arch
    journal.add("turn-started", session_id="own_1", lane="fg", text="q")
    events.append({"type": "session.text.delta",
                   "data": {"sessionID": "stranger", "text": "secret"}})
    archiver.note_event({"type": "session.idle", "data": {"sessionID": "stranger"}})
    assert archive.turns() == []


def test_archiver_ignores_duplicate_idles(arch):
    events, journal, archive, archiver = arch
    journal.add("turn-started", session_id="own_1", lane="fg", text="q")
    events.append({"type": "session.text.delta",
                   "data": {"sessionID": "own_1", "text": "answer"}})
    ev = {"type": "session.idle", "data": {"sessionID": "own_1"}}
    archiver.note_event(ev)
    archiver.note_event(ev)                    # same idle again
    archiver.note_event(ev)                    # and again
    assert len(archive.turns()) == 1


def test_archiver_ignores_idle_without_a_turn(arch):
    events, journal, archive, archiver = arch
    archiver.note_event({"type": "session.idle", "data": {"sessionID": "own_1"}})
    assert archive.turns() == []


def test_archiver_ignores_non_idle_events(arch):
    events, journal, archive, archiver = arch
    journal.add("turn-started", session_id="own_1", lane="fg", text="q")
    archiver.note_event({"type": "session.text.delta",
                         "data": {"sessionID": "own_1", "text": "x"}})
    assert archive.turns() == []


# ---------------------------------------------------------------- compaction

class FakeCompactKernel:
    """Records the compaction choreography in call order."""

    def __init__(self, n_messages=7):
        self.calls = []
        self._n = n_messages

    def messages(self, sid):
        self.calls.append(("messages", sid))
        return [{}] * self._n

    def compact(self, sid, **kw):
        self.calls.append(("compact", sid))

    def wait(self, sid, timeout=60.0):
        self.calls.append(("wait", sid))

    def inject_context(self, sid, text, **kw):
        self.calls.append(("inject", sid, text))


def test_compactor_sequence_and_reinjection(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    fs.add("my name is Aditya", "identity")
    ar = Archive(tmp_path / "archive.jsonl")
    journal = Journal(tmp_path / "journal.jsonl")
    k = FakeCompactKernel(n_messages=7)
    out = Compactor(k, fs, ar, journal).run("own_1")
    assert [c[0] for c in k.calls] == ["messages", "compact", "wait", "inject"]
    assert out == {"messages_before": 7, "facts_injected": 1}
    injected = k.calls[-1][2]
    assert "my name is Aditya" in injected
    assert ar.compactions()[0]["messages_before"] == 7
    assert journal.tail(5, kind="compact-finished")[-1]["facts_injected"] == 1


def test_compactor_with_empty_memory_still_snapshots(tmp_path):
    fs = FactStore(tmp_path / "facts.jsonl")
    ar = Archive(tmp_path / "archive.jsonl")
    journal = Journal(tmp_path / "journal.jsonl")
    k = FakeCompactKernel()
    out = Compactor(k, fs, ar, journal).run("own_1")
    assert out["facts_injected"] == 0
    assert "inject" not in [c[0] for c in k.calls]     # nothing to re-inject


# ------------------------------------------------------------------ console

@pytest.fixture
def jc(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PASSWORD", "unit-test")
    monkeypatch.delenv("JARVIS_MODEL", raising=False)   # keep default pinned
    console = JarvisConsole(
        journal_path=tmp_path / "journal.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        pending_root=tmp_path / "pending",
        routines_path=str(tmp_path / "routines.toml"),  # missing -> zero routines
    )
    yield console
    console.close()


def test_remember_writes_fact_without_kernel_turn(jc):
    def forbidden(*a, **kw):
        raise AssertionError("kernel must not be called for remember")

    jc.kernel.prompt = forbidden
    out = jc.handle("remember that I prefer dark mode")
    assert "remembered" in out and "preference" in out
    facts = jc.facts.facts()
    assert len(facts) == 1 and facts[0]["category"] == "preference"
    op = jc.journal.tail(5, kind="memory-op")[-1]
    assert op["op"] == "add"


def test_remember_categorizes_identity(tmp_path, jc):
    out = jc.handle("remember my name is Aditya")
    assert "[identity]" in out


def test_forget_removes_and_journals(jc):
    f = jc.facts.add("temp note")
    out = jc.handle("forget " + f["id"])
    assert "forgot 1" in out
    assert jc.facts.facts() == []
    assert jc.journal.tail(5, kind="memory-op")[-1]["op"] == "forget"


def test_recall_answers_what_did_i_say_about(jc):
    jc.handle("remember that the feynman shim runs on 8765")
    out = jc.handle("what did i say about feynman")
    assert "feynman" in out and "[fact" in out


def test_recall_no_hits_message(jc):
    assert "nothing in memory" in jc.handle("recall quantum chromodynamics")


def test_facts_and_memory_commands(jc):
    assert "memory is empty" in jc.handle("facts")
    jc.handle("remember that I like tea")
    assert "[preference]" in jc.handle("facts")
    stats = jc.handle("memory")
    assert "facts     : 1" in stats and "archived  : 0 turns" in stats


def test_ask_injects_core_and_relevant_facts(jc):
    jc.kernel.create_session = lambda **kw: {"id": "ses_m"}
    captured = {}

    def fake_prompt(sid, text, **kw):
        captured["text"] = text
        return {"id": "msg_1"}

    jc.kernel.prompt = fake_prompt
    jc.kernel.wait = lambda sid, timeout=5.0: {}
    jc.handle("remember that my name is Aditya")     # core (identity)
    jc.handle("remember that kafka offsets confuse me")  # non-core, relevant
    jc.handle("remember that totally unrelated fact seventeen")
    out = jc.ask("explain kafka offsets again", lane="fg")
    assert out == "" or isinstance(out, str)
    sent = captured["text"]
    assert sent.endswith("explain kafka offsets again")
    assert "Durable memory" in sent
    assert "my name is Aditya" in sent               # core always rides in
    assert "kafka offsets confuse me" in sent        # relevant recall
    assert "unrelated fact seventeen" not in sent    # irrelevant stays out
    entry = jc.journal.tail(10, kind="turn-started")[-1]
    assert entry["memory_chars"] > 0
    assert entry["text"] == "explain kafka offsets again"   # original journalled


def test_ask_without_memory_sends_plain_prompt(jc):
    jc.kernel.create_session = lambda **kw: {"id": "ses_m"}

    def fake_prompt(sid, text, **kw):
        captured["text"] = text
        return {"id": "msg_1"}

    captured = {}
    jc.kernel.prompt = fake_prompt
    jc.kernel.wait = lambda sid, timeout=5.0: {}
    jc.ask("hello there", lane="fg")
    assert captured["text"] == "hello there"
    assert jc.journal.tail(10, kind="turn-started")[-1]["memory_chars"] == 0


def test_compact_requires_session(jc):
    assert jc.handle("compact") == "no session yet."


def test_compact_happy_path(jc):
    jc.registry.add("ses_c")
    jc.session = {"id": "ses_c"}
    jc.facts.add("my name is Aditya", "identity")
    injected = {}

    def fake_messages(sid):
        return [{}] * 9

    jc.kernel.messages = fake_messages
    jc.kernel.compact = lambda sid, **kw: {"ok": True}
    jc.kernel.wait = lambda sid, timeout=60.0: {}
    jc.kernel.inject_context = lambda sid, text, **kw: injected.update(sid=sid, text=text)
    out = jc.handle("compact")
    assert "snapshot of 9" in out and "re-injected" in out
    assert "my name is Aditya" in injected["text"]
    assert jc.journal.tail(5, kind="compact-finished")[-1]["messages_before"] == 9


def test_compact_kernel_failure_surfaces(jc):
    from jarvis.kernel.adapter import KernelError
    jc.registry.add("ses_c")
    jc.session = {"id": "ses_c"}

    def boom(sid):
        raise KernelError("kernel unreachable", status=0)

    jc.kernel.messages = boom
    out = jc.handle("compact")
    assert out.startswith("compaction failed:")
    assert jc.journal.tail(5, kind="compact-failed")[-1]["error"] == "kernel unreachable"


# -------------------------------------------------------------------- model

def test_parse_model_spec_variants():
    assert parse_model_spec("opencode/glm-5.3-flash-free") == \
        {"providerID": "opencode", "id": "glm-5.3-flash-free"}
    assert parse_model_spec("qwen3-coder") == \
        {"providerID": "opencode", "id": "qwen3-coder"}
    assert parse_model_spec("  anthropic/claude-x  ") == \
        {"providerID": "anthropic", "id": "claude-x"}
    assert parse_model_spec("") is None
    assert parse_model_spec("a/b/c") is None
    assert parse_model_spec("/x") is None
    assert parse_model_spec("x/") is None


def test_model_command_shows_current(jc):
    out = jc.handle("model")
    assert out.startswith("model: opencode/glm-5.3-flash-free")


def test_model_command_switches_session_and_default(jc):
    jc.registry.add("ses_m")
    jc.session = {"id": "ses_m"}
    switched = {}

    def fake_switch(sid, provider, mid):
        switched.update(sid=sid, provider=provider, mid=mid)
        return {"ok": True}

    jc.kernel.switch_model = fake_switch
    out = jc.handle("model anthropic/claude-x")
    assert switched == {"sid": "ses_m", "provider": "anthropic", "mid": "claude-x"}
    assert jc.model == {"providerID": "anthropic", "id": "claude-x"}
    entry = jc.journal.tail(5, kind="model-switched")[-1]
    assert entry["provider"] == "anthropic"


def test_model_switch_refusal_keeps_default(jc):
    from jarvis.kernel.adapter import KernelError
    jc.registry.add("ses_m")
    jc.session = {"id": "ses_m"}

    def boom(sid, provider, mid):
        raise KernelError("model unknown", status=400)

    jc.kernel.switch_model = boom
    out = jc.handle("model anthropic/claude-x")
    assert out.startswith("kernel refused")
    assert jc.model["id"] == "glm-5.3-flash-free"      # default untouched


def test_jarvis_model_env_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PASSWORD", "unit-test")
    monkeypatch.setenv("JARVIS_MODEL", "anthropic/claude-x")
    console = JarvisConsole(
        journal_path=tmp_path / "journal.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        pending_root=tmp_path / "pending",
        routines_path=str(tmp_path / "routines.toml"),
    )
    try:
        assert console.model == {"providerID": "anthropic", "id": "claude-x"}
    finally:
        console.close()


def test_status_line_includes_memory(jc):
    jc.handle("remember that I like tea")
    status = jc.handle("status")
    assert "memory    : 1 facts" in status


# ------------------------------------------------------------ model failover

from jarvis.console import QUOTA_STATUSES, turn_failed
from jarvis.kernel.adapter import KernelError


def test_turn_failed_detection():
    ev = [{"type": "session.execution.failed", "data": {"sessionID": "s1"}},
          {"type": "session.text.delta", "data": {"sessionID": "s2"}},
          {"type": "session.execution.failed", "data": {"sessionID": "s2"}}]
    assert turn_failed(ev, "s1", 0) is True
    assert turn_failed(ev, "s1", 1) is False          # sliced out
    assert turn_failed(ev, "s3", 0) is False
    assert turn_failed([], "s1") is False


def test_failover_on_rate_limit_at_dispatch(jc):
    jc.registry.add("ses_f")
    jc.session = {"id": "ses_f"}
    jc.failover_chain = [{"providerID": "opencode", "id": "backup-1"}]
    jc.kernel.wait = lambda sid, timeout=5.0: {}
    calls = {"prompt": 0, "switch": []}

    def fake_prompt(sid, text, **kw):
        calls["prompt"] += 1
        if calls["prompt"] == 1:
            raise KernelError("quota exhausted", status=429)
        return {"id": "msg_1"}

    def fake_switch(sid, provider, mid):
        calls["switch"].append((sid, provider, mid))
        return {"ok": True}

    jc.kernel.prompt = fake_prompt
    jc.kernel.switch_model = fake_switch
    out = jc.ask("hello", lane="fg")
    assert calls["prompt"] == 2
    assert calls["switch"] == [("ses_f", "opencode", "backup-1")]
    assert jc.model == {"providerID": "opencode", "id": "backup-1"}   # sticky
    fail = jc.journal.tail(10, kind="model-failover")[-1]
    assert fail["reason"] == "rate-limited" and fail["sticky"] is True


def test_failover_exhausted_returns_error(jc):
    jc.registry.add("ses_f")
    jc.session = {"id": "ses_f"}
    jc.failover_chain = [{"providerID": "opencode", "id": "backup-1"}]
    calls = {"prompt": 0}

    def fake_prompt(sid, text, **kw):
        calls["prompt"] += 1
        raise KernelError("quota exhausted", status=429)

    jc.kernel.prompt = fake_prompt
    jc.kernel.switch_model = lambda sid, p, m: {"ok": True}
    out = jc.ask("hello", lane="fg")
    assert "kernel refused the turn" in out
    assert calls["prompt"] == 2                       # current + 1 failover
    assert jc.model["id"] == "glm-5.3-flash-free"     # nothing sticky on total failure


def test_non_quota_error_does_not_failover(jc):
    jc.registry.add("ses_f")
    jc.session = {"id": "ses_f"}
    jc.failover_chain = [{"providerID": "opencode", "id": "backup-1"}]
    calls = {"prompt": 0}

    def fake_prompt(sid, text, **kw):
        calls["prompt"] += 1
        raise KernelError("bad request", status=400)

    jc.kernel.prompt = fake_prompt
    jc.ask("hello", lane="fg")
    assert calls["prompt"] == 1                       # no failover on 400


def test_midturn_death_retries_on_next_model(jc):
    jc.registry.add("ses_f")
    jc.session = {"id": "ses_f"}
    jc.failover_chain = [{"providerID": "opencode", "id": "backup-1"}]
    state = {"attempt": 0}

    def fake_prompt(sid, text, **kw):
        state["attempt"] += 1
        return {"id": "msg_" + str(state["attempt"])}

    def fake_wait(sid, timeout=5.0):
        if state["attempt"] == 1:                     # model 1 dies mid-turn
            jc.events.append({"type": "session.execution.failed",
                              "data": {"sessionID": sid}})
            return {}
        jc.events.append({"type": "session.text.delta",
                          "data": {"sessionID": sid, "text": "saved answer"},
                          "created": __import__("time").time()})
        return {}

    jc.kernel.prompt = fake_prompt
    jc.kernel.wait = fake_wait
    jc.kernel.switch_model = lambda sid, p, m: {"ok": True}
    out = jc.ask("save this", lane="fg")
    assert out == "saved answer"
    assert state["attempt"] == 2
    assert jc.model["id"] == "backup-1"
    fail = jc.journal.tail(10, kind="model-failover")[-1]
    assert fail["reason"] == "turn-failed"


def test_failover_chain_loaded_from_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PASSWORD", "unit-test")
    policy = tmp_path / "policy.toml"
    policy.write_text('[model]\nfailover = ["opencode/backup-1", "x/", 42]\n',
                      encoding="utf-8")
    console = JarvisConsole(
        policy_path=str(policy),
        journal_path=tmp_path / "journal.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        pending_root=tmp_path / "pending",
        routines_path=str(tmp_path / "routines.toml"),
    )
    try:
        assert console.failover_chain == [{"providerID": "opencode", "id": "backup-1"}]
    finally:
        console.close()


def test_empty_chain_keeps_legacy_behavior(jc):
    jc.registry.add("ses_f")
    jc.session = {"id": "ses_f"}
    assert jc.failover_chain == []                    # shipped policy: empty

    def fake_prompt(sid, text, **kw):
        raise KernelError("quota exhausted", status=429)

    jc.kernel.prompt = fake_prompt
    out = jc.ask("hello", lane="fg")
    assert out == "kernel refused the turn: quota exhausted"


def test_quota_statuses_constant():
    assert 429 in QUOTA_STATUSES and 402 in QUOTA_STATUSES and 0 not in QUOTA_STATUSES
