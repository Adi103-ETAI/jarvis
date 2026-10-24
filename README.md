# JARVIS

A personal AI assistant built as an **active layer on top of the OpenCode kernel**.

> Mental model: Linux kernel → Ubuntu. OpenCode kernel → Jarvis.
> OpenCode provides the agent loop, tools, context, permission prompts, and MCP drivers.
> Jarvis provides initiative (listen, schedule, notice, decide) and a safety gate.

## Status

Phase 0 — kernel substrate. See:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the approved architecture
- [`docs/phase0-baseline.md`](docs/phase0-baseline.md) — live-verified Phase 0 findings
- [`docs/opencode-api.lock.json`](docs/opencode-api.lock.json) — the locked API surface
- Realities corrected vs. architecture: see `docs/phase0-baseline.md` §6

## Current state

- OpenCode `v2.1.4` pinned, live at `http://127.0.0.1:49374`, password from `~/.config/opencode/service.json`
- `serena` MCP: connected
- `rea` MCP: registered, **disabled** (enabled only after Gate B policy tests)
- Feynman `0.5.57`: shim fixed (version-agnostic), alphaXiv auth ok
- OpenCode voice: investigated, **ruled out** — voice stays in the active layer

## Layout

```
docs/            architecture, phase baselines, API lock
profiles/        profile .md files (personality — NOT workspaces)
src/jarvis/      active layer (initiative, safety gate, memory, voice) + kernel adapter
sandbox/         WSL/OS confinement procedure
tests/
```
