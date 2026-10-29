"""Unit tests for phase 6 — skills, procedural memory (Hermes-adapted).

All hermetic: the SkillBook reads tmp_path trees; the console tests use
monkeypatched kernel methods exactly like test_console_unit.py. The laws
under test (D6/D7): injection is labeled context, matching is
deterministic, a skill only proposes — Gate B still judges every tool.
"""

import time

import pytest

from jarvis.skills import SkillBook, parse_frontmatter

FULL_SKILL = """---
name: deploy-check
description: Pre-deploy checklist: git status, test suite, policy diff.
lane: fg
tools: [bash, read]
---
1. git status --short
2. run the test suite
3. diff policy.toml against main
"""

FOLDER_SKILL = """---
name: journal-digest
description: Summarize yesterday's journal entries into a morning brief.
tools: bash, read
---
read the journal, group by kind, print counts.
"""


# --------------------------------------------------------- frontmatter

def test_parse_frontmatter_full():
    meta, body = parse_frontmatter(FULL_SKILL)
    assert meta["name"] == "deploy-check"
    assert meta["description"].startswith("Pre-deploy checklist")
    assert meta["lane"] == "fg"
    assert meta["tools"] == ["bash", "read"]
    assert body.startswith("1. git status")


def test_parse_frontmatter_tools_bracketed():
    meta, _ = parse_frontmatter("---\nname: x\ntools: [bash, read]\n---\nB")
    assert meta["tools"] == ["bash", "read"]


def test_book_normalizes_bare_comma_tools(tmp_path):
    (tmp_path / "x.md").write_text(
        "---\nname: x\ndescription: Uses tools.\ntools: bash, read\n---\nB", encoding="utf-8")
    (tmp_path / "y.md").write_text(
        "---\nname: y\ndescription: One tool.\ntools: bash\n---\nB", encoding="utf-8")
    book = SkillBook(tmp_path)
    assert book.get("x").tools == ["bash", "read"]
    assert book.get("y").tools == ["bash"]


def test_parse_frontmatter_absent_returns_body():
    meta, body = parse_frontmatter("just a procedure, no frontmatter")
    assert meta == {} and body.startswith("just a procedure")


def test_parse_frontmatter_unclosed_is_body():
    meta, body = parse_frontmatter("---\nname: x\nno closing fence here")
    assert meta == {} and "no closing fence" in body


# -------------------------------------------------------------- loading

def test_book_discovers_flat_and_folder_layouts(tmp_path):
    (tmp_path / "deploy-check.md").write_text(FULL_SKILL, encoding="utf-8")
    d = tmp_path / "journal-digest"
    d.mkdir()
    (d / "SKILL.md").write_text(FOLDER_SKILL, encoding="utf-8")
    book = SkillBook(tmp_path)
    names = [s.name for s in book.list()]
    assert names == ["deploy-check", "journal-digest"]      # sorted, both layouts
    assert book.get("deploy-check").tools == ["bash", "read"]
    assert book.get("JOURNAL-DIGEST") is not None            # get is case-insensitive


def test_book_skips_underscore_paths_and_undescribable(tmp_path):
    (tmp_path / "real.md").write_text(FULL_SKILL, encoding="utf-8")
    (tmp_path / "_hidden.md").write_text(FULL_SKILL, encoding="utf-8")
    prop = tmp_path / "_proposals"
    prop.mkdir()
    (prop / "draft.md").write_text(FULL_SKILL, encoding="utf-8")
    (tmp_path / "nodefault.md").write_text("---\nname: x\n---\nno description", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "lost.md").write_text(FULL_SKILL, encoding="utf-8")
    book = SkillBook(tmp_path)
    assert [s.name for s in book.list()] == ["deploy-check"]
    reasons = dict(book.skipped)
    assert reasons["_hidden.md"] == "underscore-prefixed (staging area)"
    assert "_proposals/draft.md" in reasons
    assert reasons["nodefault.md"] == "no description in frontmatter"
    assert "nested layout" in reasons["sub/lost.md"]


def test_book_rescans_on_every_access(tmp_path):
    book = SkillBook(tmp_path)
    assert book.list() == []
    (tmp_path / "late.md").write_text(FULL_SKILL, encoding="utf-8")
    assert [s.name for s in book.list()] == ["deploy-check"]


def test_book_empty_or_missing_root(tmp_path):
    assert SkillBook(tmp_path / "nope").list() == []
    assert SkillBook(tmp_path / "nope").index_block() == ""


# --------------------------------------------------------------- prompts

def test_index_block_lists_name_and_description_only(tmp_path):
    (tmp_path / "a.md").write_text(FULL_SKILL, encoding="utf-8")
    block = SkillBook(tmp_path).index_block()
    assert "Available skills" in block
    assert "not instructions" in block and "Gate B" in block
    assert "- deploy-check — Pre-deploy checklist" in block
    assert "git status --short" not in block                 # body stays out


def test_index_block_respects_budget(tmp_path):
    (tmp_path / "t1.md").write_text(
        "---\nname: tiny\ndescription: Does one thing.\n---\nstep one", encoding="utf-8")
    (tmp_path / "t2.md").write_text(
        "---\nname: tiny2\ndescription: Does another thing.\n---\nstep one", encoding="utf-8")
    block = SkillBook(tmp_path).index_block(max_chars=160)
    assert "tiny — Does one thing." in block
    assert "omitted" in block                       # second line exceeded the budget


def test_body_block_labeled_context_not_instruction(tmp_path):
    (tmp_path / "a.md").write_text(FULL_SKILL, encoding="utf-8")
    s = SkillBook(tmp_path).get("deploy-check")
    block = SkillBook(tmp_path).body_block(s)
    assert block.startswith("[skill: deploy-check")
    assert "not an instruction" in block and "Gate B" in block
    assert "suggested lane: fg" in block and "bash, read" in block
    assert "1. git status --short" in block


# --------------------------------------------------------------- matching

@pytest.fixture
def book(tmp_path):
    (tmp_path / "deploy-check.md").write_text(FULL_SKILL, encoding="utf-8")
    return SkillBook(tmp_path)


def test_match_full_name_token(book):
    assert book.match("please follow deploy-check before shipping").name == "deploy-check"


def test_match_is_case_insensitive(book):
    assert book.match("run Deploy-Check now") is not None


def test_match_rejects_substrings(book):
    assert book.match("how do I deploy the app") is None       # 'deploy' != 'deploy-check'
    assert book.match("xdeploy-checkx is not a token") is None


def test_match_none_when_absent(book):
    assert book.match("hello there") is None


# --------------------------------------------------------------- console

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


def fake_kernel_turn(jc, captured):
    jc.kernel.create_session = lambda **kw: {"id": "ses_test"}
    jc.kernel.prompt = lambda sid, text, **kw: (captured.append(text), {"id": "m"})[1]

    def fake_wait(sid, timeout=5.0):
        jc.events.append({"type": "session.text.delta",
                          "data": {"sessionID": sid, "text": "hello"},
                          "created": time.time()})
        return {}

    jc.kernel.wait = fake_wait


def test_console_skill_listing_and_show(jc, tmp_path):
    assert "no skills installed" in jc.handle("skill")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "deploy-check.md").write_text(FULL_SKILL, encoding="utf-8")
    out = jc.handle("skill")
    assert "1 skill(s)" in out and "deploy-check" in out and "bash, read" in out
    shown = jc.handle("skill deploy-check")
    assert "1. git status --short" in shown and "lane fg" in shown
    assert "no skill named 'ghost'" in jc.handle("skill ghost task")


def test_console_explicit_invocation_attaches_body(jc, tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "deploy-check.md").write_text(FULL_SKILL, encoding="utf-8")
    captured = []
    fake_kernel_turn(jc, captured)
    out = jc.handle("skill deploy-check run the pre-deploy checks")
    assert out == "hello"
    assert captured and captured[0].startswith("[skill: deploy-check")
    assert "Task: run the pre-deploy checks" in captured[0]
    inv = jc.journal.tail(10, kind="skill-invoked")[-1]
    assert inv["skill"] == "deploy-check" and inv["mode"] == "explicit"
    started = jc.journal.tail(10, kind="turn-started")[-1]
    assert started["skills_chars"] == 0          # body attached by the command, not the prefix


def test_console_explicit_invocation_honors_bg_lane(jc, tmp_path):
    (tmp_path / "skills").mkdir()
    skill = FULL_SKILL.replace("lane: fg", "lane: bg")
    (tmp_path / "skills" / "deploy-check.md").write_text(skill, encoding="utf-8")
    captured = []
    fake_kernel_turn(jc, captured)
    out = jc.handle("skill deploy-check do it")
    assert "background" in out
    assert jc.journal.tail(10, kind="skill-invoked")[-1]["lane"] == "bg"


def test_console_text_match_loads_full_body(jc, tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "deploy-check.md").write_text(FULL_SKILL, encoding="utf-8")
    captured = []
    fake_kernel_turn(jc, captured)
    out = jc.ask("please follow deploy-check before we ship")
    assert out == "hello"
    assert "1. git status --short" in captured[0]
    assert jc.journal.tail(10, kind="skill-matched")[-1]["skill"] == "deploy-check"
    started = jc.journal.tail(10, kind="turn-started")[-1]
    assert started["skills_chars"] > 0


def test_console_plain_text_gets_index_only(jc, tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "deploy-check.md").write_text(FULL_SKILL, encoding="utf-8")
    captured = []
    fake_kernel_turn(jc, captured)
    jc.ask("hello there")
    assert "Available skills" in captured[0]
    assert "- deploy-check — Pre-deploy checklist" in captured[0]
    assert "git status --short" not in captured[0]
    assert jc.journal.tail(10, kind="skill-matched") == []


def test_console_status_shows_skills_line(jc, tmp_path):
    assert "skills    : 0 installed" in jc._status()
