# Phase 0 Baseline — kernel substrate

Recorded 2026-10-24 by Phase 0. Everything below was **verified live** on this machine, not assumed from docs. Where the architecture document says one thing and the machine says another, **the machine wins**; every variance is listed under "Corrections".

OpenCode is the pinned runtime for JARVIS. This file is the baseline for that pin.

---

## 1. Runtime kernel (OpenCode)

| Item | Value | Verified via |
|---|---|---|
| **Version** | **`v2.1.4`** | `opencode --version` |
| **Config generation** | V2 | `~/.config/opencode/opencode.jsonc` uses `mcp.servers.<name>` shape; no V1 keys |
| **Live kernel** | `http://127.0.0.1:49374` | `opencode service status` |
| **Auth** | `Authorization: Basic base64(opencode:<password>)` | `~/.config/opencode/service.json` holds the password |
| **Config file** | `~/.config/opencode/opencode.jsonc` | `GEMINI_HOME`/`OPENCODE_*` unset; standard XDG path |

> Note — the architecture document's Phase-0 plan assumed OpenCode `1.17.13` and "random password per boot". Reality: **v2.1.4** and a **stable password persisted in `service.json`**. The password pin is already solved; no extra work needed.

## 2. API surface lock

- **Spec fetched** live from the running kernel at `/openapi.json`, saved as **`docs/opencode-api.lock.json`** (290 KB, **124 operations**).
- Still self-described as `"Experimental HttpApi surface for selected instance routes."` — therefore the pin in `config/jarvis.toml` must not be bumped silently. Changing it means re-locking the spec and re-running Phase 1 tests.

### Confirmed present (permission / interruption / bridge paths)

| Endpoint | Purpose |
|---|---|
| `POST /api/session` | create session |
| `POST /api/session/{sid}/prompt` | start turn |
| `POST /api/session/{sid}/interrupt` | stop current turn |
| `POST /api/session/{sid}/permission/{rid}/reply` | **programmatic permission decision** |
| `GET  /api/permission/request` | list pending permission requests |
| `POST /api/session/{sid}/synthetic` | inject write-only context |
| `POST /api/session/{sid}/fork` | branch a session |
| `POST /api/session/{sid}/compact` | force compaction |
| `GET  /api/event` | SSE event stream |
| `GET  /api/mcp` | live MCP driver status |
| `POST /api/experimental/mcp/{server}/connect` | enable/disable MCP at runtime |

The Gate B / Gate A design is directly supported by these.

## 3. Driver verification

### Serena — ✅ already connected
`GET /api/mcp` → `{"name":"serena","status":{"status":"connected"}}`. No action needed.

### REA — ✅ installed, ✓ MCP-verified, deliberately **disabled**
- Pinned install: `~/.local/opt/rea/node_modules/.bin/rea` — **`rea-agents@6.7.0`**, 174 packages.
- Registered in `~/.config/opencode/opencode.jsonc` under `mcp.servers.rea` with **`"disabled": true`** and a `$comment` explaining why.
- Live kernel sees it: `{"name":"rea","status":{"status":"disabled"}}` — exactly the architecture's "enabled only after Gate B policy tests pass".
- **`rea --mcp` stdio MCP handshake verified out-of-band**: `initialize` → `serverInfo={name:'rea', version:'6.7.0'}`, `protocolVersion=2024-11-05`, then `tools/list` → **207 tools**. So the driver *will* come up cleanly when Gate B is ready.
- `rea doctor` reports missing analysis engines (`Hopper`, `Ghidra`, `IDA`, `jadx`, `kuna`) — expected; native RE stays out of the first milestone per the architecture.
- Backups created: `~/.config/opencode/opencode.jsonc.bak-phase0`.

### Feynman — ✅ fixed and more robust than the doc assumed
- Architecture doc said `0.5.51`. Reality: **`0.5.57`** is the only version installed (0.5.51 dir is gone).
- The old shim at `~/.feynman/bin/feynman` hardcoded 0.5.34 and was broken; a nicer `~/.local/bin/feynman` existed pointing to 0.5.57.
- **Replaced `~/.feynman/bin/feynman` with a version-agnostic shim** that resolves the newest `~/.local/share/feynman/feynman-*-linux-x64` install at runtime, prefers the bundled launcher, and falls back to bundled node. This removes the exact recurring bug that broke it in the first place.
- Backup created: `~/.feynman/bin/feynman.bak-pre-phase0`.
- `feynman doctor` reports alphaXiv auth ok, user `SentArc Labs`, model `anthropic/claude-opus-5-5` (valid), pi runtime ok, search keys unconfigured.
- Backfill note: the architecture doc's §16 row citing "0.5.51" should read "0.5.57".

## 4. OpenCode voice capability — checked and **ruled out**

`GET /api/experimental/voice/models` (live, v2.1.4):
```json
{"data":{"models":[
  {"model":"gpt-live-1","provider":"openai","transports":["webrtc"],"available":false},
  {"model":"gpt-realtime-2.1","provider":"openai","transports":["webrtc","websocket"],"available":false},
  {"model":"grok-voice-3.0","provider":"xai","transports":["websocket"],"available":false},
  {"model":"gemini-3.8-live","provider":"google","transports":["websocket"],"available":false}]}}
```

All four are third-party realtime speech-to-speech providers, and **all four report `available: false`** on this machine. That closes Open Question §15.5 in the architecture doc:

> **Voice stays in the active layer** (browser capture → VAD → local STT → TTS), as the architecture originally specified. No dependency on `experimental.voice.session`.

## 5. Environment

| Dry-run finding | Reality on this machine |
|---|---|
| `bubblewrap` not installed | **OS sandbox is the biggest Phase 2 unknown.** Options in order: (1) install `bubblewrap` and check unprivileged userns; (2) dedicated WSL distro via `wsl --import` (no `/mnt/c`, separate user) if bwrap/userns fails. |
| No `/dev/snd`, no PulseAudio | Confirmed; browser-mic path in the architecture is the only workable path unless we add native audio. |
| Docker not installed | Container sandbox is not a Phase 2 option without installing it. |

## 6. Corrections to the architecture doc (from this Phase 0 run)

1. §15.2 "OpenCode version" → **v2.1.4**, config generation V2, consistent V2 shapes.
2. §16 "feynman 0.5.51" → **0.5.57**, shim now version-agnostic.
3. §15.5 `experimental.voice.session` → **ruled out** for JARVIS; voice lives in the active layer.
4. §5 Gate B — the `permission.reply` path is present and V2-shaped; no V1/V2 mismatch encountered on the live spec.
5. `random password per boot` concern → **already solved** by `~/.config/opencode/service.json` (persistent password).

## 7. What's still open from Phase 0

- [ ] Sandboxed execution: choose and verify bubblewrap/userns, or commit to a dedicated WSL distro (Phase 2 task).
- [ ] `feynman search set <provider>` — web search keys are unconfigured; research driver is degraded until then.
- [ ] Profiles-vs-workspace mechanism: does `v2.1.4` accept a named agent per session with a *different working directory*? (Phase 1 validation, per architecture §15.1.)
