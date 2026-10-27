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

- Confirmation presenter polish (codes announced live as they appear)
- Memory summarizer (journal -> daily brief -> context weaving)
- Kernel-update standing job (snapshot -> update -> restart -> compat suite;
  admit on green, rollback on red, ask-human on unclear)
- OS sandbox (user decision pending)
