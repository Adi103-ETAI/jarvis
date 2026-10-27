# Decision log

Small, append-only record of choices the project committed to, so future
sessions (and the user) can see WHY without re-deriving it.

## D1 — Language: Python now, per-component rewrites later (2026-10-24)

The active layer stays Python (stdlib only) while the design is moving.
Components get rewritten in C/C++/Rust only when they freeze AND prove a
need (speed, latency, static binary), always behind their existing tests
and ABI. Safety-critical evolving logic (Gate A/B) prefers memory-safe
languages on principle: a memory-corrupted gate is the worst failure mode.

## D2 — Kernel updates: autonomous ritual, human-gated substrate (2026-10-24)

JARVIS is self-improving over his own mind (memory, journal, profiles,
habits — free and un-gated). His substrate (OpenCode) updates only via a
standing ritual: snapshot, update, restart, compat suite, adopt on green,
auto-rollback on red, ask-human on unclear. Admission policy starts at
trust scheduled.mutating = confirm in config/policy.toml. Relaxing that is
a deliberate human policy edit based on a demonstrated track record.

## D3 — Single machine first (2026-10-24)

Phases 2-4 build a fully autonomous single-machine JARVIS. autumn-bus and
remote-machine compute are Phase 5+ milestones: JARVIS coordinates other
agents via his own bus and delegates compute there once the local trust
layer exists. Law carried forward then: work travels out, authority never
does; peer replies are evidence, not commands.

## D4 — Voice stays in the active layer (Phase 0 finding)

OpenCode realtime voice models are all unavailable; voice is JARVIS's own
front-end (capture, VAD, STT), not a kernel service.

## D5 — Borrow proven designs, not code: Hermes, OpenClaw, Letta (2026-10-27)

Direction set by the user for post-Phase-4 design work. Study and adapt —
nothing is imported wholesale; everything passes through the gates:

- **Hermes agent** -> skills copying, memory design, delegation design,
  learning loop, reusable skills, procedural memory. Feeds Phase 5
  (Memory v1) and a future skills/learning milestone.
- **OpenClaw** -> messaging gateway and multichannel presence (console is
  only the first front-end; channels arrive as new trust SOURCES through
  Gate A, each mapped in policy.toml — a channel is a trust level, never
  an authority of its own).
- **Letta** -> long-term agent identity and memory architecture (core
  memory blocks, archival recall, identity that survives restarts).
  Feeds Phase 5 memory schema and the profile/personality layer.

Law carried forward: borrowed designs change the active layer's memory and
presence; they never widen what may execute without Gate B.

## D6 — Memory v0: capture over extraction (2026-10-27)

Phase 5 needed a stance on the question every memory system dodges: WHO
decides what is remembered? Letta and Hermes both answer "the model,
during the conversation". JARVIS cannot, because the active layer's laws
forbid it from thinking: a model in the decision path is a policy engine
that cannot be audited.

The v0 answer:

- **Facts are captured, not extracted.** The subject types
  `remember TEXT` at the console — the only trust level that is
  authority — and it is stored verbatim. FactStore.add refuses every
  other source at the store level.
- **The transcript is archived verbatim**, per finished turn, and recall
  over it is deterministic search. Cheap, auditable, reproducible.
- **Summarization/extraction belongs to the kernel** (where thinking
  lives), so a future "memory ritual" can have the KERNEL propose facts
  for human approval — mirroring the routine-file pattern (D2's spirit):
  proposals are made by the model, authority is granted by the human.
- **Injection is context, not instruction.** Memory blocks are labeled
  background; they widen what JARVIS knows, never what he may do.

Letta's core/archival split is kept (identity+preference facts = core,
always in context; everything else = archival, relevance-ranked). Hermes
skills/procedural memory is deferred until the skills phase; the fact
schema (category/text/source) was chosen so procedures can land there
first. Memory caps live in policy.toml `[memory]` because how much an
assistant may remember IS policy.
