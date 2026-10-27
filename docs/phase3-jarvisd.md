# Phase 3 — jarvisd (the assistant you can actually talk to)

Date: 2026-10-26. Deliverable: the text front-end plus the gated MCP bridge
that makes "JARVIS knows every corner of the system" true without a bypass.

## 1. What now exists

| Piece | File | Job |
|---|---|---|
| Console | src/jarvis/console.py | `user line -> Gate A -> kernel turn -> Gate B -> journal`; `ask` / `pending` / `yes CODE` / `no CODE` / `status` / `quit` |
| CLI entry | src/jarvis/__main__.py | `python3 -m jarvis` (interactive) and `python3 -m jarvis ask "task"` (one-shot) |
| Journal | src/jarvis/journal.py | append-only `.jarvis/journal.jsonl` memory stream |
| Gated MCP bridge | src/jarvis/mcp_gate.py | stdio proxy per MCP server: discovery/reads through, executions judged by Gate B |
| Pending store | src/jarvis/pending_store.py | file-backed cross-process one-time confirmations (bridge process <-> console process) |
| MCP launcher | scripts/jarvis-mcp-gate | what the kernel spawns: `jarvis-mcp-gate SERVER -- <real command>` |

## 2. The human-in-the-loop channel (unchanged law)

- `yes CODE` / `no CODE` — consumed exactly once; unknown codes refused
- Nothing auto-solves, nothing times out into approval
- Ambient speech may **never** mutate the machine
- `always` (standing) grants blocked by the subject rule

## 3. Gated MCP bridge — "JARVIS knows everything, nothing runs unasked"

The kernel (OpenCode 2.1.4) raises permission requests for its own native
tools (shell/edit/webfetch) but **not** for MCP-provided tools. Rather than
excluding MCP (which would blindfold JARVIS), every MCP server is now wired
through `jarvis-mcp-gate`:

```
OpenCode kernel ──spawn──▶ jarvis-mcp-gate (bridge) ──▶ serena
                                      │
                                      ├─ initialize / tools/list   -> pass straight through
                                      │   JARVIS sees every tool; nothing hidden
                                      └─ tools/call:
                                           benign allowlisted call   -> forward, audited
                                           forbidden content          -> refused inline
                                           mutating, not benign       -> parked with a one-time code
                                                                         human: yes CODE / no CODE
```

Kernel config (`~/.config/opencode/opencode.jsonc`) routes serena through
`scripts/jarvis-mcp-gate`. Verified live: `GET /api/mcp` → serena **connected**.

All classification is text-editable in `config/policy.toml`:
- `[[benign.shell]]` — commands that run free (echo, ls, cat, git read-only,
  pytest test runs…)
- `[[forbiddens]]` — never runnable (rm -rf, mkfs, shutdown, raw device damage…)
- `[trust.console] mutating = "confirm"` — typed commands need a code
- `[mcp] mutating_tool_matches` — tool-name substrings that COULD mutate

## 4. Using the console

```
cd /home/sonic/work-jarvis
PYTHONPATH=src python3 -m jarvis
```

```
j> run the shell command: echo hello-from-jarvis
    -> Gate A: typed at console = direct intent -> kernel turn on a JARVIS-owned session
    -> Gate B: echo is allowlisted -> auto-runs, audited
j> touch /tmp/anything
    -> parked: one-time code appears
j> pending
    754e38  (12s ago)  shell: touch /tmp/anything
j> yes 754e38      (or "no 754e38" to refuse)
j> status
j> quit
```

One-shot: `PYTHONPATH=src python3 -m jarvis ask "task"` prints the final
assistant text.

Everything — asks, verdicts, approvals, refusals — lands in
`.jarvis/journal.jsonl` (memory stream) and `.jarvis/gate-log.jsonl`
(audit trail).

## 5. Test status

- tests/test_mcp_gate.py — 14 passed (store semantics, classification,
  bridge→fake-server subprocess roundtrip: discovery pass-through, free reads,
  granted mutations, refused mutations)
- tests/test_gates_unit.py — 16 passed
- tests/test_jarvisd_live.py, tests/test_gateb_live.py,
  tests/test_adapter_live.py — live kernel E2E; passed before the default
  model's free-tier quota was exhausted (kernel error `provider.free-limit`,
  retry ~10h). Code unchanged; re-run when quota resets or a model with
  quota is pinned in the session.

## 6. Still open

- [x] Confirmation presenter polish — DONE 2026-10-27 (see §7: announcer)
- [ ] Memory summarizer (journal -> daily brief -> context weaving)
- [ ] Kernel-update standing job (snapshot -> update -> restart -> compat suite;
      admit on green, rollback on red, ask-human on unclear)
- [ ] OS sandbox (user decision pending)

## 7. Phase 3 tail — live presenter, cancel, lanes, latency (2026-10-27)

Closes the Phase 3 acceptance items from the architecture build order:
foreground/background lanes, cancellation, first-token latency, and the
"codes announced live" polish.

| Piece | File | Job |
|---|---|---|
| Announcer | src/jarvis/announcer.py | background presenter: new confirmations surface as they appear (broker AND MCP-bridge store), resolution noted once, turn-idle narrated once; never decides anything |
| cancel | src/jarvis/console.py | interrupts the current turn through OwnedKernel — subject rule applies, foreign sessions stay untouchable; journalled |
| replay | src/jarvis/console.py | prints the latest turn's assistant text only (turn-scoped event slice, not the whole session transcript) |
| fg/bg lanes | src/jarvis/console.py | plain text = foreground (waits, returns reply); `bg TEXT` = fire-and-forget; `turn-started` journals the lane |
| pause-on-pending | src/jarvis/console.py | foreground wait polls in chunks and yields the console the moment a human decision is pending — a turn waiting on a confirm code can no longer hold the person who must type the code hostage |
| latency metrics | src/jarvis/console.py | `turn-metrics` journal entry per foreground turn: first_token_s (ms-or-s epoch handled, clock-skew guarded) and total_s |

Console surface is now: any-text (fg) / `bg TEXT` / `pending` / `yes CODE` /
`no CODE` / `cancel` / `replay` / `status` / `help` / `quit`.

Design notes:

- ONE SSE event pipe, two subscribers (transcript capture + announcer).
  `events_nonblocking()` builds a fresh pipe per call — attaching twice via
  two pipes would duplicate the stream.
- The announcer is deliberately dumb: announces once, resolution once, and a
  broken source is skipped, never fatal. It never decides.
- Latency: `created` timestamps arrive in epoch ms or s depending on route;
  both handled, nonsense values return None rather than a wrong number.

Bug fixed en route (caught by the new unit tests): `KernelError` never set a
`.message` attribute while console.py read `e.message` — the first kernel
refusal would have raised AttributeError instead of showing the refusal.
`KernelError` now carries `.message` explicitly.

Test status: tests/test_console_unit.py — 16 passed (announcer dedupe/
resolution/broken-source/idle-narration, latency math, hermetic console boot,
cancel paths, pause-on-pending, bg/fg lanes, replay). test_gates_unit.py now
boots OwnedKernel hermetically (password="unit-test") — no service.json
dependency; full hermetic suite 46/46 green. Live E2E suites still pending
model quota (unchanged from §5).
