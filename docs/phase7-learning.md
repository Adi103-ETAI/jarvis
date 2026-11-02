# Phase 7 — Learning loop, self-editing memory, delegation

Date: 2026-10-29. Built after studying NousResearch/hermes-agent's actual
designs (delegation.md, loops.md, curator.md). Per D5: shapes borrowed,
nothing imported wholesale, everything passes the gates. Per D7: the
model proposes, the human authorizes; delegation adds restriction, never
permission; a delegate's output is evidence, not commands.

## What Hermes does (studied 2026-10-29)

- **Loops** (`/loop`): self-paced or fixed-cadence wakeups, each a real
  agent turn; guardrails are `--times N`, `loops.max_ticks` (default 100,
  pauses unattended token burn), one loop per session, idle-only wakeups,
  user messages take precedence; a model "judge" evaluates `--until`.
- **Curator**: two-phase skill lifecycle — deterministic transitions
  (unused 14d → stale, 30d → archived, no LLM) plus an OPT-IN LLM
  consolidation pass that runs in a background fork, previews with
  --dry-run, snapshots before touching anything, and never deletes (worst
  case: recoverable archive). Pinned/hub skills are off-limits.
- **Delegation** (`delegate_task`): children start with a FRESH
  conversation (zero parent history), inherit toolsets + workspace
  context; leaf subagents are BLOCKED from delegate_task / clarify /
  memory-writes / send_message / cronjob; only the final summary enters
  the parent's context; concurrency capped (10), iteration-limited
  (250), stall-monitored.

## What JARVIS adopts (and how it differs)

### 1. Learning loop — the ritual (Hermes loops + curator)

Trigger (deterministic, policy `[learning]`): after a compaction, or
every N completed console turns (`every_n_turns`, default 20). Guards,
each the analog of a Hermes guardrail:

| Hermes guardrail          | JARVIS guardrail                                   |
|---------------------------|----------------------------------------------------|
| `loops.max_ticks`         | `[learning] enabled` kill switch in policy         |
| idle-only wakeups         | ritual fires only AFTER a completed fg console turn |
| user precedence           | never mid-turn; one ritual in flight, ever         |
| `--times N` budget        | `review_hours = 20` — at most one ritual per window |
| curator "pause on hoard"  | `max_staged = 3` — pauses while proposals await you |

The ritual is ONE background kernel turn on the console session with a
fixed template (session evidence in: archived turn excerpts + journal
kind counts; instructions out: exact fenced formats). Its reply is
parsed deterministically (`parse_proposals`) for:

- ` ```skill-proposal ` blocks → `skills/_proposals/<name>.md` (invisible
  to index/match until approved; name kebab-sanitized, description
  required)
- ` ```memory-proposal ` blocks (`remember:` / `forget:` lines) →
  `memory_proposals.jsonl` staging

Malformed drafts are counted and dropped (`learning-ritual` journal,
`malformed=N`), never half-staged. Outcome is journalled and, when
something staged, announced with the review verbs.

### 2. Self-editing memory — staged, not written (Letta via D6/D7)

Letta/Hermes let the model edit memory. D6 refuses: facts are
subject-written. So the kernel's only "memory write" is a PROPOSAL the
human resolves:

    fact proposals        list staged changes
    fact approve ID       apply through the SAME FactStore path as
                          'remember' (source="console" — your approval
                          IS the subject's write; provenance journalled
                          as memory-op proposal-approved)
    fact reject ID        record refusal; the draft stays in the log

Staging (`memory/staging.py`) is append-only JSONL; resolved entries are
kept forever as audit. `skill approve NAME` works the same way on the
skills side — the only path from staging to live skills.

### 3. Delegation — `delegate TASK` (Hermes delegate_task, gated)

    delegate TASK      child session, foreground; evidence returns to you
    delegatebg TASK    background; announcer reports "delegate finished"
    delegates          list delegations and their state
    delegate-read SID  print a delegation's captured output

Design, mapped from Hermes:

- fresh child session on the same kernel (`delegate-<HHMMSS>`),
  registry-owned, watched by the same doorkeeper;
- child prompt = core-facts block + skills prefix + a frame that says
  its output is EVIDENCE (read, never executed), the human is not in the
  session (state assumptions, don't ask questions), and it may not
  delegate further;
- **STRICTER inheritance, enforced in the gate**: a new trust source
  `delegated` with `mutating = "confirm"` and `deny_actions = ["task"]`
  (Hermes' leaf-subagent law). `Policy.decide` checks deny_actions
  BEFORE the benign allowlist — inheritance can only remove permissions.
  The default (even with no stanza) denies `task` for delegated trust;
- journal lineage: `delegate-started` carries parent + child ids; the
  turn-started line is tagged `delegated=True`; `delegate-result` lands
  when the fg wait ends;
- output is returned verbatim to the console and journalled; the active
  layer never parses it into instructions (D3: work travels out,
  authority never does).

### Refused, on principle

- Hermes' model "judge" for loop termination — our guards are rules.
- Curator's auto-stale/auto-archive of skills — our lifecycle is human
  writes it, human approves a proposal, or it does not exist. (A usage
  counter is a D6-revision question, not a phase-7 build.)
- Children writing shared memory (Hermes blocks it too) — our children
  get read-only context only; FactStore refuses everything but the
  console.
- Parallel batch delegation (up to 10 children) — v0 is one child per
  `delegate` call; concurrency multiplies blast radius for no current
  need.

## Journal vocabulary (new)

`learning-ritual` (stage=dispatched|skipped|result, trigger, reason,
outcome, skills, memory, malformed, refused) · `skill-proposal`
(action=approve|reject, outcome) · `memory-op op=proposal-approved|
proposal-rejected` · `delegate-started|delegate-failed|delegate-result`.

## Tests

20 new hermetic tests in tests/unit/test_learning_unit.py (suite: 162
green): proposal parsing (both fences, malformed counting, empty),
staged-memory lifecycle (including audit survival), threshold/window/
max_staged/disabled/compaction/kernel-down guard matrix, one-ritual-
in-flight, stray-idle immunity, failed-turn handling, delegation (fg
evidence + trust wiring, bg announcer consumption, frame content,
kernel refusal, listings/readback), policy deny_actions (delegated
rejects task; shell still confirms; console unaffected), and the
console proposal flows for skills and facts.

## Honest limits

- The ritual runs bg on the console session; typing a new request before
  its "turn finished" narration can mix deltas into the parsed reply
  (one-ritual-at-a-time keeps the blast radius at one window).
- Proposal quality is the kernel's; the parser checks SHAPE only
  (fence, name, description, body). You are the quality gate.
- `delegate` fg blocks the console like any fg turn (240s cap, then
  "still running"); bg delegates narrate completion but the evidence is
  read back with `delegate-read`.
- No iteration cap / stall monitor on children yet (Hermes: 250 turns,
  heartbeat). The doorkeeper still parks every mutation for a human, so
  a runaway child can burn tokens but not act — worth a Phase 8+ watch.
- Live-kernel behavior untested (standing quota prerequisite), same as
  phases 5/6.
