# JARVIS

A personal AI assistant built as an **active layer on top of the OpenCode kernel**.

> Mental model: Linux kernel → Ubuntu. OpenCode kernel → Jarvis.
> OpenCode provides the agent loop, tools, context, permission prompts, and MCP drivers.
> Jarvis provides initiative (listen, schedule, notice, decide) and a safety gate.

## Status

Phase 3 — jarvisd console (turn runner) under construction; spine live-tested. Run the console: `PYTHONPATH=src python3 -m jarvis`. See:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the approved architecture
- [`docs/phase0-baseline.md`](docs/phase0-baseline.md) — live-verified Phase 0 findings
- [docs/phase1-kernel-adapter.md](docs/phase1-kernel-adapter.md) — Phase 1 record + kernel behaviors
- [docs/phase2-gates.md](docs/phase2-gates.md) — Phase 2 safety spine (Gate A/B, broker, subject rule, **gated MCP bridge live**)
- [docs/phase3-jarvisd.md](docs/phase3-jarvisd.md) — Phase 3: jarvisd console v0 + gated MCP bridge (ask/pending/yes-no/status, journal, bridge roundtrip tested)
- [docs/decisions.md](docs/decisions.md) — decision log (language, kernel-update ritual, single-machine first)
- jarvisd console v0: src/jarvis/console.py + src/jarvis/__main__.py — ask/pending/yes/no/status, journal
- [`docs/opencode-api.lock.json`](docs/opencode-api.lock.json) — the locked API surface

## Current state

- OpenCode `v2.1.4` pinned, live at `http://127.0.0.1:49374`
- Kernel adapter: `src/jarvis/kernel/adapter.py` — sessions, prompt, steer,
  interrupt, permissions, fork, compact, events, model ops (stdlib only)
- Live integration tests: `tests/test_adapter_live.py` — 11/11 passed
- Default model fixed (dead → `opencode/glm-5.3-flash-free`); sessions pin models explicitly
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
