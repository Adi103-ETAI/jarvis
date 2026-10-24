# JARVIS — Full Architecture

> **Project root:** `/home/sonic/work-jarvis`
> **Status:** revised architecture proposal; implementation not yet verified
> **Revision date:** 2026-10-22
> **Revision:** v2 — security boundary, provenance, privacy, scheduling, concurrency, and profile/workspace separation
> **Supersedes:** the earlier "own agent loop" plan — the kernel architecture below replaces it.

---

## 0. The whole thing in one paragraph

OpenCode is the **kernel**: it already has every primitive an agent needs (read, write, fetch, run code, reason, manage context, call tools, ask permission). REA, Feynman and other MCP servers are **drivers** plugged into that kernel. What OpenCode lacks is **initiative** — it only ever reacts. So we build an **active layer** beside it: a daemon that listens, watches the clock, watches the filesystem, notices failures, and *chooses* to call the kernel — through a **safety gate** that makes sure ambient noise never becomes an action. The kernel does the thinking; the active layer does the waking up. That is the entire design.

---

## 1. The mental model

```
   LINUX                          JARVIS
   ─────────────────────────      ──────────────────────────────────────────
   kernel            (Linux)     kernel            (opencode)
   init/systemd                   init/systemd      (jarvisd — the active layer)
   drivers          (GPU, NIC)   drivers          (REA, Feynman, MCP servers)
   userland         (bash, X)    userland         (browser UI, TTS, memory)
   distro           (Ubuntu)     "personality"    (agent .md, skills, plugins)
```

**The rule the analogy gives us:** *a kernel never decides to do anything — `init` does.* Everything that makes a Linux box **act** (boot, cron, restart crashed services, watch directories, wait for network events) lives in **user space, beside the kernel**, never inside it.

That single rule decides the whole architecture:

| Question | Answer | Because |
|---|---|---|
| Where does "always on" live? | Active layer | A kernel blocked on a microphone can't serve |
| Where does "schedule a briefing at 8am" live? | Active layer | The kernel has no clock |
| Where does "never act on background chatter" live? | Active layer | The kernel can't know what it heard |
| Where does "write a file / run tests / use REA" live? | Kernel | It already does this better than we would |
| Where does "remember me across months" live? | Active layer | Kernel sessions are project-scoped and not the canonical long-term memory store |

---

## 2. System overview

The original four-layer model remains. The main change is that **safety is two gates, not one**: an intent gate decides whether a trigger may start a task, and a tool gate decides whether each actual operation is allowed when OpenCode attempts it.

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ L4  INTERFACE — Windows / Chrome                                             │
│  authenticated UI · push-to-talk · mic-off · transcripts · task queue        │
│  Origin-checked WebSocket + short-lived connection ticket                    │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               │ audio / UI commands / streamed output
┌──────────────────────────────▼───────────────────────────────────────────────┐
│ L3  ACTIVE LAYER — jarvisd (Python)                                          │
│                                                                              │
│  PERCEPTION          ACTOR / SCHEDULER          MEMORY         PRESENCE      │
│  VAD + local STT     T1–T8 trigger funnel       Markdown vault  local TTS     │
│  wake/PTT channel    foreground/background      SQLite index   barge-in      │
│                      queue + cycle guards       transcripts    notifications│
│                                                                              │
│  ┌──────────────────────┐         ┌──────────────────────────────────────┐  │
│  │ INTENT GATE          │────────▶│ TOOL GATE                            │  │
│  │ source/mode/trust    │         │ actual action/resource/permission    │  │
│  │ capability envelope  │         │ unknown = deny; risky = hold/deny    │  │
│  └──────────────────────┘         └──────────────────┬───────────────────┘  │
└──────────────────────────────────────────────────────┼───────────────────────┘
                                                       │ only approved path
┌──────────────────────────────────────────────────────▼───────────────────────┐
│ L2  KERNEL ADAPTER — src/jarvis/kernel/adapter.py                            │
│  only application boundary that speaks OpenCode HTTP/SSE/API                 │
│  generated client + API lock are version-pinned and tested                   │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ authenticated loopback HTTP / SSE
┌──────────────────────────────────────▼───────────────────────────────────────┐
│ L1  KERNEL — OpenCode                                                       │
│  agent loop · tools · context · LSP · git/worktrees · agent profiles · MCP   │
│  Every action that the policy must inspect is configured to ask or deny.    │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ MCP / configured integrations
┌──────────────────────────────────────▼───────────────────────────────────────┐
│ L0  DRIVERS — Feynman · REA · Serena · web search · future MCP servers       │
│  Driver output is evidence/data, never a source of new authority.            │
└──────────────────────────────────────────────────────────────────────────────┘

OS SECURITY BOUNDARY around OpenCode and executed tools:
  unprivileged process · project-specific filesystem view · no Windows-drive
  access by default · no sudo · command/network restrictions · resource limits
```

### Non-negotiable execution path

1. A trigger becomes a `TaskIntent` carrying source, provenance, trust level, requested profile, project root, capabilities, and budget.
2. The **intent gate** validates the source, mode, schedule authorization, and maximum capability set.
3. OpenCode receives the admitted task using the pinned adapter. Its configured permission system must stop at-risk tools before execution.
4. The **tool gate** evaluates the actual permission request and, where supported by the pinned version, tool/shell execution hooks. A tool not covered by enforceable policy is denied rather than assumed safe.
5. The OS sandbox limits the blast radius if application-level controls fail.
6. Every allow/hold/deny and final outcome is written to the audit log.

The model, retrieved content, MCP output, `AGENTS.md`, and skills may propose actions. They cannot grant permissions or widen the capability envelope.

---

## 3. Layer contracts

### L4 — Interface (Chrome on Windows)

| Piece | Job | Key constraint |
|---|---|---|
| `getUserMedia` + AudioWorklet | Capture 16 kHz mono PCM16 frames | Audio stays in memory; no raw audio is persisted |
| energy gate + VAD | Drop silence and segment speech | Mic-off blocks capture, not merely transcription |
| push-to-talk | Explicit command path and sensitive confirmation | Required fallback when wake detection fails |
| wake badge + mic indicator | Make listening/capture state visible | State persists across restarts; visible and easy to toggle |
| transcript ribbon | Show recognition and whether it is a command | Passive transcript logging is off unless explicitly enabled |
| audio queue | Play local TTS chunks in order | Barge-in clears queue and interrupts current work |
| authenticated WebSocket | Stream audio, text, status, and TTS | Exact Origin allowlist + short-lived token; loopback only |

### L3 — Active layer (`jarvisd`, Python)

| Module | Job | Key constraint |
|---|---|---|
| `perception/vad.py` | Split audio into utterances | No raw audio saved to disk |
| `perception/stt.py` | Local speech-to-text | No audio leaves the machine by default |
| `perception/wake.py` | Find `jarvis` at the start of an utterance | Wake detection alone cannot approve destructive operations |
| `actor/triggers.py` | Map T1–T8 to `TaskIntent` | Preserve source and provenance; no trust flattening |
| `actor/router.py` | Pick profile/model from intent | Deterministic rules first; any classifier is advisory |
| `actor/queue.py` | Foreground and bounded background lanes | Cancellation, project locks, dedupe, priority, and budgets |
| `policy/intent_gate.py` | Validate trigger, mode, and capability envelope | Content-derived events start read-only, restricted tasks only |
| `policy/tool_gate.py` | Evaluate actual kernel permission requests/actions | Unknown actions fail closed; never trust prompt text as policy |
| `policy/rules.py` | Central policy definitions | No model-editable permission widening |
| `policy/sandbox.py` | Launch/verify OS-level confinement | `allowed_roots` alone is not enforcement |
| `memory/vault.py` | Curated durable Markdown memory | User can inspect, correct, and delete entries |
| `memory/store.py` | SQLite sessions, transcripts, audit, indexes | No raw audio blobs or audio file paths |
| `memory/recall.py` | FTS5 + optional local embeddings | Retrieved material is labelled untrusted context |
| `presence/tts.py` | Sentence-chunked speech | Local Piper is default; cloud TTS requires opt-in |
| `presence/notifier.py` | Desktop/UI result notifications | DND suppresses interruptions, not authorized work |
| `actor/budget.py` | Per-task token/time/cost limits | Required before proactive triggers are expanded |

### L2 — Kernel adapter

**Canonical adapter:** `src/jarvis/kernel/adapter.py`. It is the only application-facing module that knows OpenCode endpoint paths, request shapes, and event schemas. The generated client and locked API schema live beneath `src/jarvis/kernel/` and `docs/` respectively; no active-layer module may construct OpenCode URLs directly.

```python
class KernelAdapter:
    async def create_session(self, project_root, profile, ...) -> Session: ...
    async def prompt(self, session, text, delivery="queue") -> None: ...
    async def steer(self, session, text) -> None: ...
    async def interrupt(self, session) -> None: ...
    async def inject_context(self, session, text) -> None: ...
    async def reply_permission(self, session, request_id, decision="once") -> None: ...
    async def fork(self, session) -> Session: ...
    async def compact(self, session) -> None: ...
    async def events(self): ...
    async def list_agents(self): ...
    async def list_skills(self): ...
```

The method names are JARVIS's stable interface, **not a claim that every method maps to a stable or identical endpoint in every OpenCode release**. Each call must be implemented and tested against the API specification produced by the exact pinned binary. If a capability is absent, the adapter reports it as unsupported; other layers must not guess an endpoint.

### OpenCode contract and version drift

OpenCode's current documentation distinguishes V1 and V2 API/configuration contracts. For example, the documented permission field and action names differ between V1 (`permission`, `bash`) and V2 (`permissions`, `shell`). Do not copy a V2 example into the originally pinned `1.17.13` setup without verification. Official docs: [Server](https://docs.opencode.ai/docs/server/), [V1 permissions](https://docs.opencode.ai/docs/permissions/), [V2 permissions](https://dev.opencode.ai/v2/docs/permissions/).

Phase 0 must establish: exact binary version; actual OpenAPI/spec endpoint; supported session, prompt, event, permission, agent/profile, and compaction operations; actual config schema; and which operations truly pause for permission approval. Pin and test that exact contract before implementing the adapter.

### L1 — Kernel (OpenCode)

OpenCode owns the agent loop, tools, context window/compaction, LSP, Git/worktrees, skills, agents, plugins, MCP client, and permission requests. JARVIS owns initiative, trigger provenance, durable memory, policy, OS confinement, queueing, and user-facing presence.

### Profiles (“hats”) are separate from project workspaces

A **profile** selects behavior and capabilities; a **workspace** is the actual repository or directory being worked on. They are separate concepts and must not be represented by the same working directory.

```text
profiles/
  general.md      everyday interaction and concise spoken answers
  code.md         engineering conventions and test expectations
  research.md     evidence quality, citations, source comparison
  re.md            reverse-engineering workflow and evidence locations

workspace = /path/to/real/project  # e.g. an actual repository, not profiles/code
```

The JARVIS profile manifest maps a profile to its instructions, desired agent, permitted driver set, and policy capability ceiling. The adapter opens the session in the **real project directory** and selects/applies the profile through the mechanism verified for the pinned OpenCode version. Where a repository has its own `AGENTS.md`, LSP configuration, or local rules, those remain project context beneath JARVIS's global safety policy.

**Implementation detail still requiring Phase 0 validation:** can the pinned version select named agents and scope MCP/tool availability per session while keeping the target project as the working directory? If yes, use that facility. If not, generate a controlled profile config overlay or isolated per-profile runtime, while preserving the real project as the working directory. Never “solve” the profile problem by running the session from `hats/code` instead of the user's repository.

### L0 — Drivers

| Driver | Kind | Gives the kernel | Trust treatment |
|---|---|---|---|
| **REA** | MCP | Static analysis, strings, xrefs, JS/ASAR/Electron, .NET, HAR; native support later | Inputs and outputs are untrusted evidence |
| **Feynman** | MCP or CLI shim | Literature review, paper search/QA, comparison, research | Retrieved pages and tool output are untrusted evidence |
| **Serena** | MCP | Symbol-level code intelligence, references, project memories | Project-derived output is untrusted data |
| **web search/fetch** | Built-in or MCP | Public web retrieval | No authority from page instructions |
| future drivers | MCP | Explicitly registered capability | Disabled until policy and permission tests exist |

Drivers provide capabilities; they do not get to decide when to act or to widen policy.

---

## 4. The Activeness Engine

The active layer is what makes JARVIS proactive. Every source enters the same typed funnel, but **same shape does not mean same trust**.

```text
T1 clock ─────────────┐
T2 voice (wake/PTT) ──┤
T3 authenticated text ┤
T4 filesystem ────────┤
T5 Git / CI ──────────┤──▶ Trigger engine ─▶ TaskIntent + provenance
T6 system health ────┤                      │
T7 webhook ──────────┤                      ▼
T8 kernel event ─────┘                 Intent gate
                                             │
                          ┌──────────────────┼─────────────────┐
                          ▼                  ▼                 ▼
                    foreground lane    background lane     deny/log
                          │                  │
                          └───────▶ Kernel adapter ◀───────────┘
                                          │
                                     tool gate + OS sandbox
```

### The 8 triggers

| ID | Trigger | Source of truth | Example | Default authority |
|---|---|---|---|---|
| T1 | **Time** | User-approved routine in DB | Weekday 08:00 briefing | Only that routine's predeclared capabilities |
| T2 | **Voice** | Wake phrase at utterance start or PTT | “Jarvis, what is on my plate?” | Interactive-user policy; destructive approval still requires UI/PTT |
| T3 | **Typed** | Authenticated browser UI/CLI | Explicit typed command | Interactive-user policy |
| T4 | **Filesystem** | Watch on user-selected directories | A new PDF arrived | Content-derived, read-only by default; no network or writes |
| T5 | **Git / CI** | Verified hook, `gh` poll, or authenticated CI webhook | Build failed on main | Event data is untrusted; actions limited to configured workflow |
| T6 | **System** | Explicitly supported local OS sensors | Disk low or network down | Notify/read diagnostics; changes need separate policy |
| T7 | **Webhook** | Local authenticated endpoint | Dataset published | Payload is untrusted; no action instructions accepted from it |
| T8 | **Kernel event** | SSE/event stream | Tool failure or compaction | Diagnostics only; cooldown/cycle guards required |

### `TaskIntent` contract

Every trigger emits the same schema, with source-specific fields retained:

```text
TaskIntent {
  id, created_at,
  source: clock|voice|typed|filesystem|git_ci|system|webhook|kernel_event,
  provenance: user_explicit|user_routine|system_signal|content_derived,
  trust_level: interactive|preauthorized_routine|restricted_event|untrusted_content,
  text, profile, project_root,
  capabilities: {read, write, shell, network, mcp_tools},
  risk_hint,                         # advisory only; never authority
  parent_event_id, dedupe_key,
  deadline, token_budget, cost_budget,
  interaction_mode: foreground|background
}
```

The intent gate intersects requested capabilities with a **server-side maximum allowed for that source and profile**. An intent cannot add capabilities by claiming a different `risk_hint`, prompt field, or profile name. Content-derived inputs (documents, web pages, repository files, webhook payloads, driver output and kernel event text) are data, not instructions.

### Loop and budget controls are part of the initial engine

- Deduplicate by stable event fingerprint and parent-event lineage.
- Apply per-source cooldowns and rate limits; coalesce repeated events.
- Never let an event produced by JARVIS directly re-trigger itself without a distinct, explicitly allowed root cause.
- A failed task may emit a notification, but the notification must not automatically create an unlimited retry task.
- Enforce maximum concurrent jobs, per-task wall-time, model/token/cost budgets, and a daily budget before enabling proactive work.
- Every background job has a cancellation handle, deadline, and result-notification policy.

---

## 5. The Safety Gate (the security boundary)

The gate is split into two independent checks. **The first controls what may start; the second controls what actually runs.** A prompt-level check alone is not a security boundary.

### Gate A — intent admission

```text
TaskIntent arrives
  ├─ authenticate/validate source and schema
  ├─ check mic/PTT/wake/UI state where relevant
  ├─ check sleep, mute, Do Not Disturb, schedule status
  ├─ classify provenance and trust level
  ├─ intersect requested capabilities with source/profile ceiling
  ├─ apply dedupe, cooldown, concurrency, and budget rules
  └─ ALLOW within envelope / HOLD for explicit user input / DENY + audit
```

Mode semantics:

| State | Meaning |
|---|---|
| **Mic off** | Stop capture and block voice triggers. It does not stop authorized typed or scheduled work. State survives restart. |
| **Do Not Disturb** | Continue work permitted by policy, but suppress speech and noncritical interruptions; queue notifications. |
| **Asleep/disabled** | No new proactive work except explicitly configured critical system notifications. |
| **User speaking / TTS playing** | Hold speech output or accept barge-in; do not start a competing spoken response. |
| **Scheduled routine** | Run only the preapproved routine, with its configured profile, roots, capabilities, and budget. |
| **Content-derived event** | Restricted read-only inspection by default; never treat embedded instructions as user approval. |

### Gate B — operation/tool authorization

The model's initial prompt is not an action plan. Gate B must inspect the **actual requested operation and resource** at the kernel permission boundary, before execution. Depending on the pinned OpenCode version, use native permission requests/events and supported permission or tool-execution hooks. These mechanisms must be verified in Phase 0; if a tool can execute without reaching an enforceable hook/permission boundary, it must be disabled or isolated until an enforceable control is available.

| Action class | Default decision | Rules |
|---|---|---|
| Read/list/search inside approved project | Allow only after a narrow tested allowlist is established | Project root and symlinks must resolve inside the sandbox |
| File write/edit/patch | Ask or allow only in a narrowly scoped approved workspace | No writes for content-derived tasks by default |
| Shell/process execution | Ask or deny by default; narrow command allowlist only | Compound/unknown commands are held or denied; no `sudo` |
| Network/fetch/API | Deny by default for restricted tasks; allow narrowly for approved tasks | Host policy is supplementary; OS egress controls are required where feasible |
| MCP/driver tool | Ask or deny until individually reviewed | Unknown tools are denied; output never grants more authority |
| Install/package change | Hold for explicit confirmation | Treat scripts and post-install hooks as executable code |
| Destructive action, force push, delete | Hold; explicit UI click or fresh PTT confirmation | Approval bound to exact target/action and valid once only |
| Credential/secret exfiltration | Deny | A prompt or confirmation cannot override hard-deny policy |
| Unknown/unclassifiable action | Deny | Fail closed |

### OpenCode permissions are part of the control, not a substitute for it

For every profile/hat, explicitly configure the pinned OpenCode version so all actions JARVIS needs to mediate request approval (`ask`) or are blocked (`deny`). Do **not** assume defaults are restrictive: current official docs say default behavior permits tools unless permissions are configured. Check each actual tool, MCP action, shell action, external-directory operation, subagent/skill, and network capability used by the project.

No `--auto` / auto-approval mode is permitted for the JARVIS kernel process. JARVIS should reply `once` to an approved pending permission; automated code must never choose `always`. The exact field names and action names depend on the pinned OpenCode API generation. See the official [V1 permissions guide](https://docs.opencode.ai/docs/permissions/) and [V2 permissions guide](https://dev.opencode.ai/v2/docs/permissions/).

### Shell and filesystem containment

Regex-style command classification is not enough. Examples such as `ls; curl ... | sh`, `python -c`, `find -delete`, shell aliases, command substitutions, and nested interpreters defeat simplistic pattern checks. Use this order:

1. Default-deny shell. Permit only simple, explicitly allowlisted commands or require approval.
2. Treat compound commands and unknown shell syntax as HOLD/DENY; do not attempt to make a regex a full shell parser.
3. Run OpenCode/tool processes as an unprivileged account with no `sudo` capability.
4. Enforce filesystem containment at OS level: project-specific sandbox/mount namespace or dedicated user/distro; verify symlinks and canonical paths. `allowed_roots` is a policy input, not OS enforcement.
5. In WSL, do not expose Windows drives (such as `/mnt/c`) to the JARVIS execution sandbox by default. Use a dedicated WSL configuration/distro or restricted mount namespace; verify the actual mount view from inside the process.
6. Restrict network egress for the sandbox to what the task needs. An application-level host allowlist alone is not sufficient against every child process.

### Explicit confirmation

A confirmation record contains the task ID, exact action, canonical target, relevant arguments/digest, requesting source, expiry, and one-time nonce. The UI shows what will happen. Confirmation expires quickly and authorizes only that exact operation. **A spoken “yes” detected passively is never sufficient for a destructive action.** Require a deliberate UI click or fresh PTT interaction while the confirmation is active. Replayed audio, video/call audio, or unrelated speech must not authorize it.

### WebSocket and local server security

- Bind `jarvisd` and OpenCode to loopback; do not expose either on the LAN by default.
- Require an exact Origin allowlist and reject missing or unexpected browser origins.
- Authenticate WebSocket establishment with a short-lived, single-use connection ticket; never hard-code a reusable secret in static JavaScript.
- Protect ticket issuance with the authenticated UI/session and same-origin/CSRF protections; rotate/revoke tickets on logout or restart.
- Validate message schema, size, sequence number, and rate for every WebSocket message.
- Treat `text`, `mute`, `ptt`, and confirmation messages as privileged commands, not mere UI signals.

### Invariants (each gets a dedicated automated test)

1. Passive/ambient speech cannot create an executable kernel task.
2. Wake phrase must occur at the start of the utterance; wake phrase alone cannot approve destructive work.
3. Replayed audio and room audio cannot confirm destructive actions.
4. No `--auto`/auto-approval mode is passed to the kernel.
5. Only the intent gate can create admitted tasks; only the tool gate can approve gated operations; no module can bypass either adapter path.
6. Every profile's actual OpenCode configuration is tested to ensure protected actions stop for approval or are denied.
7. Unknown tools, command forms, permission actions, or resource scopes fail closed.
8. OS sandbox cannot read/write outside its allowed workspace or access Windows drives by default.
9. Every decision and result is audited without storing secrets or full command output.
10. Mic-off persists across restarts and blocks capture until explicitly re-enabled.
11. Do Not Disturb suppresses interruptions but does not silently widen or narrow task permissions.
12. A confirmation cannot be replayed or applied to a changed target.
13. Content-derived inputs cannot widen trust, tool permissions, or network access.
14. T8 and other event handlers have cycle detection, cooldowns, and budget ceilings.

---

## 6. Memory architecture — identity, durable recall, and session state

JARVIS has three persistence mechanisms with distinct canonical roles:

```text
┌──────────────────────────────────────────────────────────────────────┐
│ IDENTITY / POLICY                                                    │
│ profiles/*.md + policy/rules.py + versioned configuration            │
│ The model cannot edit policy or grant itself permissions.            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ read-only instructions/context
┌──────────────────────────────▼───────────────────────────────────────┐
│ DURABLE MEMORY VAULT — ~/.jarvis/memory/                             │
│ Human-readable Markdown is canonical for saved facts and decisions.  │
│ Includes provenance, timestamp, confidence, sensitivity, and status. │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ indexed by
┌──────────────────────────────▼───────────────────────────────────────┐
│ SQLITE — ~/.jarvis/jarvis.db                                         │
│ Sessions, tasks, audit, opt-in transcripts, FTS5 index, optional     │
│ local embedding index, routine state, memory-file index.             │
│ No raw audio, audio_path, or permanent transcript archive by default.│
└──────────────────────────────┬───────────────────────────────────────┘
                               │ relevant context injected into
┌──────────────────────────────▼───────────────────────────────────────┐
│ OPENCODE SESSION / TURN                                              │
│ Current conversation and task context; subject to compaction.        │
└──────────────────────────────────────────────────────────────────────┘
```

### Memory rules

- **Markdown vault is canonical for durable saved memory.** SQLite is the index/runtime store, not the only copy of long-lived user facts.
- Record provenance (`explicit`, `inferred`, or `tool-derived`), creation/update time, confidence, sensitivity, and last confirmation for each durable entry.
- Explicit “remember this” requests can be saved. Automatically inferred facts go into a reviewable candidate state unless the user has explicitly enabled automatic memory creation.
- The user can list, inspect, correct, export, and delete memories individually or wipe all JARVIS data using one documented command.
- Passive capture and passive transcript logging are **off by default**. If the user opts into passive transcript retention, the default retention is 14 days and the user can change it. Raw audio is not persisted.
- JARVIS-owned transcript copies/indexes live in SQLite, subject to retention policy. Never store audio files or an `audio_path` in the JARVIS database. OpenCode may persist its own session history separately; Phase 0 must identify that storage and retention behavior, and purge/privacy commands must not claim to delete it unless they actually do.
- Embeddings are optional and should be generated locally by default. If remote embedding is ever introduced, it requires explicit configuration and disclosure.
- Retrieved memory, transcripts, repository content, web pages, and driver output are context/evidence, not executable policy or instructions.

### Context bridges

1. **Recall bridge:** query FTS5 and, if enabled, local embeddings; inject only relevant, provenance-labelled excerpts into the active session. Retrieval does not create a new model turn where the pinned API supports write-only context injection.
2. **Compaction bridge:** a version-compatible plugin adds a small task-state summary—current objective, decisions, open threads, relevant memory pointers—when context is compacted. Plugin hooks are version-specific; verify the hook and behavior in Phase 0.
3. **Recovery bridge:** persist task state outside the model context so a restarted daemon can report what was in progress and ask whether to resume safely.

Do not rely on a compaction plugin as the only durable state mechanism. It is a context-restoration convenience; the vault and database remain the source of truth.

---

## 7. Voice pipeline (data flow)

Voice is deliberately introduced after the text path, safety gate, and a minimal proactive routine work reliably.

```text
Chrome mic
   │  16 kHz mono PCM16 frames (memory only)
   ▼
energy gate → VAD → short ring buffer → local STT
                                      │
                                      ▼
                         wake phrase at utterance start?
                             │                 │
                           yes                 no
                             │                 └─▶ no task; no passive transcript saved
                             ▼
                      source=voice command
                             │
                    intent gate + provenance
                             │
                    foreground task / steer
                             │
                    tool gate + OS sandbox
                             ▼
                           OpenCode
                             │ text deltas / task events
                             ▼
                    transcript ribbon + local TTS
                             │
            PTT / “stop” ────┴──▶ clear TTS queue + interrupt
```

### Voice constraints

- VAD, wake detection, and speech-to-text run locally by default. There is no raw audio recording feature in the initial implementation.
- `jarvis` is the only initial wake phrase and must occur at the start of an utterance. Push-to-talk remains the reliable explicit path.
- A wake word distinguishes a likely request from ambient speech; it does not authenticate the speaker and cannot authorize destructive operations.
- Passive transcription/logging stays off unless the user opts in. If the mic is off, capture stops completely.
- On speech while a foreground task is running: “stop” interrupts; a new explicit instruction steers the current task by default when safe; an explicit “after this” queues the request. The user can choose the behavior in settings. Ambiguous speech does not create a new independent task.
- Local Piper is the default TTS target. A cloud TTS provider may be enabled only with a clear opt-in because response text may contain private or recalled information.
- Measure time-to-first-token and time-to-first-audio during the text/voice phases. Do not assume a model route is fast enough based on model names alone.
- Check the pinned OpenCode build's voice-session feature briefly in Phase 0, but do not make the architecture depend on it unless a working prototype passes the same policy and privacy tests.

---

## 8. Sequence diagrams

### 8.1 Explicit text command

```text
User/UI       jarvisd         Intent gate       Adapter/OpenCode      Tool gate
  │              │                 │                   │                  │
  │ typed task ─▶│                 │                   │                  │
  │              │ validate source │                   │                  │
  │              ├────────────────▶│                   │                  │
  │              │                 │ ALLOW envelope    │                  │
  │              │◀────────────────┤                   │                  │
  │              │ prompt ─────────────────────────────▶│                  │
  │              │                 │      tool request ──────────────────▶│
  │              │                 │                   │                  │
  │              │                 │                   │     allow/hold/deny
  │              │                 │                   │◀─────────────────┤
  │              │◀──────────── streamed result / events ─────────────────│
  │◀─────────────┤                 │                   │                  │
```

### 8.2 Preapproved 08:00 briefing

```text
Scheduler       jarvisd         Intent gate       Foreground/background lane
   │               │                 │                        │
   │ fires routine │                 │                        │
   ├──────────────▶│ create intent with routine ID/budget     │
   │               ├────────────────▶│                        │
   │               │                 │ validate routine still enabled,
   │               │                 │ source, capability ceiling, budget
   │               │◀────────────────┤                        │
   │               │────────────────────────────────────────▶│
   │               │                 │             kernel through adapter
   │               │◀────────────────────────────────────────│
   │               │ notify/store result; apply DND policy   │
```

A routine can execute only its saved prompt/profile/capabilities. It cannot use new instructions extracted from a web page or file to increase its permissions.

### 8.3 Destructive operation requires explicit approval

```text
OpenCode      Tool gate       Browser UI / PTT       Tool gate       OpenCode
   │              │                  │                  │               │
   │ permission ─▶│                  │                  │               │
   │              │ evaluate exact action/path/args      │               │
   │              ├─────────────────▶│ show target + ask│               │
   │              │                  │ user confirms     │               │
   │              │◀─────────────────┤ signed/nonce-bound approval       │
   │              │ verify expiry, identity, exact digest, one-time use  │
   │              ├────────────────────────────────────▶│ reply once ───▶│
   │              │                  │                  │               │
```

No passive “yes” is accepted. A confirmation for one target or command cannot authorize a changed target or a second operation.

### 8.4 Barge-in and concurrent work

```text
TTS playing / background research running
   │
   ├─ user says “stop” via accepted voice/PTT path
   │      ├─ clear browser audio queue
   │      ├─ interrupt foreground kernel session
   │      └─ leave independently authorized background job running unless user cancels it
   │
   └─ user asks another question
          ├─ route to foreground lane
          ├─ steer current task / queue / create new session per configured rule
          └─ use project locks to prevent conflicting concurrent writes
```

---

## 9. Protocol specifications

### 9.1 Browser ↔ jarvisd (WebSocket)

WebSocket connections are privileged local control channels and must be authenticated. The protocol below is a logical message contract, not a substitute for authentication or policy checks.

**Connection requirements**

- Bind to loopback only.
- Check `Origin` against an exact configured allowlist; reject unexpected or missing browser origins.
- Require a short-lived, single-use connection ticket obtained through the authenticated UI path; do not ship a reusable token in static JavaScript.
- Protect ticket issuance with same-origin/CSRF controls and revoke on logout/restart.
- Apply message size limits, schema validation, sequence checking, and rate limits.

**Client → server**

| Message | Payload | Meaning |
|---|---|---|
| `hello` | `{ticket, clientNonce, protocolVersion}` | Authenticated handshake; ticket is consumed once |
| `audio` | `{pcm, seq}` | 20 ms PCM16 frame; accepted only while mic is enabled |
| `ptt` | `{down: bool}` | Push-to-talk state |
| `mute` | `{on: bool}` | Mic state change; cannot override persisted policy without authorized UI action |
| `text` | `{text, requestId}` | Typed command from authenticated UI; passes intent gate |
| `confirm` | `{approvalId, nonce, decision}` | Explicit UI/PTT confirmation tied to one action |
| `ack` | `{id}` | Flow control |
| `cancel` | `{taskId}` | Cancel task after ownership and task-state checks |

**Server → client**

| Message | Payload | Meaning |
|---|---|---|
| `ready` | `{sampleRate, frameMs, protocolVersion}` | Handshake acknowledgment |
| `vad` | `{speech: bool}` | Local speech activity state |
| `transcript` | `{text, ts, channel, persisted}` | What was recognized and whether it is stored |
| `agent_delta` | `{text}` | Streamed response text |
| `tts` | `{audio, seq, codec}` | Speech output chunk |
| `tts_stop` | `{}` | Clear audio queue on barge-in |
| `ask` | `{approvalId, actionSummary, target, expiresAt, allowedChannels}` | Explicit confirmation request |
| `task` | `{id, state, title, lane}` | Queue/task state |
| `error` | `{code, message, retryable}` | Non-fatal protocol error |

The server does not treat client-sent `channel`, `trust_level`, `source`, or “confirmed” flags as authoritative. It derives them from authenticated connection state and the interaction that actually occurred.

### 9.2 jarvisd ↔ kernel (HTTP + SSE)

The exact endpoint paths, auth scheme, payloads, and event names are produced from the pinned server's actual OpenAPI specification. The adapter normalizes changing API details into internal event types such as:

| Internal event | Used for |
|---|---|
| `text_delta` / `turn_started` / `turn_ended` | Live captions and TTS streaming |
| `permission_requested` / `permission_resolved` | Tool gate and audit |
| `tool_started` / `tool_finished` / `tool_failed` | Activity panel and audit |
| `task_started` / `task_finished` | Queue state and notifications |
| `context_compacted` | Memory bridge |
| `driver_state_changed` | Show unavailable integrations instead of hallucinating |
| `usage_updated` | Budget accounting |
| `server_connected` / `server_disconnected` | Reconnect and recovery |

These normalized event names are JARVIS names. Do not assume literal OpenCode event names stay stable across API generations. Phase 0 maps each one to an actual event supported by the pinned build and tests its ordering/reconnect behavior.

---

## 10. Data model (`~/.jarvis/jarvis.db`)

SQLite is the runtime store and search index. The Markdown vault is canonical for durable saved facts.

```text
utterances
  id, ts, channel(command|passive_opt_in), text, confidence,
  wake_hit, session_id, profile, retention_expires_at
  # no audio_path, no raw audio blobs; passive rows exist only when enabled

sessions
  id, profile, project_root, started_at, ended_at, status
  # no engine column: OpenCode is the kernel for this architecture

tasks
  id, parent_event_id, source, provenance, trust_level, profile,
  project_root, lane, state, created_at, started_at, ended_at,
  deadline, token_budget, cost_budget, dedupe_key

memory_index
  id, markdown_path, heading, summary, source, confidence,
  sensitivity, created_at, updated_at, last_confirmed_at, embedding_blob?
  # metadata/index only; Markdown file is canonical

tool_audit
  id, ts, task_id, operation, resource_digest, risk, decision,
  reason, confirmation_id?, args_digest, result_digest, duration_ms,
  outcome, policy_version
  # store digests/metadata, not secrets or full command output

routines
  id, name, cron, prompt, profile, capabilities_json, roots_json,
  token_budget, cost_budget, enabled, last_run, cooldown_until

usage
  id, ts, task_id, session_id, model, input_tokens, output_tokens,
  cost_estimate, currency

settings
  key, value
  # mic_enabled, passive_transcript_logging, wake_words, do_not_disturb,
  # retention_days, budgets, allowed_origins, etc.

search_fts
  FTS5 over opted-in utterances and memory index fields

event_dedupe
  dedupe_key, source, parent_event_id, first_seen_at, last_seen_at,
  count, cooldown_until
```

### Data lifecycle and privacy

- Passive capture/logging is off until deliberately enabled in settings. No raw audio is recorded or retained by the initial version.
- Default transcript retention, if enabled, is 14 days. A purge job deletes expired records and their search-index entries.
- Durable memory is never silently treated as immutable: the user can update/delete facts; generated candidates are reviewable.
- `tool_audit` retains operational evidence without saving full command output, tokens, credentials, or secret values. Use keyed/redacted digests where needed.
- Provide `jarvis purge --transcripts`, `jarvis memory list/edit/delete`, and an explicit full-data wipe command with clear confirmation.
- Retention and consent changes themselves are audited without recording private transcript content.

---

## 11. Configuration

The following is the **JARVIS-side conceptual configuration**. It is not an OpenCode configuration file; the adapter/Phase 0 process must generate or validate OpenCode-specific settings for the pinned API generation.

```toml
# config/jarvis.toml

[kernel]
command = "opencode"
args = ["serve", "--port", "4096", "--hostname", "127.0.0.1"]
version = "1.17.13"                 # original proposal; re-verify before lock
password_env = "OPENCODE_SERVER_PASSWORD"
api_spec_lock = "docs/opencode-api.lock.json"
auto_approval_mode = false

[server]
host = "127.0.0.1"
port = 8787
allowed_origins = ["http://127.0.0.1:8787", "http://localhost:8787"]
connection_ticket_ttl_seconds = 60
connection_ticket_single_use = true

[voice]
enabled = false                     # enable only after text path is secure
sample_rate = 16000
frame_ms = 20
wake_words = ["jarvis"]             # must occur at start of utterance
model = "base.en"                   # verify performance/language scope before final choice
stt_engine = "faster-whisper"
vad = "webrtcvad"
aggressiveness = 2
hangover_ms = 400
pre_roll_ms = 300
max_utterance_seconds = 15
persist_raw_audio = false
passive_transcript_logging = false
tts_engine = "piper"                # local default; remote TTS is explicit opt-in

[policy]
unknown_tool = "deny"
unknown_command = "deny"
require_gate_for = ["edit", "shell", "external_directory", "network", "mcp"]
require_human_for = ["destructive", "credential_access", "external_publish", "installation"]
confirmation_channels = ["ui_click", "push_to_talk"]
confirmation_ttl_seconds = 15
no_auto_approval = true

[policy.sandbox]
mode = "required"
allow_sudo = false
allow_windows_drives = false
network_default = "deny"
roots = ["~/work-jarvis", "~/Documents/jarvis"] # illustrative; add project roots explicitly

[execution]
foreground_concurrency = 1
background_concurrency = 2
per_task_timeout_seconds = 1800
max_retries_per_root_event = 1
trigger_cooldown_seconds = 60
daily_cost_budget = 0                 # configure an explicit nonzero budget before model work

[memory]
db = "~/.jarvis/jarvis.db"
vault = "~/.jarvis/memory"
retention_days = 14
passive_logging_default = false
embeddings = false                    # optional; enable local embeddings after review

[profiles]
general = "profiles/general.md"
code = "profiles/code.md"
research = "profiles/research.md"
re = "profiles/re.md"

[drivers]
rea_enabled = false                   # enable after tool policy tests
feynman_mode = "unconfigured"         # decide MCP vs CLI in Phase 0
serena_enabled = false
```

### OpenCode policy generation rules

- The exact config syntax is version-specific. V1 and V2 use different configuration keys and action names; generate a config compatible with the locked build rather than copy-pasting current V2 examples into V1.
- Every profile receives an explicit permission policy. Any action that must pass through JARVIS approval must be configured to ask, and high-risk actions may be denied outright. Verify this by issuing real test prompts and observing whether execution pauses before the operation.
- No execution path starts OpenCode with `--auto` or another blanket auto-approval setting.
- The JARVIS gate and OS sandbox remain required even after OpenCode permission configuration passes.

---

## 12. File layout

```text
/home/sonic/work-jarvis/
├── pyproject.toml
├── README.md
├── config/jarvis.toml
├── src/jarvis/
│   ├── cli.py                    # chat | serve | doctor | purge | routines | memory
│   ├── config.py
│   ├── active/
│   │   ├── daemon.py
│   │   ├── perception/
│   │   │   ├── vad.py
│   │   │   ├── stt.py
│   │   │   ├── wake.py
│   │   │   └── audio.py           # in-memory ring buffer only
│   │   ├── actor/
│   │   │   ├── triggers.py        # T1..T8 + provenance
│   │   │   ├── router.py          # profile and model routing
│   │   │   ├── queue.py           # foreground/background lanes + locks
│   │   │   ├── budget.py          # cost/time/concurrency budgets
│   │   │   └── dedupe.py          # cooldowns/cycle detection
│   │   ├── policy/
│   │   │   ├── intent_gate.py     # source/mode/trust/capabilities
│   │   │   ├── tool_gate.py       # real operation permissions
│   │   │   ├── rules.py           # immutable server-side policy
│   │   │   ├── sandbox.py         # OS confinement verification
│   │   │   └── confirmations.py   # one-time UI/PTT approvals
│   │   ├── memory/
│   │   │   ├── vault.py           # canonical Markdown memory
│   │   │   ├── store.py           # SQLite operational store
│   │   │   └── recall.py          # FTS5 + optional local vectors
│   │   ├── presence/
│   │   │   ├── tts.py             # Piper default
│   │   │   └── notifier.py
│   │   └── server/
│   │       ├── app.py
│   │       ├── auth.py            # Origin check + one-time tickets
│   │       ├── ws.py
│   │       └── routes.py
│   ├── kernel/
│   │   ├── adapter.py             # sole application-facing OpenCode boundary
│   │   ├── events.py              # normalize API-specific event names
│   │   ├── permissions.py         # permission reply semantics
│   │   └── generated_client.py    # generated from exact locked spec
│   └── web/
│       ├── index.html
│       ├── app.js
│       ├── audio-worklet.js
│       └── style.css
├── profiles/
│   ├── general.md
│   ├── code.md
│   ├── research.md
│   └── re.md
├── plugins/
│   └── jarvis-memory.ts           # implemented for pinned plugin API only
├── sandbox/
│   ├── README.md                  # WSL/mount/container confinement procedure
│   └── policy.example
├── tests/
│   ├── test_intent_gate.py
│   ├── test_tool_gate.py
│   ├── test_websocket_auth.py
│   ├── test_provenance.py
│   ├── test_trigger_loops.py
│   ├── test_memory_retention.py
│   └── integration/
├── scripts/
│   └── update_opencode_api.py
└── docs/
    ├── ARCHITECTURE.md            # this file when installed in repository
    ├── opencode-api.lock.json     # one canonical name for locked API schema
    └── decisions/                 # ADRs, including profile/workspace decision
```

The external durable memory vault lives under `~/.jarvis/memory/`, and the operational database at `~/.jarvis/jarvis.db`. Profile definitions do not double as workspaces.

---

## 13. Build order

Each phase ends with a usable deliverable and a gate that must pass. Security and feedback-loop protection are implemented before voice and broad proactivity.

| # | Phase | Deliverable | Acceptance gate |
|---|---|---|---|
| **0** | **Kernel substrate and compatibility proof** | Exact OpenCode version, authenticated loopback server, actual API schema locked as `docs/opencode-api.lock.json`; verify permissions, profile selection, compaction hook, and voice-session feature briefly | `jarvis doctor` reports exact supported capabilities; test every API/config claim against the pinned binary; no version/schema ambiguity |
| **1** | **Kernel adapter** | `src/jarvis/kernel/adapter.py` plus generated client from locked spec | Integration tests: create session → prompt → stream → steer → interrupt → request/reply permission; unsupported operations fail explicitly |
| **2** | **Safety boundary + sandbox** | Intent gate, tool gate, profile permission config, WSL/OS confinement, WebSocket auth foundation, audit, dedupe/budgets | Invariants 1–14 pass, including tool calls actually stopping before execution, no Windows-drive access, and shell bypass cases |
| **3** | **Text-first active layer** | CLI/UI typed requests, foreground/background lanes, cancellation, streaming answers, audit, first-token latency measurement | Explicit text request completes end-to-end; controls and status work; task cancellation and project locking pass tests |
| **4** | **Minimal proactivity** | T1 scheduler and one user-approved 08:00 briefing routine; per-routine capabilities/budget, notification policy, cycle guards | Briefing runs without a person present only under its saved scope; DND suppresses speaking; repeat/failure events do not loop |
| **5** | **Memory v1** | Markdown vault, SQLite search/index, retention/purge commands, recall, task recovery, compatible compaction bridge | Explicit memory persists; user can correct/delete it; opt-in transcript retention expires; no raw audio; state restores after compaction/restart |
| **6** | **Voice input** | Browser mic, VAD, local STT, wake phrase and PTT path; capture indicator and hard mic-off | PTT command works; passive speech cannot start tasks; replayed audio cannot approve risky actions; mic-off persists |
| **7** | **Voice output and barge-in** | Local Piper TTS, audio queue, stop/interruption, UI/PTT confirmations | TTS is interruptible; destructive confirmation is exact and one-time; cloud TTS remains off unless explicitly enabled |
| **8** | **Drivers and profiles** | REA, Feynman, and Serena configured by profile; project-root/profile separation | Correct tool set/profile for a real repository; driver absence is clearly reported; untrusted results remain data |
| **9** | **Additional triggers** | T4–T8: filesystem, Git/CI, system health, webhook, kernel-event triggers | Provenance/capability tests, origin/authentication, dedupe, cooldowns, cycle detection, rate and budget limits all pass |
| **10** | **Hardening and service lifecycle** | Restart/recovery behavior, system service, dashboards, cost controls, migration/upgrade process | Kill switch survives reboot; rollback/recovery works; API upgrade is deliberate and fully retested |

**Phase 0 and Phase 2 are hard stops.** Do not enable broad tool execution, voice-triggered actions, or proactive event triggers before their gates pass. Proactivity is validated early with the minimal schedule in Phase 4, before investing in voice.

---

## 14. Failure modes

| Failure | Symptom / risk | Mitigation |
|---|---|---|
| OpenCode API/config generation mismatch | Adapter or permission boundary behaves differently than expected | Verify exact binary; lock actual spec; test config and every protected action; deliberate upgrade PR |
| OpenCode defaults permit an action | Tool runs without gate seeing it | Explicit per-profile policy; real execution tests; disable any path that bypasses the gate |
| Prompt says “read-only” but agent issues write/shell | Intent classification is insufficient | Gate actual operation and resource; OS-level confinement |
| Shell command obfuscation/compound commands | Regex misses dangerous subcommand | Default-deny shell, simple allowlist, hold unknown syntax, sandbox process |
| `/mnt/c` or symlink escapes workspace | Agent reaches Windows files/secrets | Restricted mount namespace/dedicated WSL setup; canonical path/symlink checks; OS tests |
| Prompt injection from a PDF, web page, repo, tool, webhook | Untrusted content asks agent to exfiltrate or execute | Provenance tags; restricted capability envelope; output treated as data; no action authority from content |
| Browser website connects to localhost WebSocket | Cross-origin drive-by control attempt | Exact Origin/Host checks, one-use ticket, auth, loopback bind, rate/size limits |
| Spoken “yes” comes from a video/call | Dangerous action falsely confirmed | UI click or deliberate PTT only; one-time action-bound nonce; replay tests |
| Repeated T8 failure events trigger repeated tasks | Infinite retry/token loop | Parent lineage, dedupe, cooldown, retry ceiling, budgets, no self-triggering outputs |
| Research task blocks conversation | Assistant appears unresponsive | Foreground + bounded background lanes; independent cancellation; project write locks |
| Conflicting jobs mutate same repository | Concurrent changes corrupt workspace | Per-project locks or isolated worktrees; bounded concurrent work; explicit write policy |
| Memory records a wrong inference forever | Stale/false facts influence future answers | Provenance/confidence, review queue for inferred facts, user correction/deletion |
| Privacy leak through audio/transcripts/TTS | Bystanders or remembered text disclosed | Passive logging off, no raw audio, local TTS default, cloud TTS explicit opt-in, retention/purge |
| Compaction plugin API changes | Context state stops being reinjected | Version-specific adapter/plugin test; vault/DB remain canonical state |
| Feynman/REA/Serena offline | Capability silently disappears or model invents a result | Driver health tracking; explicit unavailable response; no hallucinated success |
| Cost spiral or runaway task | Unexpected spend | Per-task/daily budgets, cheap route for routine turns, hard timeout and cancellation |
| Daemon/server restart | Assistant silent or resumes unsafely | Persist mic-off and task states; recover only after gate revalidation; never replay stale approvals |
| High latency on trivial questions | JARVIS feels slower than a chatbot | Measure first token/audio; route general conversation to a lower-latency profile only after measurement |

---

## 15. Open questions

These questions do not block the architecture document, but the relevant implementation phase must resolve them before the dependent feature is enabled.

1. **Profile/workspace implementation:** verify whether the pinned OpenCode version can choose a named agent/profile and profile-specific MCP/tool permissions while keeping the session working directory set to the real project. The architectural rule is settled: profile and workspace must remain separate. The exact API/config mechanism is pending Phase 0.
2. **OpenCode version:** the original proposal pins `1.17.13`. Verify that version is actually installed and whether it matches the API schema already observed. If it does not, choose one version deliberately, re-run the full smoke tests, and update the lock.
3. **Feynman as MCP or CLI:** if `feynman serve` exposes a supported MCP interface, use it; otherwise wrap the CLI in the active layer. Keep network/auth capabilities explicit.
4. **One session per profile or pooled sessions:** begin with one session per active task/profile and measure cost/resource usage. Reuse only when project root, permission envelope, and profile identity match; do not let one session cross trust boundaries.
5. **`experimental.voice.session`:** inspect the pinned build briefly in Phase 0. Use only if it passes audio, interruption, permission, and privacy tests; otherwise keep voice in the browser/active layer.
6. **Speech languages:** the initial `base.en` model is English-only. Decide whether Hindi/Hinglish/other Indic languages are needed before locking STT and wake-word behavior.
7. **TTS voice:** local Piper is the default. Pick an English or Indian-English voice after latency and intelligibility tests; any cloud voice is opt-in.
8. **Native reverse engineering:** start with JS, Electron/ASAR, .NET, and HAR support. Ghidra/Hopper/IDA/native binaries are a later, explicit installation decision.
9. **Allowed project roots:** decide which repos JARVIS may work in and add them explicitly. The OS sandbox must enforce those roots, not just read this configuration.
10. **Embedding model:** initially disabled. If needed, select a local embedding model and benchmark it against FTS5 before enabling.
11. **Remote/cloud providers:** document which prompts, memory excerpts, tool outputs, and TTS text may leave the machine for every chosen provider. Never describe the full system as “local” just because ASR is local.

Defaults chosen by this document: wake word `jarvis` at utterance start; passive transcript logging off; no raw audio storage; local Piper TTS; no Ghidra in the initial milestone; budget and loop protection before proactive triggers.

---

## 16. Appendix — baseline observations and verification requirements

The following are observations recorded in the original architecture draft on 2026-10-22. They are **not a substitute for rerunning Phase 0 against the exact binary used for implementation**.

| Baseline claim in original draft | Original verification described | Required action in this revision |
|---|---|---|
| OpenCode can be driven headlessly | Session created, prompt ran, tool executed, steer enqueued/delivered, interrupt accepted | Re-run all flows using the exact pinned build and adapter |
| REST API schema was captured | `/openapi.json` fetched and parsed (281 KB; 119 operations reported) | Confirm current actual spec endpoint and store one canonical `docs/opencode-api.lock.json` |
| Permission reply values are `once\|always\|reject` | OpenAPI enum | Re-read exact schema; automated JARVIS approval must use `once`, never `always` |
| Inbox delivery supports `steer\|queue` | OpenAPI schema | Re-test both modes and delivery event behavior |
| SSE has 30+ event types | Captured during a run | Normalize only events actually supported by the pinned build |
| API described as experimental | OpenAPI description | Keep version pin and tested upgrade process |
| Feynman `0.5.51` available; shim was fixed for `0.5.34` | Local `--version` output | Re-run `feynman doctor`, ensure web search/auth are configured, and record actual interface |
| REA is an MCP server | Package metadata and official docs | Register disabled until tool policy/permission tests pass |
| WSL had no `/dev/snd` | Local inspection | Recheck environment; retain browser audio path unless native audio is deliberately configured |
| Chrome/Edge binaries were present on Windows | Local filesystem | Not an architecture dependency; verify during UI implementation |
| Model routes were available | `opencode models` output | Recheck provider, route IDs, cost, latency and privacy behavior before selection |

### Current official OpenCode documentation to consult

- [Server / HTTP API](https://docs.opencode.ai/docs/server/)
- [V1 permissions](https://docs.opencode.ai/docs/permissions/)
- [V1 plugins](https://docs.opencode.ai/docs/plugins/)
- [V2 permissions](https://dev.opencode.ai/v2/docs/permissions/)
- [V2 plugin hooks](https://dev.opencode.ai/v2/docs/build/plugins/)

The docs currently distinguish V1 and V2 contract details. In particular, the permission schema and action names differ. Treat the live schema from the pinned binary as authoritative for implementation.

---

## 17. Revision summary

This revision applies the security and consistency fixes discussed alongside the original plan:

- Split safety into intent admission and actual tool/permission authorization.
- Add explicit OpenCode permission configuration checks, version/API schema locking, and an OS sandbox that limits filesystem and network access.
- Preserve source provenance and reduced capability envelopes for content-derived events and tool output.
- Secure the browser WebSocket with exact Origin checks and short-lived single-use connection tickets.
- Restrict destructive confirmations to action-bound UI clicks or deliberate PTT; passive/replayed audio cannot approve them.
- Add foreground/background task lanes, project locks, trigger deduplication, cooldowns, cycle detection, and budgets from the beginning.
- Separate JARVIS profiles (“hats”) from real project workspaces; the exact pinned-version mechanism is a Phase 0 validation item.
- Make passive transcript logging opt-in, never persist raw audio, use a Markdown memory vault with SQLite indexes, and default to local TTS.
- Move a minimal 08:00 scheduler immediately after the text-first milestone.
- Unify the adapter path (`src/jarvis/kernel/adapter.py`), generated-client location, and one API lock filename (`docs/opencode-api.lock.json`); remove the redundant session `engine` column.

The revised document is still an architecture proposal. The tests, sandbox, API compatibility, driver configuration, and runtime behavior must be implemented and verified before JARVIS is considered secure or production-ready.
