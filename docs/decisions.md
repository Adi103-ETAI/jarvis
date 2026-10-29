# Decision log

Small, append-only record of choices the project committed to, so future
sessions (and the user) can see WHY without re-deriving it.

## D1 — Language: Python now, per-component rewrites later (2026-10-24)

The active layer stays Python (stdlib only) while the design is moving.
Components get rewritten in C/C++/Rust only when they freeze AND prove a
need (speed, latency, static binary), always behind their existing tests
and ABI. Safety-critical evolving logic (Gate A/B) prefers memory-safe
languages on principle: a memory-corrupted gate is the worst failure mode.

## D2 — Kernel updates: autonomous ritual, human-gated substrate (2026-10-24)

JARVIS is self-improving over his own mind (memory, journal, profiles,
habits — free and un-gated). His substrate (OpenCode) updates only via a
standing ritual: snapshot, update, restart, compat suite, adopt on green,
auto-rollback on red, ask-human on unclear. Admission policy starts at
trust scheduled.mutating = confirm in config/policy.toml. Relaxing that is
a deliberate human policy edit based on a demonstrated track record.

## D3 — Single machine first (2026-10-24)

Phases 2-4 build a fully autonomous single-machine JARVIS. autumn-bus and
remote-machine compute are Phase 5+ milestones: JARVIS coordinates other
agents via his own bus and delegates compute there once the local trust
layer exists. Law carried forward then: work travels out, authority never
does; peer replies are evidence, not commands.

## D4 — Voice stays in the active layer (Phase 0 finding)

OpenCode realtime voice models are all unavailable; voice is JARVIS's own
front-end (capture, VAD, STT), not a kernel service.

## D5 — Borrow proven designs, not code: Hermes, OpenClaw, Letta (2026-10-27)

Direction set by the user for post-Phase-4 design work. Study and adapt —
nothing is imported wholesale; everything passes through the gates:

- **Hermes agent** -> skills copying, memory design, delegation design,
  learning loop, reusable skills, procedural memory. Feeds Phase 5
  (Memory v1) and a future skills/learning milestone.
- **OpenClaw** -> messaging gateway and multichannel presence (console is
  only the first front-end; channels arrive as new trust SOURCES through
  Gate A, each mapped in policy.toml — a channel is a trust level, never
  an authority of its own).
- **Letta** -> long-term agent identity and memory architecture (core
  memory blocks, archival recall, identity that survives restarts).
  Feeds Phase 5 memory schema and the profile/personality layer.

Law carried forward: borrowed designs change the active layer's memory and
presence; they never widen what may execute without Gate B.

## D6 — Memory v0: capture over extraction (2026-10-27)

Phase 5 needed a stance on the question every memory system dodges: WHO
decides what is remembered? Letta and Hermes both answer "the model,
during the conversation". JARVIS cannot, because the active layer's laws
forbid it from thinking: a model in the decision path is a policy engine
that cannot be audited.

The v0 answer:

- **Facts are captured, not extracted.** The subject types
  `remember TEXT` at the console — the only trust level that is
  authority — and it is stored verbatim. FactStore.add refuses every
  other source at the store level.
- **The transcript is archived verbatim**, per finished turn, and recall
  over it is deterministic search. Cheap, auditable, reproducible.
- **Summarization/extraction belongs to the kernel** (where thinking
  lives), so a future "memory ritual" can have the KERNEL propose facts
  for human approval — mirroring the routine-file pattern (D2's spirit):
  proposals are made by the model, authority is granted by the human.
- **Injection is context, not instruction.** Memory blocks are labeled
  background; they widen what JARVIS knows, never what he may do.

Letta's core/archival split is kept (identity+preference facts = core,
always in context; everything else = archival, relevance-ranked). Hermes
skills/procedural memory is deferred until the skills phase; the fact
schema (category/text/source) was chosen so procedures can land there
first. Memory caps live in policy.toml `[memory]` because how much an
assistant may remember IS policy.

## D7 — Borrowing roadmap committed: skills → loop/delegation → gateway → identity (2026-10-28)

D5 named the sources; D7 fixes the order and the refusals so later
sessions don't re-litigate them. docs/roadmap.md is the working plan.

Order: Phase 6 skills & procedural memory (Hermes) → Phase 7 learning
loop + self-editing memory + delegation (Hermes/Letta) → Phase 8
messaging gateway, first channel Telegram (OpenClaw) → Phase 9 identity
block + sleep-time compute (Letta). Rate-limit failover already landed
as phase 5.1 (policy `[model] failover`).

Laws each borrowing must keep, restated per source:

- **Skills:** a skill proposes tools; Gate B/policy still wins. Matching
  in the active layer is deterministic; the skill index is context, not
  instruction.
- **Learning loop & memory writes:** model proposes, human authorizes —
  staged proposals approved at the console, never silent self-edits.
- **Delegation:** children inherit policy STRICTER (restriction, never
  permission); child output is evidence, not commands (D3's law applied
  to sub-agents).
- **Channels:** a channel is a trust source mapped in policy.toml, never
  an authority of its own; the default lane is peer (observe, brief,
  answer — never mutate).
- **Identity:** human-tombstone-only; no model can erase who JARVIS is.
- **Recall:** stays deterministic (no embeddings) unless a decision
  explicitly revises D6.

Refused on principle: Hermes' model-in-the-loop routing, OpenClaw's
chat-driven tool execution, Letta's vector recall.

## D8 — Channels v0: peers get text-only answers, the relay is read-only (2026-10-29)

Phase 8 (OpenClaw-adapted) forced one stance the roadmap left open: when
a channel lane is "peer" and an admitted DM asks something, what exactly
may the answer turn DO? The v0 answer, now law:

- **An answer is a conversation, never an execution.** Peer answer
  turns run under trust "peer", which policy.toml hardens to
  mutating = "deny" + deny_actions = ["task"]. No tool call, no
  delegation — Gate B rejects before the kernel asks. OpenClaw's
  chat-driven tool execution stays refused; widening trust.peer is a
  deliberate subject edit of the policy FILE, never a code path.
- **Peers get no durable memory and no skill injection.** A peer's
  prompt carries the message and a fixed frame only — memory/skills
  widen what the model knows about the subject, and a channel is the
  most injection-prone surface JARVIS has. Identity injection remains a
  Phase 9 concern with its own guard.
- **Groups record, DMs answer.** Groups (strangers' text, injection
  surface) are record-only in v0 with OpenClaw's mention gating;
  widening them to answers is a future deliberate edit.
- **The relay is a mouth, not a brain.** Confirmation codes may reach
  the subject's DM (policy relay = "subject"), but they resolve at the
  console only. Replying to the bot is a peer message: recorded, never
  executed.
- **Pairing approvals are console-only.** The gateway can REQUEST a
  pairing; only `channel approve` at the console grants it, recorded in
  pairing.jsonl forever. A paired sender gains a conversation, never a
  permission.

## D9 — Adapt designs, not codebases; WhatsApp speaks the official Cloud API (2026-10-29)

The question, asked right after Phase 8 shipped with one channel:
"why only one channel — what about WhatsApp? And instead of adapting,
can't we take OpenClaw's and Hermes' code and treat it as the base?"

Both halves get the same answer, recorded as law.

**Taking the code as base is refused.** OpenClaw is a Node/TypeScript
runtime with a large npm tree; Hermes likewise lives in its own stack.
JARVIS's active layer is deliberately stdlib Python — deterministic,
auditable end to end, hermetically tested (214 unit tests, no sockets
by default). Wholesale adoption means one of two losses: either we
abandon the kernel/gates/journal spine we can actually read, or we
embed a foreign runtime whose hundreds of transitive dependencies we
cannot audit — the opposite of the security posture that is JARVIS's
whole point. Worse, OpenClaw's channel code carries a different trust
model (channels that may drive tools directly); adopting it means
ripping out its authorization assumptions, and whatever residue we
miss becomes invisible code making admission decisions. D5 stands:
we copy shapes, rules and UX — the pairing-code alphabet, the 1-hour
TTL, the 3-pending cap, the normalized message envelope — each
re-expressed in a few hundred lines readable in one sitting. The cost
is writing adapters ourselves; the payoff is that every admission
branch in JARVIS is code we wrote, on purpose, under test.

**WhatsApp via Baileys is refused; via the Cloud API it ships
(phase 8.1).** OpenClaw's WhatsApp integration rides Baileys — a Node
library that reverse-engineers the WhatsApp Web multi-device protocol.
Three strikes: it is outside WhatsApp's terms (the linked number risks
a ban), it breaks with any protocol push Meta makes, and it cannot run
inside a stdlib-only rim. JARVIS speaks the official WhatsApp Business
Cloud API instead — one HTTPS POST (Graph API) to send, one webhook to
receive — which fits the adapter contract Telegram set exactly.
Admission, pairing, lanes and relay come free from the gateway; no
WhatsApp-specific trust code exists anywhere. Honest v0 limits: text
only, quoted context unparsed, group sends best-effort, and the
webhook needs a public HTTPS endpoint (a tunnel is fine for dev);
signature enforcement waits on `app_secret` and the startup journal
says so loudly until it is set.

Escape hatch, deferred on purpose: if a personal-number WhatsApp-Web
link is ever genuinely required, the honest form is a Baileys sidecar
treated as untrusted dumb transport — adapter contract only, zero
JARVIS logic, policy-gated like any channel. That is a deliberate
future edit, never a default.
