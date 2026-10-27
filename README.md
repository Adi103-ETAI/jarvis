# JARVIS

A personal AI assistant built as an **active layer on top of the OpenCode kernel**.

> Mental model: Linux kernel → Ubuntu. OpenCode kernel → Jarvis.
> OpenCode provides the agent loop, tools, context, permission prompts, and MCP drivers.
> Jarvis provides initiative (listen, schedule, notice, decide) and a safety gate.

## Status

Phase 5 — memory (facts + transcript + recall + compaction) built and
unit-tested (112 hermetic tests); live E2E pending model-quota reset. Run the
console: `PYTHONPATH=src python3 -m jarvis`.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the approved architecture
- [`docs/phase0-baseline.md`](docs/phase0-baseline.md) — live-verified Phase 0 findings
- [docs/phase1-kernel-adapter.md](docs/phase1-kernel-adapter.md) — Phase 1 record + kernel behaviors
- [docs/phase2-gates.md](docs/phase2-gates.md) — Phase 2 safety spine (Gate A/B, broker, subject rule, **gated MCP bridge live**)
- [docs/phase3-jarvisd.md](docs/phase3-jarvisd.md) — Phase 3: jarvisd console v0 + gated MCP bridge (ask/pending/yes-no/status, journal, bridge roundtrip tested)
- [docs/phase4-scheduler.md](docs/phase4-scheduler.md) — Phase 4: T1 scheduler, 08:00 briefing, cycle guards (dedupe, budget, failure auto-disable, DND)
- [docs/phase5-memory.md](docs/phase5-memory.md) — Phase 5: memory — facts, archive, recall, compaction, prompt injection, model knob
- [docs/decisions.md](docs/decisions.md) — decision log (language, kernel-update ritual, single-machine first, borrowed designs D5, memory stance D6)
- jarvisd console: src/jarvis/console.py + src/jarvis/__main__.py — ask (fg) / `bg TEXT` / pending / `yes CODE` / `no CODE` / cancel / replay / routines / `run NAME` / `routine NAME off|on` / remember / forget / facts / recall / memory / compact / `model P/M` / models / status, live announcer, journal, turn-latency metrics
- T1 scheduler: src/jarvis/scheduler.py + config/routines.toml — routines fire without a person present, on a dedicated session, guarded (one fire/day, no overlap, budget watchdog, failure auto-disable, DND)
- Memory: src/jarvis/memory.py — subject-written facts (core), verbatim turn archive (archival), deterministic recall, compaction plugin that re-injects durable facts; caps in config/policy.toml `[memory]`
- [`docs/opencode-api.lock.json`](docs/opencode-api.lock.json) — the locked API surface

## Current state

- OpenCode `v2.1.4` pinned, live at `http://127.0.0.1:49374`
- Kernel adapter: `src/jarvis/kernel/adapter.py` — sessions, prompt, steer,
  interrupt, permissions, fork, compact, events, model ops (stdlib only)
- Live integration tests: `tests/test_adapter_live.py` — 11/11 passed
- Default model `opencode/glm-5.3-flash-free`; switch with `model provider/model-id` or `JARVIS_MODEL` env; auto-failover on rate limits via `config/policy.toml [model] failover` (you write the chain — file is the approval)
- `serena` MCP: connected **through jarvis-mcp-gate** (discovery & reads flow free; mutating calls gated by one-time code) · `rea` MCP: registered, **disabled** (until Gate B)
- Feynman `0.5.57`: CLI-only (no MCP server) — wrapped as active-layer tools

## Layout

```
docs/            architecture, phase baselines, API lock
profiles/        profile .md files (personality — NOT workspaces)
src/jarvis/      active layer (initiative, safety gate, memory, voice) + kernel adapter
sandbox/         WSL/OS confinement procedure
tests/
```
