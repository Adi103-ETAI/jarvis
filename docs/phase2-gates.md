# Phase 2 — the safety spine (gates)

Date: 2026-10-24. Deliverable: Gate A (intent), Gate B (tool), the human
confirmation channel, the subject rule, and the audit log — the pieces that
make JARVIS safe enough to leave running.

## 1. What now exists

| Piece | File | Job |
|---|---|---|
| Policy table | config/policy.toml | declarative rules: benign allowlist, forbidden patterns, trust levels |
| Policy engine | src/jarvis/policy.py | first-match-wins matcher; unknowns default to confirm |
| Gate A (intent) | src/jarvis/gates/intent.py | console/ambient/peer/scheduled -> act / confirm / journal / ignore |
| Gate B (tool) | src/jarvis/gates/tool.py | maps each kernel permission request through policy |
| Doorkeeper | src/jarvis/gates/worker.py | background thread answering JARVIS-owned sessions' permission asks |
| Human channel | src/jarvis/confirm.py | one-time codes; consumed exactly once; expire unanswered |
| Subject rule | src/jarvis/registry.py | only JARVIS-created sessions may be signaled; 'always' grants blocked |
| Audit log | src/jarvis/audit.py | every verdict, JSONL, .jarvis/gate-log.jsonl |

## 2. Posture (as the user directed)

OpenCode's own permission machinery is the wall; JARVIS is the responsible
doorkeeper rather than a second opinion machine:

- Kernel config now runs permission bash/edit/webfetch = **ask** (the
  "human in the loop when messy" posture — standing config as of this phase).
- Gate B auto-runs only allowlisted read-only commands (echo, ls, cat,
  git status...) and parks everything else with a one-time code for the
  human. Nothing times out into approval.
- Overheard (ambient) speech may **never** mutate the machine; peer
  messages are journaled, never acted on.

## 3. Live-verified findings

1. **The live permission queue is the session-scoped endpoint**:
   GET /api/session/{sid}/permission — the global endpoint stays empty in
   2.1.4. Request shape: {id: per_..., action, resources[], save[], source}.
2. **Action names are runtime names**: the shell tool's permission action is
   "shell", not "bash". This explains Phase 1's "session rules are ignored"
   scare — the rules were keyed with the wrong name.
3. **End-to-end proved live** (tests/test_gateb_live.py):
   - echo -> auto-approved once -> executed;
   - touch -> parked with a code -> human approves -> executed; code reuse fails;
   - ambient trust -> automatic reject + audit trail.
4. **KNOWN BYPASS #1 closed** (user direction 2026-10): MCP-provided tools
   raise NO kernel permission requests in 2.1.4, so execution could sneak
   through serena's execute_shell_command even under a shell-deny. The
   gated MCP bridge now carries Gate B for MCP instead:
   - [`src/jarvis/mcp_gate.py`](src/jarvis/mcp_gate.py) — stdio proxy; discovery
     and reads flow through untouched (JARVIS sees every tool); mutating
     calls are judged by the SAME Gate B rules as native tools (benign run
     free, forbidden refused inline, everything else parks with a one-time
     code). Nothing auto-solves.
   - [`scripts/jarvis-mcp-gate`](scripts/jarvis-mcp-gate) — launcher the
     kernel spawns per MCP server.
   - [`config/policy.toml`](config/policy.toml) `[mcp]` — tool-name substrings
     that COULD mutate; content judged by policy (benign allowlist +
     forbiddens + trust), same as native.
   - Kernel config now routes serena through `jarvis-mcp-gate`; verified live
     via GET /api/mcp → serena **connected**.
   Mitigation replaces the old "exclude MCP from JARVIS sessions" idea —
   JARVIS keeps full system knowledge through MCP; only execution is gated.

## 4. Still open (Phase 2 tail)

- [x] Gated MCP bridge live — bypass closed (serena routed through
      jarvis-mcp-gate; connected verified live).
- [ ] OS sandbox for turns (bwrap/userns vs dedicated distro) — user decision
      pending; not blocking today because kernel asks cover interactive
      mutability and forbidden patterns cover the extremes.
- [ ] 'always' patterns (save[]) are refused at runtime by design; if ever
      enabled they belong in policy.toml, verified at startup.

## 5. Test status

- tests/test_gates_unit.py — 16 passed (policy, Gate A, broker, audit, subject rule)
- tests/test_gateb_live.py — 3 passed + 1 documented-bypass assertion
- tests/test_mcp_gate.py — 14 passed (store semantics, classification,
  bridge→fake-server roundtrip: pass-through discovery, free reads, granted
  mutations, refused mutations)
- tests/test_mcp_gate.py integration runs the real bridge as a subprocess —
  the same way the kernel will spawn it.
