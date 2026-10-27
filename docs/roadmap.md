# Roadmap — the borrowing plan (D5 turned into build order)

Date: 2026-10-28. D5 named the sources (Hermes, OpenClaw, Letta) and the
law: study and adapt, import nothing wholesale, everything passes the
gates. This file turns that direction into a concrete build order — what
each source contributes, where it lands in the active layer, and what is
refused on principle.

The law carried forward (D5): borrowed designs change what JARVIS knows
and where he is present; they never widen what may execute without Gate B.

## Phase 6 — skills & procedural memory (from Hermes)

Hermes pattern: skills as folders (SKILL.md + optional scripts),
progressive disclosure (name + description always in context, full body
loaded on demand), project-local discovery, and the habit of turning
repeated ad-hoc procedures into reusable skills.

What lands in JARVIS:

- `skills/` directory next to the journal. Each skill = SKILL.md
  frontmatter (name, description, suggested lane, tools it intends to
  use) + body.
- A skill INDEX (name + description only) is injected exactly like the
  memory block: char-budgeted via policy `[skills]`, labeled context-
  not-instruction. The full body loads only when a skill is matched or
  explicitly invoked.
- Matching is deterministic (Gate A-style rules, or explicit
  `skill NAME` at the console) — never a model call, same law as D6.
- Gate B keeps final say. A skill PROPOSES tools; policy.toml still wins
  on first-match. A skill can never grant what policy denies.
- Procedural memory = the skills themselves; episodic = archive.jsonl;
  semantic = facts.jsonl. The Phase 5 fact schema already reserved
  `category` room so procedures can land there first and be promoted.

Refused: Hermes' model-in-the-loop routing. The active layer does not
think (D6); classification stays rule-based and auditable.

## Phase 7 — learning loop + delegation + self-editing memory (Hermes + Letta)

- **Learning loop = the proposal pattern.** After compaction or N idle
  turns, a kernel ritual reviews archive + journal and drafts a skill
  proposal into `skills/_proposals/`. The subject approves at the console
  (`skill approve NAME`). Model proposes, human authorizes — D2's ritual
  pattern applied to skills, and D6's deferred "kernel proposes facts"
  finally built.
- **Self-editing memory (Letta's memory tools):** the kernel gets a
  gated memory-write that STAGES fact changes needing console
  confirmation. Same approval machinery as the learning loop — one build.
- **Delegation:** `delegate TASK` spawns a child OpenCode session. The
  child inherits policy but STRICTER (delegation adds restriction, never
  permission); its journal lines are tagged with the parent; its output
  returns as evidence, not commands — D3's law (work travels out,
  authority never does) applied to sub-agents.

Refused: autonomous self-modification without a human gate.

## Phase 8 — messaging gateway & multichannel presence (from OpenClaw)

OpenClaw pattern: one gateway per host, loopback-bound; channels
(Telegram / WhatsApp / Slack / Discord) connect through it; sessions ARE
security boundaries (`main` vs `dm:<channel>:<id>`); pairing + allowFrom
access control; requireMention in groups.

What lands in JARVIS:

- jarvisd IS the gateway skeleton (daemon + EventPipe already exist).
  Channel adapters plug into it; a Telegram adapter is achievable with
  stdlib only (Bot API over HTTPS, no SDK dependency).
- A channel is a trust SOURCE, never an authority (D5). policy.toml maps
  every channel to a lane; the default is peer — observe, brief, answer,
  never mutate. OpenClaw's allowFrom/pairing becomes a policy stanza,
  not a code path.
- Session naming borrowed directly: `dm:telegram:<id>` slots into
  `registry.owns()` and the subject rule unchanged.
- Replies route back through the Announcer with channel targeting;
  confirmations, if ever elevated by a deliberate policy edit, go out
  and expire on the same channel.

Refused: OpenClaw's chat-driven tool execution. Peer messages are
recorded, never executed; the console stays the only mutating surface
unless the subject deliberately edits policy.

## Phase 9 — identity block + sleep-time compute (from Letta)

Already shipped in Phase 5: core/archival split, deterministic recall,
compaction with durable-fact re-injection, caps in policy. The remainder:

- **Identity block:** `identity` facts always inject FIRST and are
  human-tombstone-only — `forget` cannot remove identity; only the
  subject can. Small guard + prompt-order change.
- **Self-editing memory** (listed under Phase 7) is Letta's other
  borrow — shared build with the learning loop.
- **Sleep-time compute (OpenClaw calls it "dreaming"):** a nightly
  Scheduler T1 job — compact stale sessions, quality-check recall, stage
  skill proposals for morning approval. The Phase 4 scheduler runs this
  for free; no new daemon.

Refused: embedding-based recall and model-managed memory managers (D6:
deterministic, auditable, reproducible — unless a decision revises D6).

## Already landed from this plan

- **Phase 5 (Letta, part 1):** memory v0 — facts, archive, recall,
  compaction. See docs/phase5-memory.md.
- **Phase 5.1 (standalone):** model failover on rate limits — policy
  `[model] failover` chain, QUOTA-triggered, sticky winner, journalled +
  announced. See docs/phase5-memory.md §8.

## Standing prerequisites (not phase-numbered)

- **Live E2E rerun** — blocked on free-model quota reset. Rerun the
  tests/live/ suites (adapter, gateb, jarvisd), and verify the
  adapter's compact / inject_context / switch_model / list_models paths
  against the real kernel (unit tests use fakes for those).
- **OS sandbox decision** — bubblewrap/userns vs a dedicated WSL distro.
  Hard prerequisite before scheduled MUTATING routine jobs run on a real
  machine unattended.

## Order rationale

Skills first: they are the unit delegation delegates and the thing the
learning loop produces, and they reuse Phase 5's injection machinery for
free. Learning loop + delegation second: they share the
proposal/approval machinery, so one build serves both. Gateway third:
channels multiply blast radius, so they arrive once safe defaults are
proven and there is a brain worth exposing. Identity + sleep-time last:
small, and they ride infrastructure that will already exist.
