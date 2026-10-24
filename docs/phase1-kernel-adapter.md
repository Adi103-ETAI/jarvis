# Phase 1 — kernel adapter

Date: 2026-10-24. Deliverable: `src/jarvis/kernel/adapter.py` — the stable ABI
between the JARVIS active layer and the pinned OpenCode kernel (v2.1.4).
Tested live: `tests/test_adapter_live.py`, **11/11 passed** in ~15 s against the
running service at `127.0.0.1:49374`.

## 1. What the adapter provides

| Method | Locked endpoint | Notes |
|---|---|---|
| `create_session(agent, directory, permissions, model, ...)` | `POST /api/session` | profile ≠ workspace, independent fields |
| `sessions` / `active_sessions` / `delete_session` | `GET/DELETE /api/session…` | |
| `prompt(sid, text, files, agents, skills)` | `POST .../prompt` | returns the *user* message, not the reply |
| `steer(sid, text, delivery=steer/queue)` | `POST .../synthetic` | `queue` variant exposed as `inject_context` |
| `interrupt(sid)` | `POST .../interrupt` | returns whether an execution actually stopped |
| `wait(sid)` | `POST /api/experimental/session/{sid}/wait` | server-side block until idle |
| `permission_requests` / `reply_permission(once\|always\|reject)` | permission endpoints | adapter never *chooses* — Gate B does |
| `fork(sid, before)` / `compact(sid)` / `messages(sid)` | fork / compact / message list | |
| `list_agents` / `list_skills` / `list_models` / `default_model` / `switch_model` | discovery + model ops | |
| `events()` / `events_nonblocking()` | `GET /api/event` (SSE) | background thread + subscriber pipes |

Stdlib only (urllib + threading). One method = one endpoint = one audit line.

## 2. Live-verified kernel behaviors discovered this phase

1. **The kernel-level default model was broken** — config pointed at
   `opencode/ling-3.0-flash-fin-free`, which no longer routes (`provider.no-route`);
   the kernel then silently fell back to `openrouter/moonshotai/kimi-k3.1-pro`,
   a **paid** model, producing `402 provider.quota` on every turn.
   **Fix applied**: default model in `opencode.jsonc` → `opencode/glm-5.3-flash-free`
   (free, tool-capable, 200k ctx). Backup: `opencode.jsonc.bak-model-fix`.
   Lesson encoded into policy: JARVIS sessions **always pin a model explicitly**;
   ambient defaults are not an API we control.
2. **Message shape**: transcripts carry `type` (`user` / `assistant` / `synthetic` /
   `idle`, plus `outcome` and `error` on the idle record) — there is no `role` field.
   Turn success is `idle.outcome == "succeeded"`.
3. **Event shape** (`GET /api/event`): `{id, type, data, location, created}`
   with `data.sessionID`. Execution truth lives here
   (`session.execution.started/failed`, `session.step.*`, `session.inbox.*`).
4. **`GET /api/session/active` is NOT turn-level truth**: it lists durable
   executions and can hold stale zombie entries while a short running turn is not
   listed. Running-ness must come from events.
5. **Foreign sessions are untouchable.** Interrupting/deleting a session entry
   from `active_sessions` can abort a turn belonging to a *different* process
   (this bit us directly: our probe interrupted a session in flight and the abort
   propagated to an unrelated executor). Policy: JARVIS may only signal/drop
   sessions it created itself (tracked in its own session registry).
6. **`session.syntheticdelivery=steer` works mid-turn**; injected synthetic
   messages appear in the transcript with `type == "synthetic"`.
7. **Permission plumbing verified without granting anything**: invalid decision
   → clean client error; valid decision on a bogus handle → structured
   `404 PermissionNotFoundError`. Real decision routing is Gate B (Phase 2).
8. **Session create/delete round-trip clean**: deleted sessions 404 afterwards.
9. Fork works and preserves readFile; per-model pricing/limits live in
   `GET /api/model` (33 free tool-capable models available at test time).

## 3. Safety notes carried into Phase 2

- The adapter deliberately holds **no policy**. Gates make decisions; the adapter
  executes them.
- Test sessions used `permissions: [{bash|edit|write → deny}]`, proving the
  per-session permission array works — Gate B can deny destructive actions
  *at session creation time* before any model call.
- Sensitive ops (`delete_session`, `interrupt`, `reply_permission`) are wrapped
  with docstring warnings; Phase 2 must mediate them behind the tool gate with a
  subject-rules check ("did JARVIS create this session?").

## 4. Open / next

- Phase 2 — Gate A (intent) + Gate B (tool policy) + OS sandbox choice
  (bubblewrap/userns vs dedicated WSL distro). The permission-reply path is
  proven; attaching policy to it is pure local work.
- Zombie sessions `ses_ea13…` and `ses_f980…` remain in `active_sessions`
  (marked running, not one of ours — left untouched on purpose).
