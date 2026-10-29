# JARVIS

A personal AI assistant built as an **active layer on top of the OpenCode kernel**.

> Mental model: Linux kernel → Ubuntu. OpenCode kernel → Jarvis.
> OpenCode provides the agent loop, tools, context, permission prompts, and MCP drivers.
> Jarvis provides initiative (listen, schedule, notice, decide) and a safety gate.

## Status

Phase 6 — skills (procedural memory, Hermes-adapted) built and
unit-tested on top of Phase 5 memory + 5.1 failover; live E2E pending
model-quota reset. Next: Phase 7 — learning loop + delegation (the plan
lives in [`docs/roadmap.md`](docs/roadmap.md)). Run the console:
`PYTHONPATH=src python3 -m jarvis`.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the approved architecture
- [`docs/phase0-baseline.md`](docs/phase0-baseline.md) — live-verified Phase 0 findings
- [docs/phase1-kernel-adapter.md](docs/phase1-kernel-adapter.md) — Phase 1 record + kernel behaviors
- [docs/phase2-gates.md](docs/phase2-gates.md) — Phase 2 safety spine (Gate A/B, broker, subject rule, **gated MCP bridge live**)
- [docs/phase3-jarvisd.md](docs/phase3-jarvisd.md) — Phase 3: jarvisd console v0 + gated MCP bridge (ask/pending/yes-no/status, journal, bridge roundtrip tested)
- [docs/phase4-scheduler.md](docs/phase4-scheduler.md) — Phase 4: T1 scheduler, 08:00 briefing, cycle guards (dedupe, budget, failure auto-disable, DND)
- [docs/phase5-memory.md](docs/phase5-memory.md) — Phase 5: memory — facts, archive, recall, compaction, prompt injection, model knob + failover (§8)
- [docs/phase6-skills.md](docs/phase6-skills.md) — Phase 6: skills — procedural memory, SKILL.md format, progressive disclosure, deterministic matching, `skill` commands
- [docs/decisions.md](docs/decisions.md) — decision log (language, kernel-update ritual, single-machine first, borrowed designs D5, memory stance D6, roadmap D7)
- [docs/roadmap.md](docs/roadmap.md) — the borrowing plan (D5 → build order): Hermes skills/learning loop, OpenClaw gateway + channels, Letta identity + sleep-time compute, phases 6–9
- jarvisd console: `src/jarvis/console/` package — `app.py` (core loop, ask +
  failover, dispatch) + `commands.py` (command surface) + `turns.py` (scan
  helpers); entry `src/jarvis/__main__.py` — ask (fg) / `bg TEXT` / pending / `yes CODE` / `no CODE` / cancel / replay / routines / `run NAME` / `routine NAME off|on` / remember / forget / facts / recall / memory / compact / `model P/M` / models / `skill` / `skill NAME` / `skill NAME TASK` / status, live announcer, journal, turn-latency metrics
- T1 scheduler: src/jarvis/scheduler.py + config/routines.toml — routines fire without a person present, on a dedicated session, guarded (one fire/day, no overlap, budget watchdog, failure auto-disable, DND)
- Memory: `src/jarvis/memory/` package — `facts.py` (subject-written core),
  `archive.py` (verbatim archival), `recall.py` (deterministic search),
  `prompt.py` (injection block), `archiver.py` (turn capture), `compactor.py`
  (compaction plugin); caps in config/policy.toml `[memory]`
- Skills: `src/jarvis/skills.py` — procedural memory (Hermes-adapted):
  SKILL.md book, index injection under `[skills]` budget, deterministic
  name matching, `skill NAME TASK` invocation; skills live in
  `.jarvis/skills/`, proposals reserved under `_proposals/` (phase 7)
- [`docs/opencode-api.lock.json`](docs/opencode-api.lock.json) — the locked API surface

## Current state

- OpenCode `v2.1.4` pinned, live at `http://127.0.0.1:49374`
- Kernel adapter: `src/jarvis/kernel/` package — `adapter.py` (one method per
  locked operation), `http.py` (transport + SSE), `events.py` (EventPipe),
  `config.py`, `errors.py` (stdlib only)
- Tests: `tests/unit/` (hermetic, 142 green) + `tests/live/` (real kernel) —
  `PYTHONPATH=src python3 -m pytest tests/unit -q`
- Default model `opencode/glm-5.3-flash-free`; switch with `model provider/model-id` or `JARVIS_MODEL` env; auto-failover on rate limits via `config/policy.toml [model] failover` (you write the chain — file is the approval)
- `serena` MCP: connected **through jarvis-mcp-gate** (discovery & reads flow free; mutating calls gated by one-time code) · `rea` MCP: registered, **disabled** (until Gate B)
- Feynman `0.5.57`: CLI-only (no MCP server) — wrapped as active-layer tools

## Layout

```
docs/            architecture, phase baselines, roadmap, decisions, API lock
profiles/        profile .md files (personality — NOT workspaces)
src/jarvis/      the active layer
  gates/         Gate A (intent) · Gate B (tool policy) · permission worker
  kernel/        OpenCode adapter: transport, SSE, EventPipe, operation surface
  memory/        core facts · verbatim archive · recall · prompt block · compaction
  console/       text front-end: app (loop+failover) · commands (surface) · turns
  scheduler.py   routines (config/routines.toml)
  skills.py      procedural memory: SKILL.md book · index · deterministic match
  registry.py    owned-session trust registry
  announcer.py   live narration of codes/results
  mcp_gate.py    gated MCP bridge (jarvis-mcp-gate)
sandbox/         WSL/OS confinement procedure
tests/           unit/ (hermetic) · live/ (real kernel) · fixtures
```
