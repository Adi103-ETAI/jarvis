# JARVIS — Full Architecture

> **Project root:** `/home/sonic/work-jarvis`
> **Status:** architecture approved, not yet implemented
> **Date:** 2026-10-22
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
| Where does "remember me across months" live? | Active layer | Kernel sessions are project-scoped and ephemeral |

---

## 2. System overview

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ L4  INTERFACE — Windows / Chrome                                               │
│     (the "console" — where you talk to it and watch it work)                    │
│                                                                                │
│  ┌───────────────┐  PCM16 16kHz frames  ┌──────────────────────────────────┐  │
│  │ mic capture   │─────────────────────▶│  WebSocket  /ws/audio            │  │
│  │ + energy gate │                      │  WebSocket  /ws/control          │  │
│  └───────────────┘                      │  (bi-directional, one port each) │  │
│  ┌───────────────┐                      └──────────────┬───────────────────┘  │
│  │ push-to-talk  │                                     │                      │
│  │ mic-off       │   TTS mp3 ◀── text deltas ◀──┐     │                      │
│  │ wake badge    │────────────────────────────┴─────┘                      │
│  └───────────────┘                                                            │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐   │
│  │ transcript    │  │  assistant    │  │  audit trail  │  │  task queue   │   │
│  │ ribbon        │  │  state panel  │  │  viewer       │  │  panel        │   │
│  └───────────────┘  └───────────────┘  └───────────────┘  └───────────────┘   │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     │  audio frames  ↓        ↑  commands
┌────────────────────────────────────▼───────────────────────────────────────────┐
│ L3  ACTIVE LAYER — "jarvisd"    Python 3.13 · one always-on process           │
│                                                                                │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   │
│  │ PERCEPTION   │   │ ACTOR        │   │ MEMORY       │   │ PRESENCE     │   │
│  │              │   │              │   │              │   │              │   │
│  │ • VAD        │   │ • trigger    │   │ • facts      │   │ • TTS        │   │
│  │   segmenter  │   │   engine     │   │ • transcript │   │ • barge-in   │   │
│  │ • STT        │   │ • scheduler  │   │ • recall     │   │ • notify     │   │
│  │   (whisper)  │   │ • router     │   │   (FTS5 +    │   │ • mic state  │   │
│  │ • wake-word  │   │ • task queue │   │    embed)    │   │              │   │
│  │   matcher    │   │ • hat picker │   │ • sessions   │   │              │   │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘   │
│         │                  │                   │                  │          │
│         │        ┌─────────▼───────────────────▼──────────────────▼────┐      │
│         └───────▶│              ★ SAFETY GATE ★                       │◀─────┤
│                  │  command?  wake word?  ptt?  risk?  asleep?       │      │
│                  │                                                    │      │
│                  │   ALLOW ──▶ HOLD (ask out loud) ──▶ DENY ──▶ LOG  │      │
│                  └────────────────────────┬───────────────────────────┘      │
│                                           │  "do this task" / "stop"       │
└───────────────────────────────────────────┼─────────────────────────────────┘
                                            │  STABLE ABI  (we own this file)
┌───────────────────────────────────────────▼─────────────────────────────────────┐
│ L2  KERNEL ADAPTER — libjarvis/kernel.py   thin · version-pinned · tested      │
│                                                                                │
│   create_session   prompt        steer        interrupt   on_event             │
│   reply_permission fork          compact      inject_context                   │
│   list_agents      list_skills   set_model    get_context                      │
│                                                                                │
│   ← maps to →  opencode serve  ·  HTTP + SSE  ·  auth: fixed password          │
└───────────────────────────────────────────┬─────────────────────────────────────┘
                                            │
┌───────────────────────────────────────────▼─────────────────────────────────────┐
│ L1  KERNEL — opencode (one process, many sessions = many "hats")              │
│                                                                                │
│   agent loop · tool execution · context window + auto-compaction · LSP ·       │
│   git / worktree / pty · skills · agents · plugins · permission ASKING ·       │
│   long-running background tasks · steerable mid-turn                          │
│                                                                                │
│   HATS (each = its own working directory, hence its own config):              │
│     [general]   ~/.config/jarvis/hats/general     → personality, no special   │
│     [code]      ~/work-jarvis/hats/code           → AGENTS.md, serena, lsp    │
│     [research]  ~/work-jarvis/hats/research       → feynman driver            │
│     [re]        ~/work-jarvis/hats/re             → rea driver                │
│                                                                                │
│   ★ working directory IS the personality selector (per OpenCode's own model)   │
└───────────────────────────────────────────┬────────────────────────────────────┘
                                            │  MCP (drivers)
┌───────────────────────────────────────────▼─────────────────────────────────────┐
│ L0  DRIVERS — MCP servers, loaded per-hat                                      │
│                                                                                │
│   REA · reverse engineering (decompile, strings, xrefs, JS/ASAR, .NET, HAR)     │
│   Feynman · research (lit, deepresearch, compare, audit, papers, web search)   │
│   serena · code intelligence (symbols, references, memories)                   │
│   built-in websearch · plus any MCP server you add later                       │
│   ★ in-kernel PLUGINS: jarvis-memory (inject state at compaction)             │
└────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Layer contracts

### L4 — Interface (Chrome on Windows)

| Piece | Job | Why here |
|---|---|---|
| `getUserMedia` + AudioWorklet | 16 kHz mono PCM16, 20 ms frames | WSL has no `/dev/snd`; the browser *is* the sound card |
| energy gate | drop silence before it hits the network | CPU + bandwidth; Whisper-meeting-notes style |
| push-to-talk button | hold to speak = guaranteed command path | works even if the wake word fails |
| mic-off toggle + hotkey | hard stop on capture | privacy; persists across restarts |
| transcript ribbon | live view of what it heard | trust: you can see what it will act on |
| audio queue | play TTS chunks in order, drop on barge-in | no stutter, instant interruption |

### L3 — Active layer (`jarvisd`, Python)

| Module | Job | Key constraint |
|---|---|---|
| `perception/vad.py` | split stream into utterances | never transcribe silence |
| `perception/stt.py` | utterance → text | local model, no audio leaves the machine |
| `perception/wake.py` | detect the wake phrase in the transcript | transcript-based is more reliable than acoustic KWS |
| `actor/triggers.py` | the 8 trigger sources → one `TaskIntent` | **all triggers funnel through one place** |
| `actor/router.py` | TaskIntent → which hat / which model | cheap classifier, not a full turn |
| `actor/queue.py` | one task at a time, cancellable | the assistant must not talk over itself |
| `policy/gate.py` | **decide ALLOW / HOLD / DENY** | the security boundary — nothing bypasses it |
| `policy/rules.py` | risk classification of the requested action | `rm -rf` ≠ `ls` |
| `memory/store.py` | facts, utterances, sessions, audit | survives restarts |
| `memory/recall.py` | FTS5 + optional embeddings | "what did I say about X" |
| `presence/tts.py` | text → speech, sentence-chunked | first syllable fast |
| `presence/notifier.py` | desktop/UI alerts for async results | research takes 10 min |

### L2 — Kernel adapter

**This is the most important file in the project.** It is the only place that knows OpenCode's HTTP API exists. Everything above it is version-independent.

```
libjarvis/kernel.py
  class Kernel:
      async def create_session(hats: str, ...) -> Session
      async def prompt(session, text, delivery="queue") -> None
      async def steer(session, text) -> None          # inject mid-turn
      async def interrupt(session) -> None            # stop now
      async def inject_context(session, text) -> None # write-only, no turn
      async def reply_permission(session, request_id, decision) -> None
      async def fork(session) -> Session
      async def compact(session) -> None
      async def events() -> AsyncIterator[KernelEvent] # SSE
      async def list_agents() / list_skills()
```

Every method maps to exactly one OpenCode endpoint. **Rule: if a change to OpenCode breaks something, it breaks this file and one test.** Nothing else in the codebase may import an OpenCode URL.

| Our method | OpenCode endpoint (v2) | Stability |
|---|---|---|
| `create_session` | `POST /api/session` | ⚠ experimental |
| `prompt` | `POST /api/session/{id}/prompt` | ⚠ experimental |
| `steer` | same, `delivery: "steer"` | ⚠ experimental |
| `interrupt` | `POST /api/session/{id}/interrupt` | ⚠ experimental |
| `reply_permission` | `POST /api/session/{id}/permission/{rid}/reply` `{once\|always\|reject}` | ⚠ experimental |
| `inject_context` | `POST /api/session/{id}/synthetic` | ⚠ experimental |
| `fork` | `POST /api/session/{id}/fork` | ⚠ experimental |
| `compact` | `POST /api/session/{id}/compact` | ⚠ experimental |
| `events` | `GET /api/event` (SSE) | ⚠ experimental |
| `list_agents` | `GET /api/agent` | ⚠ experimental |
| `list_skills` | `GET /api/skill` | ⚠ experimental |
| `sessions` | `GET /api/session` | ⚠ experimental |
| `mcp` | `GET /api/mcp`, `experimental.mcp.*` | ⚠ experimental |

> ⚠ **Every row is experimental.** The OpenAPI spec describes itself as *"Experimental HttpApi surface for selected instance routes"*, and at least three API generations exist in the wild (legacy v1 path/body shape · v2 `/api/*` · the new embedded `@opencode/sdk`). Therefore: **pin the OpenCode version, generate the client from `/openapi.json` at build time, and treat any upgrade as a deliberate tested change.**

### L1 — Kernel (OpenCode)

Owns, and does well: the agent loop, tool execution, context-window management and auto-compaction, permission *asking*, LSP, git, worktrees, PTY, skills, agents, plugins, MCP client, background tasks.

Does **not** own, by design: initiative, perception, long-term memory, policy.

**Why the working directory is the personality selector.** OpenCode loads the configuration of the directory a session belongs to — its agents, skills, commands, plugins, MCP servers, `AGENTS.md`. So a "hat" is just a directory:

```
~/work-jarvis/hats/
├── general/
│   ├── .opencode/agent/jarvis.md      ← persona, tone, speakable-output rules
│   ├── AGENTS.md                      ← standing instructions
│   └── .opencode/plugins/jarvis-memory.ts   ← inject state at compaction
├── code/
│   ├── AGENTS.md                      ← engineering conventions, test rules
│   └── .mcp.json                      ← serena
├── research/
│   └── .mcp.json                      ← feynman
└── re/
    └── .mcp.json                      ← rea
```

**The `jarvis-memory` plugin** hooks `experimental.session.compacting` and pushes persistent state into the compaction prompt:

```ts
export const JarvisMemory = async () => ({
  "experimental.session.compacting": async (_input, output) => {
    output.context.push(`
## Persistent state (do not lose)
- Current task: ${await currentTask()}
- Decisions made: ${await recentDecisions()}
- Open threads: ${await openThreads()}
- Long-term memory index: ${await memoryPointer()}
`)
  },
})
```

This is the **pager syscall**: when the kernel's context window is compacted, the important state is written back in, so the assistant doesn't forget what it was doing.

### L0 — Drivers

| Driver | Kind | Gives the kernel |
|---|---|---|
| **REA** | MCP | decompilation, strings, xrefs, JS/ASAR/Electron, .NET, HAR, firmware — *evidence with locations* |
| **Feynman** | MCP or CLI | literature review, deepresearch, paper search/QA, comparison matrices, web search |
| **serena** | MCP | symbol-level code intelligence, project memories |
| **websearch** | built-in | plain web search when Feynman isn't the right tool |
| your own | MCP | anything else you add later |

Drivers are **in-kernel**, so the *model* decides when to use them. You don't write a router for "tell Jarvis to control Feynman" — the kernel does it, because Feynman is simply one of its tools.

---

## 4. The Activeness Engine

This is the part that does not exist in OpenCode, and it is the reason the project exists.

```
                          ┌──────────────────────────┐
   T1 CLOCK ────────────▶│                          │
   T2 VOICE (wake/PTT) ─▶│                          │
   T3 TYPED INPUT ──────▶│   TRIGGER ENGINE         │
   T4 FS WATCH ─────────▶│   (single funnel)        │──▶ TaskIntent
   T5 GIT / CI ─────────▶│                          │    {text, source, urgency,
   T6 SYSTEM HEALTH ────▶│                          │     risk_hint, hat}
   T7 WEBHOOK ──────────▶│                          │
   T8 KERNEL EVENT ─────▶│                          │
                          └──────────────────────────┘
                                          │
                                          ▼
                              ┌───────────────────────┐
                              │  ★ SAFETY GATE ★      │
                              └───────────────────────┘
                                          │ ALLOW
                                          ▼
                              ┌───────────────────────┐
                              │  TASK QUEUE           │
                              │  one active task      │
                              │  cancellable          │
                              └───────────────────────┘
                                          │
                                          ▼
                              ┌───────────────────────┐
                              │  HAT PICKER           │
                              │  general/code/        │
                              │  research/re          │
                              └───────────────────────┘
                                          │
                                          ▼
                              ┌───────────────────────┐
                              │  KERNEL ADAPTER       │
                              └───────────────────────┘
```

### The 8 triggers

| ID | Trigger | Source of truth | Example |
|---|---|---|---|
| T1 | **Time** | cron expression in DB | "every weekday 08:00 → briefing" |
| T2 | **Voice** | wake word or PTT | "Hey Jarvis, what's on my plate?" |
| T3 | **Typed** | browser UI | typed command |
| T4 | **Filesystem** | inotify watch on chosen dirs | "a new PDF landed in ~/Downloads" |
| T5 | **Git / CI** | git hook, `gh` poll, or CI webhook | "the build failed on main" |
| T6 | **System** | battery, disk, thermal, network | "battery under 20% and you have 3h of work" |
| T7 | **Webhook** | local HTTP endpoint | "the lab published the new dataset" |
| T8 | **Kernel event** | SSE from the kernel | "the tool failed 3 times" / "compaction happened" |

**Design rule: every trigger produces the same `TaskIntent` shape**, so the safety gate has exactly one input to reason about. A scheduled briefing and a spoken request are indistinguishable to the gate — which is exactly right.

---

## 5. The Safety Gate (the security boundary)

```
   TaskIntent arrives
          │
          ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ Q1  SOURCE CHECK                                            │
   │     passive speech?  →  DENY (log only)                     │
   │     no wake word & not PTT?  →  DENY (log only)             │
   └───────────────────────────┬──────────────────────────────────┘
                               │ passes
                               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ Q2  MODE CHECK                                              │
   │     system is ASLEEP (mic off / do-not-disturb)? → DENY      │
   │     user is currently speaking? → HOLD (don't talk over)    │
   └───────────────────────────┬──────────────────────────────────┘
                               │ passes
                               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ Q3  RISK CLASSIFICATION (of the *planned action*)           │
   │                                                             │
   │     READ   (ls, cat, git status, search)      → ALLOW       │
   │     WRITE  (edit file, create branch)          → ALLOW*     │
   │     NET    (fetch, call API)                   → ALLOW*     │
   │     DESTRUCTIVE (rm -rf, sudo, force-push,     → HOLD       │
   │                 pip install, curl|sh)                         │
   │     EXFILTRATIVE (upload secrets, post creds)  → DENY       │
   │                                                             │
   │     * ALLOW only inside allowed_roots / allowlist          │
   └───────────────────────────┬──────────────────────────────────┘
                               │
             ┌─────────────────┼─────────────────┐
             ▼                 ▼                 ▼
          ALLOW             HOLD              DENY
             │                 │                 │
             ▼                 ▼                 ▼
       call kernel     speak the question   log + tell user
       + audit        "May I delete the      "I won't do that"
                       build folder?"                │
       (never use      wait 10s for yes/no        │
        --auto)              │                     │
             │          ┌────┴────┐                │
             │        yes│      no/timeout         │
             │          ▼         ▼                │
             │        ALLOW     DENY               │
             │          │         │                │
             └──────────┴─────────┴────────────────┘
                               │
                               ▼
                     audit log (always)
```

### Invariants (each gets a dedicated test)

1. **Ambient speech can never produce a kernel call.** The gate is checked *before* the kernel adapter, not in the prompt.
2. **`--auto` is never passed to the kernel.** The kernel's permission requests are the gate's job to answer, and the gate always answers explicitly.
3. **The gate is the only path to the kernel adapter.** No other module may call `Kernel.prompt`.
4. **Every decision is audited** — source, decision, reason, whether the human confirmed.
5. **Mic-off persists across restarts** and blocks T2 until explicitly re-enabled.
6. **The gate cannot be widened by the model.** No prompt, skill, or `AGENTS.md` can grant permission; only `policy/rules.py` can.

### Why the gate lives in user space

Because it needs state the kernel does not have: which channel the speech came from, whether the wake word was present, whether the user is currently talking, whether the system is asleep, how many times this same action was requested in the last minute. The kernel only knows "a tool wants to run X". The gate knows "whether it should".

---

## 6. Memory architecture — three tiers

```
┌──────────────────────────────────────────────────────────────────────┐
│ L3  IDENTITY        who I am: persona, tone, preferences, policies  │
│     lives: hats/*/.opencode/agent/jarvis.md  +  policy/rules.py      │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ always in effect
┌───────────────────────────────▼──────────────────────────────────────┐
│ L2  RECALL  (long-term, cross-session, yours)                       │
│     facts: "the deploy key rotates monthly"                          │
│     transcript: every utterance ever spoken                          │
│     store: ~/.jarvis/jarvis.db   ·  FTS5 + optional embeddings      │
│                                                                      │
│     read paths:                                                      │
│       session.synthetic      → inject recalled facts, no turn        │
│       compaction plugin      → re-inject state after compaction     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ bridged in
┌───────────────────────────────▼──────────────────────────────────────┐
│ L1  SESSION  (the kernel's working memory, one session = one convo) │
│     context window · auto-compacted by the kernel                   │
│     scope: one hat, one directory, until you close it               │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ summarised on compaction
┌───────────────────────────────▼──────────────────────────────────────┐
│ L0  TURN  (single exchange)                                         │
└──────────────────────────────────────────────────────────────────────┘
```

**Why L2 cannot live in the kernel:** OpenCode sessions are scoped to a directory and are ephemeral. "What did I say about the auth bug last Tuesday" spans days and projects. That data has to live in your database, and be *pushed into* the kernel at the right moments.

**The two bridge mechanisms:**
- `session.synthetic` — write-only context injection. Inject "you are working on the auth bug; the relevant note says X" without spending a turn.
- the compaction plugin — when the kernel summarises, your state is pushed in, so it survives.

---

## 7. Voice pipeline (data flow)

```
 Chrome                    jarvisd                                 kernel
   │                          │                                      │
   │  ── PCM16 frames ──────▶ │                                      │
   │                          │  ring buffer (5 s)                   │
   │                          │      │                               │
   │                          │      ├──▶ VAD segmenter               │
   │                          │      │      (silero / webrtcvad)     │
   │                          │      │      hangover 400 ms          │
   │                          │      │      pre-roll 300 ms          │
   │                          │      ▼                               │
   │                          │  utterance (≤15 s, else force-split) │
   │                          │      │                               │
   │                          │      ├──▶ wake-word matcher (text)    │
   │                          │      │      └─▶ "jarvis" detected?     │
   │                          │      │                               │
   │                          │      └──▶ STT (faster-whisper int8)    │
   │                          │             CPU, ~0.2× realtime       │
   │                          │                 │                     │
   │                          │                 ▼                     │
   │                          │        ┌──────────────────┐            │
   │                          │        │ CHANNEL SPLIT   │            │
   │                          │        └──┬───────────┬───┘            │
   │                          │   command │           │ passive        │
   │                          │           ▼           ▼               │
   │                          │      SAFETY GATE   transcript log      │
   │                          │           │           │               │
   │                          │           ▼           ▼               │
   │                          │      KERNEL ADAPTER  jarvis.db        │
   │                          │           │                          │
   │  ◀── text deltas ─────────┼───────────┼──────────────────────────▶│
   │  ◀── TTS mp3 chunks ──────┼───────────┘                          │
   │                          │                                      │
   │  ── "stop" (barge-in) ──▶ │────── interrupt ────────────────────▶│
   │                          │                                      │
   │  ◀── cleared audio queue ┘                                      │
```

**Barge-in** = detect speech while TTS is playing → clear the browser's audio queue → `interrupt` the kernel → the new utterance becomes the command. Two independent mechanisms, so interruption feels instant.

---

## 8. Sequence diagrams

### 8.1 Spoken command

```
User        Chrome        jarvisd              Gate            Kernel
 │            │             │                   │                │
 │ "hey      │             │                   │                │
 │  jarvis,  │──frames───▶ │                   │                │
 │  what's   │             │ VAD → utterance   │                │
 │  2+2?"    │             │ STT → "hey jarvis,│                │
 │            │             │   what's 2+2?"   │                │
 │            │             │ wake? YES        │                │
 │            │             │──────────────────▶│                │
 │            │             │                   │ source=command │
 │            │             │                   │ wake=yes       │
 │            │             │                   │ mode=awake     │
 │            │             │                   │ risk=READ      │
 │            │             │                   │ → ALLOW        │
 │            │             │◀──────────────────│                │
 │            │             │ prompt("what's 2+2?")────────────────▶│
 │            │             │                   │                │
 │            │             │◀────────── text deltas ─────────────│
 │            │◀──delta─────│                   │                │
 │            │◀──mp3───────│                   │                │
 │            │ play        │                   │                │
```

### 8.2 Proactive routine (no human present)

```
Scheduler        jarvisd           Gate            Kernel          Notify
   │               │                │                │               │
   │ 08:00 fires   │                │                │               │
   │──────────────▶│                │                │               │
   │               │ TaskIntent     │                │               │
   │               │ source=cron    │                │               │
   │               │───────────────▶│                │               │
   │               │                │ source≠passive │               │
   │               │                │ asleep? no     │               │
   │               │                │ risk=READ      │               │
   │               │                │ → ALLOW        │               │
   │               │◀───────────────│                │               │
   │               │ prompt("summarise today")───────────────────▶│
   │               │◀─────────────────────────── text ────────────│
   │               │ TTS + push notification──────────────────────▶│
   │               │ store in transcript                          │
```

### 8.3 Destructive action → spoken confirmation

```
User      jarvisd        Gate            Kernel          Browser/TTS
 │           │             │               │                │
 │ "delete   │             │               │                │
 │  the      │────────────▶│               │                │
 │  build"   │             │ risk=DESTRUCT │                │
 │           │             │ → HOLD        │                │
 │           │◀── "May I delete the build folder?" ──────────▶│
 │           │── speak ──────────────────────────────────────▶│
 │ "yes"     │◀────────── audio ────────────────────────────│
 │           │             │               │                │
 │           │             │ → ALLOW (human confirmed)        │
 │           │ prompt("delete the build folder")───────────▶│
 │           │             │◀── permission.asked ─────────────│
 │           │ reply_permission(once)──────────────────────▶│
```

### 8.4 Barge-in

```
User     Browser     jarvisd              Kernel
 │          │           │                   │
 │          │◀── TTS playing ────│          │
 │ "wait,"  │           │                   │
 │──────────┼──frames──▶│                   │
 │          │           │ VAD → speech      │
 │          │           │ interrupt ────────▶│
 │          │◀── clear audio queue        │
 │          │           │ new utterance → gate → prompt ───▶│
```

---

## 9. Protocol specifications

### 9.1 Browser ↔ jarvisd (WebSocket)

**Client → server**

| Message | Payload | Meaning |
|---|---|---|
| `hello` | `{}` | handshake |
| `audio` | `{pcm: base64, seq: int}` | 20 ms PCM16 frame |
| `ptt` | `{down: bool}` | push-to-talk state |
| `mute` | `{on: bool}` | mic off/on |
| `text` | `{text: str}` | typed command |
| `ack` | `{id: int}` | flow control |

**Server → client**

| Message | Payload | Meaning |
|---|---|---|
| `ready` | `{sampleRate, frameMs}` | handshake ack |
| `vad` | `{speech: bool}` | live level state |
| `transcript` | `{text, ts, channel}` | what it heard (passive or command) |
| `agent_delta` | `{text}` | live kernel text |
| `tts` | `{pcm, seq}` | speech chunk |
| `tts_stop` | `{}` | clear the audio queue (barge-in) |
| `ask` | `{question, expiresAt}` | spoken confirmation request |
| `answer` | `{question, decision}` | user's spoken yes/no |
| `task` | `{id, state, title}` | task queue state |
| `error` | `{message}` | non-fatal error |

### 9.2 jarvisd ↔ kernel (SSE events consumed)

| Event | Used for |
|---|---|
| `session.text.delta` / `.started` / `.ended` | live captions + TTS streaming |
| `session.reasoning.delta` | show "thinking" state (optional) |
| `session.tool.called` / `.success` / `.progress` | activity panel, audit |
| `session.tool.input.started` / `.ended` | show what it's about to do |
| `session.execution.started` / `.succeeded` | task lifecycle |
| `session.step.started` / `.ended` | step progress |
| `session.inbox.enqueued` / `.delivered` | confirm a steer landed |
| `session.usage.updated` | token accounting / budget |
| `session.instructions.updated` | detect config drift |
| `mcp.status.changed` / `mcp.resources.changed` | driver online/offline |
| `agent.updated` / `skill.updated` / `plugin.updated` | **capability discovery** — the assistant notices when a new driver or skill appears |
| `server.connected` | reconnect signal |

---

## 10. Data model (`~/.jarvis/jarvis.db`)

```
┌──────────────┬──────────────────────────────────────────────────────────┐
│ utterances  │ id, ts, channel(passive|command), text, confidence,    │
│              │ audio_path, wake_hit, session_id, hat                   │
├──────────────┼──────────────────────────────────────────────────────────┤
│ facts        │ id, ts, text, source(explicit|auto|tool), source_id,   │
│              │ embedding (BLOB, optional 384-dim float32)              │
├──────────────┼──────────────────────────────────────────────────────────┤
│ sessions     │ id, engine(jarvis|opencode), hat, started_at, ended_at │
├──────────────┼──────────────────────────────────────────────────────────┤
│ tool_audit   │ id, ts, task_id, action, risk, decision(allow|hold|    │
│              │ deny), confirmed_by_human, args_digest, result_digest,  │
│              │ duration_ms                                            │
├──────────────┼──────────────────────────────────────────────────────────┤
│ routines     │ id, name, cron, prompt, hat, enabled, last_run          │
├──────────────┼──────────────────────────────────────────────────────────┤
│ usage        │ id, ts, session_id, model, input_tokens, output_tokens, │
│              │ cost_usd                                                │
├──────────────┼──────────────────────────────────────────────────────────┤
│ settings     │ key, value  (mic_on, wake_words, do_not_disturb, …)     │
├──────────────┼──────────────────────────────────────────────────────────┤
│ search_fts   │ FTS5 over utterances + facts  (porter tokenizer)       │
└──────────────┴──────────────────────────────────────────────────────────┘
```

**Privacy rules:** passive utterances are retained `retention_days` (default 14) and purged by a routine; facts are never auto-purged; `tool_audit` keeps only digests, never full command output; the whole DB can be wiped with one command.

---

## 11. Configuration

```toml
# config/jarvis.toml
[kernel]
command        = "opencode"
args           = ["serve", "--port", "4096", "--hostname", "127.0.0.1"]
version        = "1.17.13"          # PINNED — upgrades are deliberate
password_env   = "OPENCODE_SERVER_PASSWORD"   # fixed, not random per boot
api_spec_cache = "kernel-api.lock.json"       # generated from /openapi.json

[voice]
sample_rate    = 16000
frame_ms       = 20
wake_words     = ["jarvis", "hey jarvis"]
model          = "base.en"          # faster-whisper, int8
vad            = "webrtcvad"
aggressiveness = 2
hangover_ms    = 400
pre_roll_ms    = 300
max_utterance_s = 15
tts            = "edge-tts"
tts_voice      = "en-US-GuyNeural"

[policy]
allowed_roots        = ["~/work-jarvis", "~/Documents/jarvis"]
allow_network_hosts  = ["api.openrouter.ai", "arxiv.org"]
auto_approve_risks   = ["read", "write"]
require_human_for    = ["destructive", "exfiltrative"]
confirm_timeout_s    = 10

[memory]
db             = "~/.jarvis/jarvis.db"
retention_days = 14
embeddings     = true

[hats]
general   = "~/.config/jarvis/hats/general"
code      = "~/work-jarvis/hats/code"
research  = "~/work-jarvis/hats/research"
re        = "~/work-jarvis/hats/re"
```

---

## 12. File layout

```
/home/sonic/work-jarvis/
├── pyproject.toml
├── README.md
├── config/jarvis.toml
├── src/jarvis/
│   ├── cli.py                  # jarvis chat | serve | doctor | purge | routines
│   ├── config.py
│   ├── active/                 # ── L3 ACTIVE LAYER ──
│   │   ├── daemon.py           # process entrypoint
│   │   ├── perception/
│   │   │   ├── vad.py          # segmenter
│   │   │   ├── stt.py          # faster-whisper
│   │   │   ├── wake.py         # wake-word matcher
│   │   │   └── audio.py        # ring buffer
│   │   ├── actor/
│   │   │   ├── triggers.py     # T1..T8
│   │   │   ├── router.py       # intent → hat
│   │   │   └── queue.py        # one active task
│   │   ├── policy/
│   │   │   ├── gate.py         # ★ SAFETY GATE ★
│   │   │   └── rules.py        # risk classification
│   │   ├── memory/
│   │   │   ├── store.py
│   │   │   └── recall.py
│   │   ├── presence/
│   │   │   ├── tts.py
│   │   │   └── notifier.py
│   │   └── server/
│   │       ├── app.py          # FastAPI
│   │       ├── ws.py
│   │       └── routes.py
│   ├── kernel/                 # ── L2 KERNEL ADAPTER (the only file that
│   │   ├── adapter.py          #    may know an OpenCode URL) ──
│   │   ├── events.py
│   │   ├── permissions.py
│   │   └── lock.py             # generated client from /openapi.json
│   ├── web/                    # ── L4 INTERFACE ──
│   │   ├── index.html
│   │   ├── app.js
│   │   ├── audio-worklet.js
│   │   └── style.css
│   └── hats/                   # ── personality + driver wiring ──
│       ├── general/.opencode/agent/jarvis.md
│       ├── code/AGENTS.md
│       ├── research/.mcp.json
│       └── re/.mcp.json
├── plugins/jarvis-memory.ts    # in-kernel: inject state at compaction
├── tests/
└── docs/
    ├── ARCHITECTURE.md         # this file
    ├── opencode-api.lock.json  # pinned API surface
    └── decisions/              # ADRs
```

---

## 13. Build order

Each phase ends with something you can *use*, and a gate that must pass.

| # | Phase | Deliverable | Gate |
|---|---|---|---|
| **0** | **Kernel substrate** | `opencode serve` running with a fixed password; API spec captured and locked; feynman shim fixed; REA registered | `jarvis doctor` all green |
| **1** | **Kernel adapter** | `libjarvis/kernel.py` + generated client | test: prompt → stream → steer → interrupt → reply_permission, all green |
| **2** | **Safety gate** | `policy/gate.py` + rules | test: 6 invariants, incl. "ambient speech never reaches the kernel" |
| **3** | **Text active layer** | CLI chat + triggers T1/T3 + task queue + audit | speak/typing → kernel → streamed answer → audit row |
| **4** | **Memory** | facts + transcript + recall + compaction plugin | "what did I say about X" answered; state survives compaction |
| **5** | **Voice in** | browser capture + VAD + STT + wake word | passive transcript live; command path works via PTT |
| **6** | **Voice out** | TTS + barge-in + spoken confirmations | interrupt works; destructive action asks out loud |
| **7** | **Drivers** | REA + Feynman as MCP per hat | "explain this Electron app's key storage" returns cited evidence |
| **8** | **Proactivity** | T4–T8 triggers, routines, scheduler | 08:00 briefing fires on its own |
| **9** | **Hardening** | injection resistance, budget, fallbacks, systemd | kill-switch survives reboot; fallbacks work |

**Phase 0 is where the project is won or lost.** Everything downstream depends on the kernel being reachable, authenticated, and pinned. Do not skip it.

---

## 14. Failure modes

| Failure | Symptom | Mitigation |
|---|---|---|
| OpenCode changes its API | adapter breaks on upgrade | adapter + pinned version + locked spec + upgrade is a deliberate PR |
| Kernel dies / WSL restarts | assistant silent | `jarvisd` retries, re-reads password, resumes session; mic state persists |
| Random server password each boot | cannot re-auth | pin via env var (verify exact name in Phase 0) |
| Wake word misses in noise | no command recognised | transcript matching + PTT fallback + optional acoustic KWS |
| Ambient speech triggers action | **the dangerous one** | channel gate before the adapter; tested invariant #1 |
| Long research blocks the queue | assistant unresponsive | research runs as a background task; notify on completion |
| Compaction loses context | assistant forgets mid-task | compaction plugin re-injects persistent state |
| Feynman/REA driver offline | capability silently missing | `mcp.status.changed` → the assistant says so instead of hallucinating |
| Token cost spiral | surprise bill | usage table, daily budget, cheap model for routine turns |
| Always-on privacy concern | user discomfort | mic-off hotkey, visible indicator, retention policy, one-command purge, local-only ASR |

---

## 15. Open questions

1. **Feynman as MCP or CLI?** If `feynman serve` exposes MCP, it's a clean driver like REA. If not, wrap it as a user-space delegate. Changes one module.
2. **One kernel session or one per hat?** The plan says one per hat (directory = personality). Confirm.
3. **What does `experimental.voice.session` do?** If it offers a usable realtime audio transport, part of the voice path could move into the kernel. Worth 15 minutes in Phase 0.
4. **Wake word(s)?** "Jarvis" only, or also "computer"?
5. **TTS voice?** Default `en-US-GuyNeural`, or an Indian-English voice?
6. **Passive capture on by default?** Privacy vs. the "what did I say about X" feature. My default: on, with a 14-day retention and a loud mic-off.
7. **Native reverse engineering?** REA needs Ghidra/Hopper/IDA for native binaries (~2 GB for Ghidra). Approve installing it, or start with JS/Electron/.NET/HAR only?
8. **Language scope?** English only for now, or Indic languages too (affects STT model, wake word, and TTS voice)?

---

## 16. Appendix — what was actually verified

| Claim | How verified |
|---|---|
| OpenCode is drivable headlessly | live server: session created, prompt ran, tool executed, steer enqueued+delivered, interrupt accepted |
| 119 REST operations with typed permissions | `/openapi.json` fetched and parsed (281 KB) |
| `Permission.Reply` = `once\|always\|reject` | OpenAPI schema enum |
| `Session.Inbox.Delivery` = `steer\|queue` | OpenAPI schema enum |
| SSE event bus with 30+ event types | live event capture during a real run |
| API is labelled experimental | OpenAPI `info.description` field |
| feynman 0.5.51 works, shim broken to 0.5.34 | `--version` on both paths |
| Feynman auth good, web search unconfigured | `feynman doctor` output |
| REA is an MCP server | npm `rea-agents@6.7.0` metadata + official docs |
| WSL has no audio device | `/dev/snd` absent, no `pactl`/`aplay` |
| Chrome + Edge present on Windows | `/mnt/c/Program Files/…` |
| Model routes available | `opencode models` → `anthropic/claude-sonnet-5`, `claude-opus-5`, `haiku-5-5`, `glm-5.3-flash`, `opencode/beam-free` |
