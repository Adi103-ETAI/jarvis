# Phase 8 — Messaging gateway & multichannel presence

Date: 2026-10-29. Built after studying OpenClaw's actual design
(docs.openclaw.ai channel/pairing/groups docs, gateway architecture
write-ups, the security-hardening guide). Per D5: shapes borrowed,
nothing imported wholesale, everything passes the gates. Per D7: a
channel is a trust SOURCE mapped in policy.toml, never an authority of
its own; the default lane is peer — observe, brief, answer, never
mutate.

## What OpenClaw does (studied 2026-10-29)

- **One gateway per host.** A single WebSocket control plane
  (`127.0.0.1:18789` by default, loopback-only) sits between every
  messaging platform and the agent runtime. Event-driven, one process
  per host; binding beyond loopback requires an explicit auth token and
  is treated as an intentional act, never a default. The interface
  layer (where messages come from) is fully separated from the runtime
  (where intelligence lives): personality, memory and tools are
  configured once; channels are delivery mechanisms.
- **Channels are adapters with four duties**: authentication, inbound
  parsing, access control, outbound formatting (markdown→platform
  markup, chunk limits, streaming modes). Telegram runs long polling by
  default (webhook optional), with a bot token from BotFather.
- **Sessions ARE security boundaries.** The main operator session
  (`agent:<id>:main`) runs tools natively on the host; DM sessions
  (`agent:<id>:<channel>:dm:<identifier>`) and group sessions
  (`agent:<id>:<channel>:group:<id>`) are sandboxed by default
  (`sandbox.mode: "non-main"`, Docker backend). One brain, two
  execution postures: DMs full tools, groups restricted.
- **DM access control — pairing.** `dmPolicy: "pairing"` (default):
  an unknown sender gets an 8-character code (uppercase, ambiguous
  chars 0O1I excluded), valid 1 hour; their message is NOT processed
  until the operator approves on the host (`openclaw pairing approve
  telegram CODE`); pending requests capped at 3 per sender; approvals
  land in a durable allow-store. `dmPolicy: "allowlist"` for known
  senders only; `"open"` is explicitly discouraged.
- **Group access control — allowlist + mention gating.** Evaluation
  order: `groupPolicy` (allowlist default, empty = blocked, or
  open/disabled) → group allowlists (`groupAllowFrom`, numeric
  Telegram ids only) → mention gating (`requireMention: true`
  default; regex `mentionPatterns` as fallback; a reply-to-the-bot
  counts as an implicit mention). Unmentioned group messages are
  stored for context only, never answered.
- **Replies route back through the gateway** to the originating
  channel: chunks formatted per platform, visible replies automatic.
- **Threat model.** Untrusted channel content is hostile (prompt
  injection is a content risk, not just a sender risk); context
  filtering (`contextVisibility`), tool profiles, sandboxing and
  allowlists all exist because a chat message can try to steer the
  agent.

## What JARVIS adopts (and how it differs)

### 1. jarvisd IS the gateway — channels plug into the existing spine

No new daemon (D3). `src/jarvis/channels/gateway.py` holds a
`ChannelGateway`: config-driven adapters, one poller thread per enabled
channel, an admission pipeline, and outbound `send()`. The console
remains the spine: admitted messages cross Gate A, turns run on
JARVIS-owned sessions, the doorkeeper (Gate B) watches every tool call.
The announcer already exists; channel targeting is wired into it, not
rebuilt.

### 2. The channel stanza — trust source, not authority (D5/D7)

```toml
[channels.telegram]
enabled = false            # channels are OFF until you write policy
lane = "peer"              # peer | observe  (never more, see refusals)
dm_policy = "pairing"      # pairing | allowlist | disabled
allow_from = []            # numeric ids YOU approved by writing them here
group_policy = "disabled"  # groups OFF by default
group_allow_from = []
require_mention = true
mention_patterns = ['\bjarvis\b']
relay = "off"              # off | subject  (briefs + code notices outbound)
subject = ""               # your own numeric id; required for relay
token = ""                 # or JARVIS_TELEGRAM_TOKEN env, or token_file
```

Invalid/missing values fall back to the SAFE choice (lane→observe,
dm_policy→disabled, group_policy→disabled, relay→off): a typo in
policy can never widen trust. `allow_from` is OpenClaw's allowFrom as a
policy stanza — the file IS the approval, same law as routines.toml and
the failover chain.

### 3. Pairing — OpenClaw's codes, console-side approval

Unknown DM sender under `dm_policy = "pairing"`: an 8-char code
(alphabet without 0O1I), 1-hour TTL, max 3 pending requests per
channel, message NOT processed, no repeat offers while one is pending.
The code is approved at the CONSOLE only — `channel approve telegram
CODE` — because the console is the only mutating surface. Approvals
persist in `pairing.jsonl` (beside the journal, append-only, audit
forever); they add a SENDER to the DM allow-set, never a permission.
`channel forget telegram <id>` revokes (runtime may tighten, never
widen).

### 4. Sessions named like OpenClaw's, owned like ours

DM answer turns run on `dm:telegram:<peer_id>` titled sessions. The
kernel session id is registry-owned as always, so the subject rule
(interrupt/delete/model-switch on JARVIS-owned sessions only) is
unchanged — a channel peer can never reach a session JARVIS did not
open. After a console restart the gateway re-adopts dm sessions from
the journal, and rebuilds cleanly if the kernel lost them.

### 5. The peer lane — answer = text, never tools

Admitted DM + `lane = "peer"`: Gate A classifies (new `lane` axis:
`answer` lane → verdict "act" with an auditable reason), the turn runs
bg on the peer's dm session with trust "peer", and the reply text
routes back out through the gateway. Policy hardens trust.peer
(`mutating = "deny"`, `deny_actions = ["task"]`): an answer turn is
conversation, not execution — OpenClaw's chat-driven tool execution is
refused, exactly as the roadmap planned. Peer turns get NO durable
memory and NO skill injection (prompt-injection surface minimization;
identity injection stays a Phase 9 concern). `lane = "observe"` records
and never launches a turn. Flipping trust.peer back to "confirm" is a
deliberate subject edit that would re-route mutation codes to the
console announcer — nothing in code does it.

### 6. Groups — record-only in v0

`group_policy = "disabled"` by default. With `"allowlist"`: admitted
groups' messages are journalled — mention-gated
(`requireMention`/`mention_patterns`/reply-to-bot) into context vs
drop — and no turn is ever launched from a group. OpenClaw sandboxes
group sessions because groups are where strangers inject; our
equivalent posture is stronger: groups observe, DMs answer.

### 7. Relay — the announcer grows a channel mouth (read-only)

`relay = "subject"`: confirmation notices ("awaiting you CODE ...") and
routine notifications also go to the subject's DM — with the standing
instruction that codes are resolved at the console. Resolution/expiry
lines follow on the same channel, so a relayed code visibly expires
there. The relay is READ-ONLY by construction: replying "yes CODE" to
the bot is a peer message — recorded, never executed.

## What is refused (and why)

| OpenClaw capability                              | JARVIS verdict                                  |
|--------------------------------------------------|-------------------------------------------------|
| Chat-driven tool execution                        | Refused. Peer turns are text-only: trust.peer denies mutation and task-spawning; Gate B rejects before the kernel does |
| `dmPolicy: "open"`                                | Refused. pairing/allowlist/disabled only        |
| Model/personality-per-channel, multi-agent routing| Refused for now — one brain, lanes are policy   |
| Group answer turns                                | Deferred. Groups record-only in v0 (widening is a deliberate future edit, not a default) |
| Webhook transport (Telegram)                      | Deferred. Long polling only (no public listener to harden). WhatsApp adopts the official Cloud API webhook below — one listener, signature-checked |
| Media, streaming drafts, typing indicators        | Deferred. Text in, chunked text out (Telegram 4096 limit) |
| Approvals resolved from chat                      | Refused. Codes relay to the subject read-only; the console resolves |

## Build

- `src/jarvis/channels/gateway.py` — ChannelConfig loader (safe
  fallbacks), PairingStore (JSONL), ChannelGateway (admission, dm
  sessions, relay watcher, outbound).
- `src/jarvis/channels/telegram.py` — TelegramAdapter: stdlib Bot API
  over HTTPS, injectable transport (hermetic tests), long-poll loop,
  sendMessage chunking, retry/backoff, never-fatal poller.
- `console/app.py` wiring — gateway built from policy, attached to the
  ONE event pipe, started/stopped with the console; `ask()` gains the
  Gate A lane axis; `_emit` feeds the relay.
- `console/commands.py` — `channels`, `channel pairings|approve|reject|
  forget|say`; HELP updated.
- `gates/intent.py` — classify(text, source, lane): peer+answer lane is
  an auditable "act"; peers without it stay journal-only.
- policy.toml — `[channels.telegram]` stanza; trust.peer hardened to
  deny (documented deliberate-edit path).

Journal kinds: `channel-message` (admitted dm/group, with admission
source), `channel-drop` (with reason), `channel-pairing`
(requested/approved/rejected/forgotten), `dm-session-opened/adopted`,
`channel-relay` (answer delivered), `channel-relay-failed`,
`channel-say`, `channel-send-failed`.

## Second channel: WhatsApp — the official Cloud API (phase 8.1)

OpenClaw's WhatsApp support rides Baileys — a Node library that
reverse-engineers the WhatsApp Web protocol. That path is refused
(D9): it sits outside WhatsApp's terms (the linked number risks a
ban), breaks with any protocol push, and cannot live in a stdlib rim.
JARVIS speaks the official **WhatsApp Business Cloud API** instead:
one HTTPS POST (Graph API) to send, one webhook to receive.

- `src/jarvis/channels/whatsapp.py` — `WhatsAppCloudAdapter`:
  `normalize_webhook` (Meta payload → the same normalized message
  shape Telegram emits; text only; statuses/media dropped; groups
  recognized by group id / `@g.us`), `send_message` (Graph API
  `/messages`, chunked at 4000), `run_loop` (a `ThreadingHTTPServer`
  webhook receiver until stop: GET = `hub.challenge` verify handshake,
  POST = `X-Hub-Signature-256` check when `app_secret` is set, then
  normalize → dedupe by message id → on_message one at a time behind a
  lock — the spine keeps its single-threaded discipline).
- Zero WhatsApp-specific trust code: unknown senders get the same
  pairing code, `channel approve` at the console, peer lane = text-only
  answers, groups record-only, relay = read-only mouth. The verify
  token and signature check are AUTHENTICATION (the wire, not the
  law) — a forged message that passed them still faces the admission
  pipeline like every other channel.
- Config: `[channels.whatsapp]` stanza; channel-specific keys
  (`phone_number_id`, `app_secret`, `webhook_port`, `webhook_path`,
  `webhook_verify_token`, `api_version`) ride in `cfg.options` and the
  adapter applies safe fallbacks — a typo cannot widen trust.
- Going live: Meta app with the WhatsApp product → copy
  `phone_number_id` → permanent access token (`JARVIS_WHATSAPP_TOKEN`)
  → expose the webhook publicly (ngrok/cloudflared tunnel is fine for
  dev) → subscribe Meta to the callback URL with the verify token →
  set `app_secret` so signatures are enforced (startup notes journal
  the dev-mode warning until you do).
- Honest v0 limits: text only (media/template/interactive dropped),
  quoted-reply context unparsed, group sends best-effort, webhook
  requires a public HTTPS endpoint.

## Acceptance (unit, all hermetic)

- Adapter normalizes updates, advances the long-poll offset, chunks
  long sends, survives transport errors.
- DM admission matrix: policy allowlist / pairing / disabled × known /
  unknown sender; pairing TTL, pending cap, no-repeat-offer, console
  approval path, forget-revocation; store persists across restarts.
- Group admission matrix: policy × allowlist × mention (pattern,
  reply-to-bot, none).
- Gate A: peer answer lane acts, observe journals; trust.peer denies a
  mutating tool through the real Policy.
- Gateway↔console wiring: an admitted DM with lane=peer launches one
  bg turn on a dm: session with trust peer, and the captured reply
  routes back out; observe launches nothing.
- Relay filter: code notices and routine lines reach the subject,
  chatter does not.
- WhatsApp: webhook normalize (dm/group/status/media), signature
  check, verify handshake, redelivery dedupe, Graph error surfacing,
  options passthrough with safe fallbacks, factory branch, and the
  end-to-end law — an unknown WhatsApp sender gets a pairing code,
  never a turn. (21 new tests, 214 total.)
