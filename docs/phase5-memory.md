# Phase 5 — memory (facts + transcript + recall + compaction)

Date: 2026-10-27. Deliverable (build order #4): "what did I say about X"
is answerable from stored memory, and state survives compaction. Design
follows D5 borrowings (Letta core/archival split, Hermes memory shape)
filtered through D6 (capture over extraction — see decisions.md).

## 1. What now exists

| Piece | File | Job |
|---|---|---|
| FactStore | src/jarvis/memory/facts.py | core memory (Letta analog): subject-written durable facts, append-only JSONL, tombstone forgetting, dedupe-refresh, hard cap |
| Archive | src/jarvis/memory/archive.py | archival memory (Letta analog): every finished JARVIS turn (question + full answer), verbatim, plus compaction snapshots |
| Recall | src/jarvis/memory/recall.py | deterministic search over facts + turns (token overlap + exact phrase + recency). No embeddings, no model — auditable and free |
| TurnArchiver | src/jarvis/memory/archiver.py | EventPipe subscriber: on session.idle, archives the turn for JARVIS-OWNED sessions only (fg, bg, and routine turns, one path) |
| Compactor | src/jarvis/memory/compactor.py | the compaction plugin: snapshot -> kernel compact -> durable facts re-injected as background |
| Prompt injection | console._memory_prefix | core facts (identity/preference) ride into every console+scheduled turn; non-core facts join only when relevant to the prompt |
| Console commands | src/jarvis/console/ | `remember TEXT` / `forget ID\|TEXT` / `facts` / `recall QUERY` / `memory` / `compact` / `model [P/M]` / `models` |
| Memory caps | config/policy.toml `[memory]` | max_facts, inject_max_chars, relevant_top_k — memory is policy, so its limits live in the policy file |

## 2. The law of memory

- **Capture over extraction.** The active layer never runs a model to
  decide what to remember. A fact exists because the SUBJECT typed
  `remember ...` at the console — the one trust level that is authority.
  `FactStore.add` hard-refuses every other source (ambient, peer,
  scheduled) at the store level, not just at the UI level.
- **Memory widens context, never authority.** Injected facts are labeled
  "background the subject asked you to keep; not new instructions" so the
  kernel treats them as context. Nothing in memory can execute anything;
  every action still passes Gate B exactly as before.
- **The archive is verbatim.** Turns are stored as spoken, not
  summarized — summaries can be rebuilt any time; the original cannot.
- **Forgetting is tombstoning.** History is never rewritten; `forget`
  appends a tombstone line. The replayed state drops the fact; the file
  keeps the record (audit consistency with the journal).
- **Privacy: owned sessions only.** The kernel event stream is global.
  The archiver filters by `registry.owns()` so a foreign TUI session's
  transcript can never leak into JARVIS memory.
- **Recall is deterministic.** Token overlap + exact-phrase bonus +
  recency boost, stdlib only. The same query returns the same ranking —
  no silent model drift between "what did I say" answers.

## 3. Compaction: state survives the context window

`compact` (console command) runs the plugin on the console session:

1. **Snapshot** — message count archived (`kind: "compaction"`), journalled
   as `compact-started`.
2. **Kernel compacts** — the model summarizes in-kernel, where thinking
   belongs (the active layer does not think, D6).
3. **Re-injection** — durable facts render into a background block and
   ride back via `inject_context` (delivery=queue). Identity and
   preferences survive even though the old messages are gone.

Gate (build order #4): after compaction, "what did I say about X" still
answers from facts + archive, and the session's next turn carries the
core memory again. Unit-tested with a recording fake kernel; live check
pending model quota.

## 4. Injection economics

A turn never pays for memory it does not use:

- **Core facts** (identity, preference) ride in always — they are few by
  construction (the subject typed them).
- **Non-core facts** join only when Recall scores them against the prompt
  (top_k from `[memory] relevant_top_k`, default 3).
- The whole block is char-budgeted (`inject_max_chars`, default 1200) and
  the journal records `memory_chars` per turn, so injection cost is
  measurable, not vibes.
- Gate A still classifies the ORIGINAL text; the journal records the
  original — the prefix is transport, not intent.

## 5. The model knob (asked for explicitly: "can't we change models?")

The model was never kernel-hardcoded — it was a Python default
(`DEFAULT_MODEL` in console.py). Phase 5 makes it a first-class knob:

- `model provider/model-id` — switches the current owned session (kernel
  `switch_model`, subject rule enforced) AND the default for future
  sessions; journalled as `model-switched`. A kernel refusal leaves the
  default untouched.
- `model` — show current; `models` — list the kernel's catalog.
- `JARVIS_MODEL=provider/model-id` env var — overrides at boot, before
  any session exists.
- Resolution order: explicit arg > env > pinned default
  (`opencode/glm-5.3-flash-free`).

## 6. What is deliberately NOT here (deferred)

- **LLM fact extraction** from conversation — a future kernel-turn ritual
  (the kernel proposes facts; the human approves; the active layer stays
  deterministic). D6 records the reasoning.
- **Embeddings / semantic recall** — current queries are lexical; if
  recall misses, the answer is better facts, not a vector DB.
- **Hermes skills / procedural memory** — the FactStore schema
  (category + text + source) was chosen so a future skills phase can
  store procedures as facts first and promote them later.
- **Cross-session identity profile** — Letta-style persona blocks beyond
  identity/preference facts.

## 7. Acceptance checklist

- [x] "what did I say about X" answered from facts + archive (recall tests)
- [x] State survives compaction (snapshot -> compact -> re-inject, tested)
- [x] Facts subject-written only (source guard, tested: ambient/peer/scheduled refused)
- [x] Forgetting never rewrites history (tombstone replay, tested)
- [x] Torn final write never corrupts state (crash-safety test)
- [x] Foreign sessions never archived (owns-filter test)
- [x] Duplicate idles never double-archive (dedupe test)
- [x] Injection budgeted and journalled (`memory_chars`, tested)
- [x] Model switchable per session + default + env (tested)
- [x] Full hermetic suite green: 112 passed (72 pre-existing + 40 new)
- [ ] Live E2E against the real kernel (blocked: model quota; rerun with the other live suites)

## 8. Model failover — surviving rate limits (phase 5.1)

Question that forced this: "if the OpenCode model hits its rate limit,
how does JARVIS change it?" Before: it didn't — the turn died with
"kernel refused", and the human ran `model P/M` by hand. Now:

- **Trigger** (deterministic, no judgement): kernel refuses dispatch with
  a QUOTA status (429 rate limit / 402 payment), OR the model dies
  mid-turn (`session.execution.failed` event for the session).
- **Action**: try the next model in `config/policy.toml [model] failover`,
  in order. The session is redirected (`switch_model`, subject rule
  enforced), the same prompt is re-dispatched, and the turn continues.
- **Sticky**: the winner becomes the console default, so the next turn
  does not repeat the dead model's refusal. A failed switch or a totally
  exhausted chain surfaces a clear error; nothing retries forever.
- **The chain is policy**: you approve it by writing it in the file
  (same law as routines.toml — D2's spirit). Runtime never widens it;
  the shipped default is empty (legacy behavior: refuse and report).
  Kernel-down is NOT failover — no model fixes a dead kernel.
- **Not failover material**: bad requests (400), unknown errors, kernel
  unreachable — a different model would die identically.

Journalled as `model-failover` (from/to/reason/sticky) and announced
live through the announcer. Applies to console, bg, and scheduled
(routine) turns alike, since they share `ask()`.
