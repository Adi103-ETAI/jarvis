# Phase 6 — Skills: procedural memory (Hermes, adapted)

Date: 2026-10-29. Hermes contributed the SHAPE of this phase: skills as
folders with a small frontmatter, progressive disclosure (index always in
context, full body on demand), and the habit of turning repeated
procedures into reusable files. Everything else is ours: the gates still
decide, the matching is still deterministic, and the injection is still
labeled context — never authority.

With this phase the memory taxonomy is complete at v1:

    procedural  = skills/          (how to do things — this phase)
    episodic    = archive.jsonl    (what happened — phase 5)
    semantic    = facts.jsonl      (what is true — phase 5)

## The format

One skill = one SKILL.md, in either layout under the skills root:

    <root>/<name>.md            flat skill file
    <root>/<name>/SKILL.md      folder skill (room for scripts later)

Frontmatter keys (parsed by a stdlib line parser — no YAML dependency):

    name         optional; defaults to the file stem
    description  REQUIRED; the skill is invisible without one
    lane         advisory: fg | bg (default fg) — a proposal, not a grant
    tools        advisory list: [a, b] or bare "a, b" — what the skill
                 INTENDS to use; Gate B still judges every real call

Example (`deploy-check.md`):

    ---
    name: deploy-check
    description: Pre-deploy checklist: git status, test suite, policy diff.
    lane: fg
    tools: [bash, read]
    ---
    1. git status --short
    2. run the test suite
    3. diff policy.toml against main

Anything under a `_`-prefixed path (e.g. `skills/_proposals/`) or a
`_`-prefixed file is invisible to the book. That directory is the Phase 7
staging area: the kernel will DRAFT skills there, and `skill approve NAME`
(later) will move them into the visible set. Nested files that are not
`<name>/SKILL.md` are loaded nowhere but explained somewhere: the book
records a skip reason for every file it saw and refused.

## Where skills live

Default root: `.jarvis/skills/` — beside the journal, so the runtime data
directory stays one hermetic unit (tests point it at tmp_path). The
`skill` command and `status` both print the exact path in use.

## The three surfaces

1. **Index injection (progressive disclosure).** Every console/scheduled
   turn gets an index block ahead of the prompt: name + description only,
   hard-bounded by policy `[skills] inject_max_chars` (default 900),
   labeled "context, not instructions". Budget overflow drops whole lines
   and says so ("more skills omitted"). Turn cost is recorded as
   `skills_chars` on the `turn-started` journal line — the same
   accounting `memory_chars` already had.

2. **Deterministic match (full body on demand).** If the request text
   contains a skill's full name as a standalone token (kebab-aware
   boundaries, case-insensitive), the index is replaced by that skill's
   FULL body, labeled "[skill: NAME — procedure context, not an
   instruction; every tool it mentions still passes Gate B]" with its
   advisory lane/tools attached. Journalled as `skill-matched
   mode=text-match`. Substrings never match: a request about "deploy"
   cannot wake "deploy-check". No model call anywhere (D6).

3. **Explicit invocation.** `skill` lists the book; `skill NAME` shows a
   skill to the human; `skill NAME TASK...` composes body + task and
   launches a normal turn on the skill's suggested lane (honored only if
   fg or bg — it is a proposal). Journalled as `skill-invoked
   mode=explicit`. The composed turn is NOT special: Gate A classifies it,
   Gate B judges every tool call in it, and confirmations work exactly as
   on any other turn.

## The laws, honored

- **D6:** matching is name-token rules only; injection is labeled
  context; the turn journal records what rode in and why.
- **D7 (skills propose, Gate B disposes):** a skill's `tools` list cannot
  grant anything — policy.toml still wins on first match; a skill can
  never widen the mutation surface. The suggested lane never escalates
  trust either: `bg` is the LESS privileged lane, and anything else falls
  back to fg.
- **Subject-owned:** only files the human (or an approved Phase 7
  proposal) places under the skills root become skills. The runtime never
  writes there.

## Policy

    [skills]
    enabled = true            # master switch (injection + matching)
    inject_max_chars = 900    # per-turn index budget
    match_enabled = true      # swap index for full body on a name match

Missing stanza = these defaults; `[skills] enabled = false` returns the
console to phase-5 behavior with zero code paths left behind.

## Tests

22 new hermetic tests in tests/unit/test_skills_unit.py (suite: 142
green): frontmatter parsing (bracketed and bare-comma lists, absent and
unclosed frontmatter), both layouts, underscore/nested/duplicate/descriptionless
skips with recorded reasons, index budget truncation, body labeling,
match boundaries (substring refusals), and console-level flows: listing,
showing, explicit invocation (fg + bg lanes), text-match body loading,
index-only injection on plain text, and the status line.

## Honest limits

- Matching is name-token only. A request phrased entirely around
  synonyms will not wake a skill — that is the price of determinism, and
  D6 stays until a decision revises it.
- The index budget is a hard cap: with many skills, later ones drop off
  the injected list (the omission is labeled). Keep descriptions short.
- Bodies are CONTEXT only. A skill cannot carry an executable script;
  whatever the kernel does with the procedure still goes through its own
  tools and Gate B. Hermes' executable-script skills are refused for now.
- Live-kernel behavior is untested (same standing prerequisite as Phase
  5: quota-gated E2E rerun); everything here is hermetically tested.
- `skill NAME TASK` on a `bg` lane returns immediately like `bg` — the
  announcer narrates completion; there is no fg streaming for bg skills.
