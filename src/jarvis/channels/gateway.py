"""Channel gateway — OpenClaw-adapted multichannel presence (phase 8).

OpenClaw's insight: ONE control plane between every platform and the
agent; channels are delivery mechanisms; sessions are security
boundaries. JARVIS already has the control plane — the console spine
(Gate A -> kernel turn -> Gate B doorkeeper -> announcer). The gateway
adds the delivery rim around it:

    poller thread   adapter long-poll -> normalized updates (per channel)
    admission       policy-driven: who may talk, in which lane
                    (pairing / allowFrom / group allowlist / mentions)
    on_message      the ONE callback into the console spine
    relay watcher   bg answer turns -> reply text back to the channel
    send()          outbound, chunked, best-effort, journalled

Laws (D5/D7): a channel is a trust SOURCE, never an authority. Every
admission decision is policy mechanics that lives HERE (visible,
testable); every trust/turn decision lives in the console spine (Gate
A/B). Configuration falls back to the SAFE choice on any invalid value
— a typo in policy.toml can widen nothing.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.channels.telegram import TelegramAdapter
from jarvis.channels.whatsapp import WhatsAppCloudAdapter

# OpenClaw's pairing-code shape: 8 chars, uppercase, no ambiguous 0O1I.
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

LANES = ("peer", "observe")
DM_POLICIES = ("pairing", "allowlist", "disabled")
GROUP_POLICIES = ("disabled", "allowlist")
RELAYS = ("off", "subject")

PAIRING_TEXT = ("JARVIS pairing — I don't answer unknown senders. Your code is "
                "{code}. Give it to the operator: they must run "
                "`channel approve {channel} {code}` at the JARVIS console. "
                "It expires in {minutes} minutes. Your message was not processed.")

RELAY_HINT = "\n(resolve at the JARVIS console — replies here are recorded, never executed)"


# ------------------------------------------------------------------- config

@dataclass
class ChannelConfig:
    """One channel as the human wrote it in policy [channels.<name>].

    Every field is policy; runtime only tightens. Invalid values fall
    back to the safe choice at load time (see load_channel_configs).
    """
    name: str
    enabled: bool = False
    lane: str = "peer"             # peer | observe
    dm_policy: str = "pairing"     # pairing | allowlist | disabled
    allow_from: frozenset = field(default_factory=frozenset)
    group_policy: str = "disabled"
    group_allow_from: frozenset = field(default_factory=frozenset)
    require_mention: bool = True
    mention_patterns: tuple = ()   # compiled, case-insensitive
    pairing_ttl_seconds: float = 3600.0
    max_pending_pairings: int = 3
    relay: str = "off"             # off | subject
    subject: str = ""              # the subject's own peer id (relay target)
    token: str = ""
    poll_timeout: float = 25.0
    options: dict = field(default_factory=dict)  # channel-specific keys
    # (whatsapp: phone_number_id, app_secret, webhook_*; adapters own
    # their options with safe fallbacks — the gateway never reads them)


def _one_of(value, choices, default, safe, note=None, label=""):
    """Missing/empty -> the documented default; present-but-invalid ->
    the SAFE choice. A typo in policy must never widen trust."""
    v = str(value or "").strip()
    if not v:
        return default
    if v in choices:
        return v
    if note is not None:
        note(f"channel {label}: invalid '{v}' -> falling back to '{safe}'")
    return safe


def _compile_patterns(raw, note=None, label=""):
    out = []
    for p in (raw or []):
        try:
            out.append(re.compile(str(p), re.IGNORECASE))
        except re.error:
            if note is not None:
                note(f"channel {label}: invalid mention pattern {p!r} skipped")
    return tuple(out)


_KNOWN_CHANNEL_KEYS = frozenset({
    "enabled", "lane", "dm_policy", "allow_from", "group_policy",
    "group_allow_from", "require_mention", "mention_patterns",
    "pairing_ttl_seconds", "max_pending_pairings", "relay", "subject",
    "token", "token_file", "poll_timeout",
})


def load_channel_configs(raw: dict, journal=None, env=None) -> list[ChannelConfig]:
    """[channels.*] policy tables -> ChannelConfig list (safe fallbacks).

    Token resolution order: policy `token` > env JARVIS_<NAME>_TOKEN >
    `token_file` contents. A channel that resolves to no token is still
    returned (enabled, tokenless) — the gateway reports it as not
    started rather than silently hiding a half-configured channel.
    """
    import os
    env = env if env is not None else os.environ
    notes = []

    def note(msg):
        notes.append(msg)
        if journal is not None:
            journal.add("channel-config", note=msg)

    cfgs = []
    for name, body in ((raw or {}).get("channels") or {}).items():
        if not isinstance(body, dict):
            note(f"channel {name}: stanza is not a table; ignored")
            continue
        label = str(name)
        cfg = ChannelConfig(name=label)
        cfg.enabled = bool(body.get("enabled", False))
        cfg.lane = _one_of(body.get("lane"), LANES, "peer", "observe", note, label)
        cfg.dm_policy = _one_of(body.get("dm_policy"), DM_POLICIES, "pairing",
                                "disabled", note, label)
        cfg.group_policy = _one_of(body.get("group_policy"), GROUP_POLICIES,
                                   "disabled", "disabled", note, label)
        cfg.relay = _one_of(body.get("relay"), RELAYS, "off", "off", note, label)
        cfg.allow_from = frozenset(str(x) for x in (body.get("allow_from") or []))
        cfg.group_allow_from = frozenset(str(x) for x in
                                         (body.get("group_allow_from") or []))
        cfg.require_mention = bool(body.get("require_mention", True))
        cfg.mention_patterns = _compile_patterns(body.get("mention_patterns"),
                                                 note, label)
        try:
            cfg.pairing_ttl_seconds = float(body.get("pairing_ttl_seconds", 3600))
        except (TypeError, ValueError):
            cfg.pairing_ttl_seconds = 3600.0
        try:
            cfg.max_pending_pairings = max(1, int(body.get("max_pending_pairings", 3)))
        except (TypeError, ValueError):
            cfg.max_pending_pairings = 3
        try:
            cfg.poll_timeout = max(1.0, float(body.get("poll_timeout", 25)))
        except (TypeError, ValueError):
            cfg.poll_timeout = 25.0
        cfg.subject = str(body.get("subject", "") or "").strip()
        # Unknown stanza keys are the channel's own dialect; they pass
        # through untouched and each adapter applies safe fallbacks.
        cfg.options = {k: v for k, v in body.items()
                       if k not in _KNOWN_CHANNEL_KEYS}

        token = str(body.get("token", "") or "").strip()
        if not token:
            token = str(env.get(f"JARVIS_{label.upper()}_TOKEN", "") or "").strip()
        if not token and body.get("token_file"):
            try:
                token = Path(str(body["token_file"])).read_text(
                    encoding="utf-8").strip()
            except OSError as e:
                note(f"channel {label}: token_file unreadable ({e})")
        cfg.token = token
        cfgs.append(cfg)
    return cfgs


# ------------------------------------------------------------ pairing store

def new_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))


class PairingStore:
    """Durable record of DM pairing requests and decisions.

    Append-only JSONL beside the journal (audit forever, survives
    restarts). The gateway can only REQUEST; approve/reject/forget are
    console-side — the human at the console is the only authority, and
    the store only records what was decided. An approval adds a SENDER
    to the DM allow-set; it never grants a permission.
    """

    def __init__(self, path, journal=None):
        self.path = Path(path)
        self.journal = journal
        self._pending: dict[tuple, dict] = {}     # (channel, code) -> entry
        self._approved: dict[tuple, dict] = {}    # (channel, peer) -> entry
        self._rebuild()

    # ------------------------------------------------------------- internals

    def _append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(_json_line(entry) + "\n")

    def _rebuild(self) -> None:
        self._pending.clear()
        self._approved.clear()
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                e = _load_line(line)
            except ValueError:
                continue
            kind = e.get("kind")
            key = (e.get("channel"), e.get("code"))
            if kind == "request":
                self._pending[key] = e
            elif kind in ("approved", "rejected"):
                self._pending.pop(key, None)
                if kind == "approved":
                    self._approved[(e.get("channel"), e.get("peer"))] = e
            elif kind == "forgotten":
                self._approved.pop((e.get("channel"), e.get("peer")), None)

    # ------------------------------------------------------------------- api

    def request(self, channel: str, peer: str, name: str,
                ttl_seconds: float = 3600.0,
                max_pending: int = 3) -> tuple[str | None, str]:
        """Register (or find) a pairing request. Returns (code, status):
        ("CODE", "created") | ("CODE", "pending") | (None, "capped").

        An open, unexpired request for the same sender is reused (no
        repeat offers — OpenClaw offers roughly once per hour). Expired
        requests are garbage-collected on touch.
        """
        now = time.time()
        for key, e in list(self._pending.items()):
            if e.get("channel") != channel:
                continue
            if now - float(e.get("ts", 0)) > ttl_seconds:
                del self._pending[key]          # expired; file row stays (audit)
                continue
            if e.get("peer") == peer:
                return str(e.get("code")), "pending"
        open_count = sum(1 for e in self._pending.values()
                         if e.get("channel") == channel)
        if open_count >= max_pending:
            return None, "capped"
        code = new_pairing_code()
        entry = {"ts": now, "kind": "request", "channel": channel,
                 "peer": peer, "code": code, "name": (name or "")[:80]}
        self._pending[(channel, code)] = entry
        self._append(entry)
        return code, "created"

    def _open(self, channel: str, code: str, ttl_seconds: float) -> dict | None:
        e = self._pending.get((channel, code))
        if e is None:
            return None
        if time.time() - float(e.get("ts", 0)) > ttl_seconds:
            del self._pending[(channel, code)]
            return None
        return e

    def approve(self, channel: str, code: str,
                ttl_seconds: float = 3600.0) -> dict | None:
        e = self._open(channel, code, ttl_seconds)
        if e is None:
            return None
        row = dict(e, kind="approved", ts=time.time())
        self._pending.pop((channel, code), None)
        self._approved[(channel, e["peer"])] = row
        self._append(row)
        return row

    def reject(self, channel: str, code: str,
               ttl_seconds: float = 3600.0) -> dict | None:
        e = self._open(channel, code, ttl_seconds)
        if e is None:
            return None
        row = dict(e, kind="rejected", ts=time.time())
        self._pending.pop((channel, code), None)
        self._append(row)
        return row

    def forget(self, channel: str, peer: str) -> bool:
        if (channel, peer) not in self._approved:
            return False
        self._approved.pop((channel, peer), None)
        self._append({"ts": time.time(), "kind": "forgotten",
                      "channel": channel, "peer": peer})
        return True

    def is_approved(self, channel: str, peer: str) -> bool:
        return (channel, peer) in self._approved

    def pending(self, channel: str | None = None) -> list[dict]:
        now = time.time()
        out = []
        for (ch, code), e in list(self._pending.items()):
            if channel is not None and ch != channel:
                continue
            if now - float(e.get("ts", 0)) > 3600.0:   # listing uses the default TTL
                continue
            out.append(dict(e, age_s=round(now - float(e.get("ts", 0)), 1)))
        return sorted(out, key=lambda e: e.get("ts", 0))

    def approved_peers(self, channel: str) -> list[str]:
        return [p for (ch, p) in self._approved if ch == channel]


def _json_line(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=True, sort_keys=True)


def _load_line(line: str) -> dict:
    e = json.loads(line)
    if not isinstance(e, dict):
        raise ValueError("not an object")
    return e


# ------------------------------------------------------------------ gateway

class ChannelGateway:
    """The delivery rim around the console spine (phase 8).

    Owns: per-channel poller threads, the admission pipeline, the dm
    session map, the answer-relay watcher, and outbound sends. The
    console provides `on_message` (spine entry), `collect_text`
    (replay_text) and the journal; the gateway never launches turns,
    never decides trust, and never sees the kernel.
    """

    def __init__(self, configs, *, on_message, journal, notify=None,
                 pairing_store: PairingStore | None = None, collect_text=None,
                 adapter_factory=None):
        self.cfgs: dict[str, ChannelConfig] = {c.name: c for c in configs}
        self.on_message = on_message    # (cfg, kind, peer_id, msg, admission)
        self.journal = journal
        self.notify = notify or (lambda line: None)
        self.pairings = pairing_store
        self.collect_text = collect_text or (lambda sid, start: "")
        self._factory = adapter_factory or _default_factory
        self.adapters: dict[str, object] = {}       # name -> running adapter
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._dm_sessions: dict[tuple, str] = {}    # (channel, peer) -> kernel sid
        self._relays: dict[str, dict] = {}          # sid -> {channel, chat_id, start}

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        for cfg in self.cfgs.values():
            if not cfg.enabled:
                continue
            adapter = self._factory(cfg)
            if adapter is None:
                self.journal.add("channel-config", channel=cfg.name,
                                 note="enabled but credentials incomplete — not started")
                self.notify(f"[jarvis] channel {cfg.name} enabled but not started "
                            "— missing token/credentials "
                            f"(JARVIS_{cfg.name.upper()}_TOKEN, docs/phase8-gateway.md)")
                continue
            for note in (getattr(adapter, "startup_notes", list)() or []):
                self.journal.add("channel-config", channel=cfg.name, note=note)
            self.adapters[cfg.name] = adapter
            t = threading.Thread(target=self._poll_loop, args=(adapter, cfg),
                                 name=f"jarvis-channel-{cfg.name}", daemon=True)
            self._threads.append(t)
            t.start()
            self.journal.add("channel-started", channel=cfg.name, lane=cfg.lane,
                             dm_policy=cfg.dm_policy, group_policy=cfg.group_policy)
            self.notify(f"[jarvis] channel {cfg.name} started (lane {cfg.lane}, "
                        f"dm {cfg.dm_policy}, groups {cfg.group_policy})")

    def stop(self, join_seconds: float = 2.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=join_seconds)
        self._threads = []

    def _poll_loop(self, adapter, cfg) -> None:
        # One thread per channel; adapter.run_loop already swallows
        # per-message and per-poll errors (never-fatal presenter law).
        adapter.run_loop(lambda msg: self.handle_update(cfg, msg), self._stop)

    # ------------------------------------------------------------- admission

    def handle_update(self, cfg: ChannelConfig, msg: dict) -> None:
        """Normalized update -> admission decision. The ONLY place
        channel access control lives; every branch journals its reason.
        """
        if str(msg.get("chat_type", "")) == "private":
            self._admit_dm(cfg, msg)
        else:
            self._admit_group(cfg, msg)

    def _admit_dm(self, cfg: ChannelConfig, msg: dict) -> None:
        peer = str(msg.get("sender_id", ""))
        chat_id = str(msg.get("chat_id", ""))
        if peer and peer in cfg.allow_from:
            self.on_message(cfg, "dm", chat_id, msg, "policy")
            return
        if self.pairings is not None and self.pairings.is_approved(cfg.name, peer):
            self.on_message(cfg, "dm", chat_id, msg, "pairing")
            return
        if cfg.dm_policy == "pairing" and self.pairings is not None:
            code, status = self.pairings.request(
                cfg.name, peer, str(msg.get("sender_name", "")),
                ttl_seconds=cfg.pairing_ttl_seconds,
                max_pending=cfg.max_pending_pairings)
            if status == "created":
                self.journal.add("channel-pairing", action="requested",
                                 channel=cfg.name, peer=peer, code=code)
                self.send(cfg.name, chat_id, PAIRING_TEXT.format(
                    code=code, channel=cfg.name,
                    minutes=max(1, int(cfg.pairing_ttl_seconds // 60))))
                return
            reason = "pairing-capped" if status == "capped" else "pairing-pending"
            self._drop(cfg, msg, reason)
            return
        self._drop(cfg, msg, f"dm-policy-{cfg.dm_policy}")

    def _admit_group(self, cfg: ChannelConfig, msg: dict) -> None:
        if cfg.group_policy != "allowlist":
            self._drop(cfg, msg, f"group-policy-{cfg.group_policy}")
            return
        chat_id = str(msg.get("chat_id", ""))
        if chat_id not in cfg.group_allow_from:
            self._drop(cfg, msg, "group-not-allowlisted")
            return
        admitted = "mentioned" if self._mentioned(cfg, msg) else "context"
        # Groups are record-only in v0: the spine journals admitted group
        # messages but never launches a turn from them (phase 8 refusal).
        self.on_message(cfg, "group", chat_id, msg, admitted)

    def _mentioned(self, cfg: ChannelConfig, msg: dict) -> bool:
        if not cfg.require_mention:
            return True
        text = str(msg.get("text", ""))
        adapter = self.adapters.get(cfg.name)
        if adapter is not None:
            bot_id = getattr(adapter, "bot_id", None)
            bot_name = getattr(adapter, "bot_username", None)
            if bot_id and msg.get("reply_to_sender_id") == bot_id:
                return True
            if bot_name and f"@{bot_name}".lower() in text.lower():
                return True
        return any(rx.search(text) for rx in cfg.mention_patterns)

    def _drop(self, cfg: ChannelConfig, msg: dict, reason: str) -> None:
        self.journal.add("channel-drop", channel=cfg.name, reason=reason,
                         peer=str(msg.get("sender_id", "")),
                         name=str(msg.get("sender_name", ""))[:80])

    # ------------------------------------------------------------- dm sessions

    def dm_session(self, channel: str, peer: str) -> str | None:
        return self._dm_sessions.get((channel, peer))

    def remember_dm(self, channel: str, peer: str, sid: str) -> None:
        self._dm_sessions[(channel, peer)] = sid

    def forget_dm(self, channel: str, peer: str) -> None:
        self._dm_sessions.pop((channel, peer), None)

    def adopt_from_journal(self, entries) -> None:
        """Rebuild the dm session map after a console restart. The last
        dm-session-opened entry per (channel, peer) wins; the console
        re-owns the kernel session lazily on first use."""
        for e in entries:
            if e.get("kind") == "dm-session-opened" and e.get("session_id"):
                self._dm_sessions[(str(e.get("channel")), str(e.get("peer")))] = \
                    str(e["session_id"])

    # ----------------------------------------------------------------- relay

    def watch_relay(self, sid: str, channel: str, chat_id: str,
                    start_index: int) -> None:
        self._relays[sid] = {"channel": channel, "chat_id": chat_id,
                             "start_index": start_index}

    def note_event(self, event: dict) -> None:
        """EventPipe subscriber: a watched dm session idled -> collect its
        reply text and route it back out the same channel."""
        etype = str(event.get("type", ""))
        if not (etype == "session.idle" or etype.endswith(".idle")
                or etype == "session.execution.failed"):
            return
        data = event.get("data") or event.get("properties") or {}
        sid = str(data.get("sessionID", ""))
        entry = self._relays.pop(sid, None)
        if entry is None:
            return
        try:
            text = self.collect_text(sid, entry["start_index"])
        except Exception:  # noqa: BLE001 — replay must never break the pipe
            text = ""
        if etype == "session.execution.failed" or not text:
            self.journal.add("channel-relay", channel=entry["channel"],
                             session=sid, chars=0, failed=True)
            self.send(entry["channel"], entry["chat_id"],
                      "(no answer — the turn failed or said nothing; check the console)")
            return
        self.journal.add("channel-relay", channel=entry["channel"], session=sid,
                         peer=entry["chat_id"], chars=len(text))
        self.send(entry["channel"], entry["chat_id"], text)

    # ---------------------------------------------------------------- outbound

    def send(self, channel: str, chat_id: str, text: str) -> bool:
        """Best-effort outbound; False (journalled) when it cannot deliver."""
        adapter = self.adapters.get(channel)
        if adapter is None:
            self.journal.add("channel-send-failed", channel=channel,
                             chat=chat_id, reason="not-running")
            return False
        try:
            adapter.send_message(chat_id, text)
        except Exception as e:  # noqa: BLE001 — outbound is never fatal
            self.journal.add("channel-send-failed", channel=channel,
                             chat=chat_id, error=str(e)[:200])
            return False
        return True

    # ---------------------------------------------------- console-line relay

    @staticmethod
    def relay_worthy(line: str) -> bool:
        """Deterministic filter: which announcer lines may leave the host.
        Confirmation notices + routine lifecycle lines, nothing else."""
        l = str(line or "")
        return ("awaiting you" in l or "resolved or expired" in l
                or l.startswith("[jarvis] routine"))

    def relay_console_line(self, line: str) -> None:
        """Called from the console emitter with every announcer line.
        Relays ONLY per policy: relay == "subject" and a subject set and
        the channel actually running."""
        for cfg in self.cfgs.values():
            if cfg.relay != "subject" or not cfg.subject:
                continue
            if cfg.name not in self.adapters:
                continue
            if not self.relay_worthy(line):
                continue
            body = line + RELAY_HINT if "awaiting you" in line else line
            self.send(cfg.name, cfg.subject, body)

    # ---------------------------------------------------------------- status

    def status_line(self, cfg: ChannelConfig) -> str:
        if cfg.name in self.adapters:
            state = "running"
        elif cfg.enabled and not cfg.token:
            state = "enabled, no token"
        else:
            state = "off (policy)"
        paired = (len(self.pairings.approved_peers(cfg.name))
                  if self.pairings is not None else 0)
        return (f"- {cfg.name}: {state}; lane {cfg.lane}; dm {cfg.dm_policy} "
                f"(allow_from {len(cfg.allow_from)}, paired {paired}); "
                f"groups {cfg.group_policy} ({len(cfg.group_allow_from)}); "
                f"relay {cfg.relay}")

    def describe(self) -> str:
        if not self.cfgs:
            return "no channels configured."
        lines = [f"{len(self.cfgs)} channel(s) configured:"]
        lines += [self.status_line(c) for c in self.cfgs.values()]
        return "\n".join(lines)


def _default_factory(cfg: ChannelConfig):
    if cfg.name == "telegram" and cfg.token:
        return TelegramAdapter(cfg.token, poll_timeout=cfg.poll_timeout)
    if cfg.name == "whatsapp":
        return WhatsAppCloudAdapter.from_config(cfg)
    return None
