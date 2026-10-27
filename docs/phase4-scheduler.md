# Phase 4 — minimal proactivity (T1 scheduler)

Date: 2026-10-27. Deliverable: the first component that acts **without a
person present** — one clock trigger (T1), one human-approved routine (the
08:00 morning briefing), and the cycle guards that make repetition safe.

## 1. What now exists

| Piece | File | Job |
|---|---|---|
| Scheduler | src/jarvis/scheduler.py | background thread: watches the clock, fires approved routines through the SAME spine (Gate A -> kernel turn -> Gate B -> codes) |
| Routine file | config/routines.toml | THE approval artifact: a routine exists because the human wrote it here |
| Routine session | scheduler-managed | dedicated `jarvis-routines` kernel session — routine turns never interleave with console turns; registry-owned, so Gate B doorkeeps it with trust="scheduled" |
| Runtime controls | console commands | `routines` (list + state), `run NAME` (manual fire; counts as today's), `routine NAME off\|on` (tighten-only) |
| Notifications | announcer + DND | routine start/finish/failure lines through the announcer; DND window silences routine chatter ONLY |

## 2. The law of the routine file

- **A routine is policy, not a runtime decision.** The scheduler never
  authors or edits tasks; it only reads `config/routines.toml`.
- **Runtime may only tighten.** `routine NAME off` always works; `on` is
  refused for file-disabled and auto-disabled routines — clearing those
  requires editing the file (human review, per D2's spirit).
- **Gate B still owns execution.** Routine turns run with
  `trust.scheduled` -> policy.toml keeps `mutating = "confirm"`: any
  non-allowlisted action in a briefing turn parks with a one-time code
  exactly like a console turn. The briefing task is read-and-report by
  design; its mutability stays human-gated.

## 3. Cycle guards (acceptance: "repeat/failure events do not loop")

| Guard | Mechanism |
|---|---|
| One fire per calendar day | `_fired_dates` in memory + `routine-started` journal entries — restarts cannot re-fire (journal is read back on boot) |
| No overlapping turns | busy until the kernel reports idle on the routine session (events, not `active_sessions` — phase 1 finding #4) |
| Runaway budget | `max_run_minutes` watchdog interrupts the turn (own session -> subject rule allows); counted as a failure |
| Cooldown | `cooldown_minutes` between launches, belt-and-braces under the daily dedupe |
| Failure auto-disable | N consecutive failures -> routine disabled + "human review needed" notice; never self-retries |
| DND | quiet window suppresses routine notifications; confirmation codes are NEVER suppressed (safety outlouds DND) |
| Kernel down | ticks survive; launches are retried on later ticks; nothing is marked fired until the turn actually starts |

## 4. The 08:00 briefing

`config/routines.toml` ships one enabled routine: `morning-briefing`,
daily 08:00, read-and-report task over `.jarvis/journal.jsonl` and
`.jarvis/gate-log.jsonl` (asks done / pending codes / failures / one focus
for the day). Edit the file to change the task, the time, or to add
routines; that edit IS the approval. A commented-out
`kernel-update-ritual` stub marks where D2's standing job will land once
its compat suite exists.

## 5. Honest limits (v0)

- The scheduler lives **inside the console process**: `python3 -m jarvis`
  must be running for routines to fire. Daemon/service lifecycle is a
  later phase (D3: single machine first; build order phase 10).
- Schedule format is `daily HH:MM` only; intervals/weekdays come when a
  routine needs them.
- Turn completion relies on the kernel event vocabulary
  (`session.idle`, `session.execution.failed`) — best-effort by design
  (phase 1: kernel event names are not an API we control).

## 6. Bugs fixed en route (caught by the new tests)

- `ensure_session` could leak `KernelError` uncaught from `ask()` — kernel
  down at console start would crash on the first typed line; now returns a
  friendly "kernel unreachable" line.
- Bare `routine` (no args) fell through to `ask` instead of showing usage.
- EventPipe thread died noisily when the kernel is unreachable; SSE
  failures now end the pipe quietly (reconnect is a later-phase concern).
- Test fixture leak: console tests could have fired a REAL routine against
  a live kernel on the user's machine — fixture now injects an empty
  routines file. Tests must never launch routines.

## 7. Test status

tests/test_scheduler_unit.py — 25 passed (schedule/DND math, config load
and broken-config safety, once-per-day + restart dedupe, cooldown/disabled
gates, dedicated session + scheduled trust, manual-run dedupe, failure
auto-disable + reset, watchdog interrupt + young-turn tolerance, event
failure/success/foreign-session handling, DND suppression, tighten-only
toggles, listing, kernel-down tick survival). Console routing tests
extended (`routines` / `run` / `routine`). Full hermetic suite: 72/72.

Live E2E on real kernel still pending quota reset (unchanged); the
scheduler adds no new kernel operations — everything it calls
(create/interrupt/prompt/wait) is already covered by phase 1/2 live tests.
