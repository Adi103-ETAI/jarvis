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
