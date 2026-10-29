"""Unit tests for Phase 8: the messaging gateway (OpenClaw-adapted).

All tests are hermetic — no network, no kernel, no threads except the
explicit run_loop test. The adapter transport is a fake; the gateway is
exercised directly through handle_update / note_event; the console
wiring is tested with monkeypatched kernel methods.
"""

import re
import threading
import time

import pytest

from jarvis.channels.gateway import (ChannelConfig, ChannelGateway,
                                     PairingStore, load_channel_configs)
from jarvis.channels.telegram import AdapterError, TelegramAdapter, normalize_message
from jarvis.console import JarvisConsole
from jarvis.gates.intent import classify
from jarvis.journal import Journal
from jarvis.policy import Policy


# ------------------------------------------------------------------ fakes

class FakeTransport:
    """Scripted Bot API transport. Values: callable(payload) -> dict, or
    a fixed response dict. Unscripted methods get sane ok=True results."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, method, payload, timeout):
        self.calls.append((method, payload))
        h = self.responses.get(method)
        if callable(h):
            return h(payload)
        if h is not None:
            return h
        if method == "getMe":
            return {"ok": True, "result": {"id": 42, "username": "jarvis_bot"}}
        if method == "getUpdates":
            return {"ok": True, "result": []}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 1}}
        return {"ok": True, "result": None}


class FakeAdapter:
    """Stand-in adapter for gateway tests: identity + captured sends."""

    def __init__(self, bot_id=None, bot_username=None):
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.sent = []

    def send_message(self, chat_id, text, reply_to=None):
        self.sent.append((chat_id, text))
        return 1


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, kind, peer, msg, admission):
        self.calls.append((cfg.name, kind, peer, msg, admission))


def dm_msg(text="hello there", sender="555", name="bob"):
    return {"update_id": 1, "message_id": 1, "chat_id": sender,
            "chat_type": "private", "sender_id": sender, "sender_name": name,
            "text": text, "reply_to_sender_id": None, "date": 1}


def grp_msg(text="room chatter", chat="-100999", sender="777", name="eve",
            reply_to=None):
    return {"update_id": 2, "message_id": 2, "chat_id": chat,
            "chat_type": "supergroup", "sender_id": sender, "sender_name": name,
            "text": text, "reply_to_sender_id": reply_to, "date": 2}


def make_gw(tmp_path, cfg=None, adapter=None):
    cfg = cfg or ChannelConfig("telegram")
    rec = Recorder()
    store = PairingStore(tmp_path / "pairing.jsonl")
    jrn = Journal(tmp_path / "journal.jsonl")
    gw = ChannelGateway([cfg], on_message=rec, journal=jrn,
                        pairing_store=store, collect_text=lambda s, st: "")
    if adapter is not None:
        gw.adapters[cfg.name] = adapter
    return gw, cfg, rec, store, jrn


# ---------------------------------------------------------------- adapter

def test_normalize_message_and_drops():
    raw = {"update_id": 9, "message": {
        "message_id": 3, "from": {"id": 555, "username": "bob"},
        "chat": {"id": 555, "type": "private"}, "text": "hi", "date": 1}}
    m = normalize_message(raw)
    assert m["sender_id"] == "555" and m["chat_type"] == "private"
    assert m["sender_name"] == "bob" and m["text"] == "hi"
    # edits, media, anonymous calls are dropped in v0
    assert normalize_message({"update_id": 10, "edited_message": raw["message"]}) is None
    assert normalize_message({"update_id": 11, "message": {
        "message_id": 4, "from": {"id": 1}, "chat": {"id": 1, "type": "private"},
        "date": 1}}) is None  # no text


def test_poll_once_normalizes_and_advances_offset():
    batches = iter([
        [{"update_id": 10, "message": {
            "message_id": 1, "from": {"id": 555},
            "chat": {"id": 555, "type": "private"}, "text": "one", "date": 1}},
         {"update_id": 11, "message": {  # non-text -> normalized away
            "message_id": 2, "from": {"id": 555},
            "chat": {"id": 555, "type": "private"}, "photo": [{"f": "x"}],
            "date": 1}},
         ],
        [],
    ])
    t = FakeTransport({"getUpdates": lambda payload: {"ok": True,
                                                      "result": next(batches)}})
    ad = TelegramAdapter("tok", transport=t, poll_timeout=0.1)
    msgs = ad.poll_once()
    assert len(msgs) == 1 and msgs[0]["text"] == "one"
    ad.poll_once()                              # second long poll carries the offset
    assert ad.offset == 12
    assert t.calls[-1][1]["offset"] == 12


def test_send_message_chunks_at_limit():
    t = FakeTransport()
    ad = TelegramAdapter("tok", transport=t, chunk_limit=100)
    n = ad.send_message("555", "x" * 250)
    sends = [c for c in t.calls if c[0] == "sendMessage"]
    assert n == 3 and len(sends) == 3
    assert all(len(p["text"]) <= 100 for _, p in sends)


def test_api_error_raises_adapter_error():
    ad = TelegramAdapter("tok", transport=FakeTransport(
        {"getMe": {"ok": False, "description": "Unauthorized"}}))
    with pytest.raises(AdapterError):
        ad.api("getMe")
    assert ad.identify() is False          # identify swallows, degrades


def test_run_loop_survives_transport_errors():
    state = {"n": 0}

    def transport(method, payload, timeout):
        if method == "getMe":
            return {"ok": True, "result": {"id": 42, "username": "jb"}}
        if method == "getUpdates":
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("network hiccup")  # transport failure
            if state["n"] == 2:
                return {"ok": True, "result": [{
                    "update_id": 5, "message": {
                        "message_id": 1, "from": {"id": 7, "username": "x"},
                        "chat": {"id": 7, "type": "private"}, "text": "hi",
                        "date": 1}}]}
            return {"ok": True, "result": []}
        return {"ok": True, "result": None}

    ad = TelegramAdapter("tok", transport=transport, poll_timeout=0.05,
                         error_backoff=0.01)
    got, stop = [], threading.Event()

    def on_message(m):
        got.append(m)
        stop.set()

    th = threading.Thread(target=ad.run_loop, args=(on_message, stop), daemon=True)
    th.start()
    th.join(timeout=5)
    assert not th.is_alive()
    assert got and got[0]["sender_id"] == "7"


# ------------------------------------------------------------- config load

def test_config_defaults_missing_lane_is_roadmap_default():
    cfgs = load_channel_configs({"channels": {"telegram": {}}})
    c = cfgs[0]
    assert c.enabled is False
    assert c.lane == "peer"            # roadmap default for a channel
    assert c.dm_policy == "pairing"    # OpenClaw default, still safe
    assert c.group_policy == "disabled"
    assert c.relay == "off"


def test_config_invalid_values_fall_back_safe():
    cfgs = load_channel_configs({"channels": {"telegram": {
        "lane": "admin", "dm_policy": "open", "group_policy": "open",
        "relay": "everything", "mention_patterns": ["[bad"]}}})
    c = cfgs[0]
    assert c.lane == "observe" and c.dm_policy == "disabled"
    assert c.group_policy == "disabled" and c.relay == "off"
    assert c.mention_patterns == ()    # invalid regex skipped


def test_token_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_TELEGRAM_TOKEN", raising=False)
    body = {"token": " direct "}
    assert load_channel_configs({"channels": {"telegram": body}})[0].token == "direct"
    monkeypatch.setenv("JARVIS_TELEGRAM_TOKEN", "envtok")
    assert load_channel_configs({"channels": {"telegram": {}}})[0].token == "envtok"
    monkeypatch.delenv("JARVIS_TELEGRAM_TOKEN")
    tf = tmp_path / "t.txt"
    tf.write_text("filetok\n")
    assert load_channel_configs({"channels": {"telegram": {
        "token_file": str(tf)}}})[0].token == "filetok"


# ----------------------------------------------------------- pairing store

def test_pairing_code_shape():
    from jarvis.channels.gateway import new_pairing_code
    code = new_pairing_code()
    assert len(code) == 8
    assert all(ch in "ABCDEFGHJKLMNPQRSTUVWXYZ23456789" for ch in code)


def test_pairing_request_repeat_and_approve(tmp_path):
    s = PairingStore(tmp_path / "p.jsonl")
    code, status = s.request("telegram", "555", "bob", ttl_seconds=100)
    assert status == "created" and len(code) == 8
    code2, status2 = s.request("telegram", "555", "bob", ttl_seconds=100)
    assert status2 == "pending" and code2 == code   # no repeat offers
    assert not s.is_approved("telegram", "555")
    row = s.approve("telegram", code, ttl_seconds=100)
    assert row and row["peer"] == "555"
    assert s.is_approved("telegram", "555")
    assert s.approve("telegram", code, ttl_seconds=100) is None  # consumed


def test_pairing_cap_and_expiry(tmp_path):
    s = PairingStore(tmp_path / "p.jsonl")
    for peer in ("1", "2", "3"):
        code, status = s.request("telegram", peer, "n", ttl_seconds=100,
                                 max_pending=3)
        assert status == "created"
    code, status = s.request("telegram", "4", "n", ttl_seconds=100, max_pending=3)
    assert status == "capped" and code is None
    # expiry: a request is garbage-collected once its TTL has passed on touch
    s2 = PairingStore(tmp_path / "p2.jsonl")
    c1, _ = s2.request("telegram", "9", "n", ttl_seconds=3600)
    c_mid, st_mid = s2.request("telegram", "9", "n", ttl_seconds=0.0)
    assert st_mid == "created" and c_mid != c1     # old one expired, new offered
    c2, st2 = s2.request("telegram", "9", "n", ttl_seconds=3600)
    assert st2 == "pending" and c2 == c_mid        # no repeat offers


def test_pairing_reject_and_forget(tmp_path):
    s = PairingStore(tmp_path / "p.jsonl")
    code, _ = s.request("telegram", "555", "bob", ttl_seconds=100)
    assert s.reject("telegram", code, ttl_seconds=100)
    assert not s.is_approved("telegram", "555")
    code2, _ = s.request("telegram", "556", "ann", ttl_seconds=100)
    s.approve("telegram", code2, ttl_seconds=100)
    assert s.is_approved("telegram", "556")
    assert s.forget("telegram", "556")
    assert not s.is_approved("telegram", "556")
    assert not s.forget("telegram", "nope")


def test_pairing_store_persists(tmp_path):
    path = tmp_path / "p.jsonl"
    s = PairingStore(path)
    code, _ = s.request("telegram", "555", "bob", ttl_seconds=3600)
    s.approve("telegram", code, ttl_seconds=3600)
    code2, _ = s.request("telegram", "777", "eve", ttl_seconds=3600)
    s2 = PairingStore(path)   # rebuilt from the file after a restart
    assert s2.is_approved("telegram", "555")
    assert [e["code"] for e in s2.pending("telegram")] == [code2]


# ---------------------------------------------------------- dm admission

def test_dm_policy_allowlist_admits_only_listed(tmp_path):
    gw, cfg, rec, store, jrn = make_gw(
        tmp_path, cfg=ChannelConfig("telegram", dm_policy="allowlist",
                                    allow_from=frozenset({"555"})))
    gw.handle_update(cfg, dm_msg("hi", sender="555"))
    gw.handle_update(cfg, dm_msg("hi", sender="666"))
    assert [(c[2], c[4]) for c in rec.calls] == [("555", "policy")]
    drops = jrn.tail(10, kind="channel-drop")
    assert drops and drops[-1]["reason"] == "dm-policy-allowlist"


def test_dm_pairing_flow_offers_then_admits_after_console_approval(tmp_path):
    ad = FakeAdapter()
    gw, cfg, rec, store, jrn = make_gw(tmp_path, adapter=ad)
    gw.handle_update(cfg, dm_msg("let me in", sender="666", name="eve"))
    # message NOT processed; pairing offer sent on-channel; request recorded
    assert rec.calls == []
    assert len(ad.sent) == 1 and "channel approve telegram" in ad.sent[0][1]
    req = jrn.tail(10, kind="channel-pairing")[-1]
    assert req["action"] == "requested" and req["code"]
    # second message while pending: no second offer
    gw.handle_update(cfg, dm_msg("again", sender="666", name="eve"))
    assert len(ad.sent) == 1
    # the console (and only the console) approves:
    assert store.approve("telegram", req["code"])
    gw.handle_update(cfg, dm_msg("now we talk", sender="666", name="eve"))
    assert rec.calls and rec.calls[0][4] == "pairing"


def test_dm_pairing_capped(tmp_path):
    ad = FakeAdapter()
    cfg = ChannelConfig("telegram", max_pending_pairings=1)
    gw, cfg, rec, store, jrn = make_gw(tmp_path, cfg=cfg, adapter=ad)
    gw.handle_update(cfg, dm_msg(sender="1"))
    gw.handle_update(cfg, dm_msg(sender="2"))
    assert len(ad.sent) == 1
    drops = jrn.tail(10, kind="channel-drop")
    assert drops[-1]["reason"] == "pairing-capped"


def test_dm_disabled_policy_drops_everything(tmp_path):
    gw, cfg, rec, store, jrn = make_gw(
        tmp_path, cfg=ChannelConfig("telegram", dm_policy="disabled"))
    gw.handle_update(cfg, dm_msg(sender="555"))
    assert rec.calls == []
    assert jrn.tail(10, kind="channel-drop")[-1]["reason"] == "dm-policy-disabled"


# ------------------------------------------------------- group admission

def test_group_policy_disabled_drops(tmp_path):
    gw, cfg, rec, store, jrn = make_gw(tmp_path)
    gw.handle_update(cfg, grp_msg())
    assert rec.calls == []
    assert jrn.tail(10, kind="channel-drop")[-1]["reason"] == "group-policy-disabled"


def test_group_allowlist_and_mention_gating(tmp_path):
    cfg = ChannelConfig("telegram", group_policy="allowlist",
                        group_allow_from=frozenset({"-100999"}),
                        mention_patterns=(re.compile("jarvis", re.IGNORECASE),))
    ad = FakeAdapter(bot_id="42", bot_username="jarvis_bot")
    gw, cfg, rec, store, jrn = make_gw(tmp_path, cfg=cfg, adapter=ad)

    gw.handle_update(cfg, grp_msg(chat="-100888"))            # not allowlisted
    assert rec.calls == []

    gw.handle_update(cfg, grp_msg())                          # unmentioned
    assert rec.calls[-1][4] == "context"

    gw.handle_update(cfg, grp_msg(text="hey jarvis status"))  # pattern mention
    assert rec.calls[-1][4] == "mentioned"

    gw.handle_update(cfg, grp_msg(text="what is up",
                                  reply_to="42"))             # reply-to-bot
    assert rec.calls[-1][4] == "mentioned"

    gw.handle_update(cfg, grp_msg(text="ping @jarvis_bot"))   # native mention
    assert rec.calls[-1][4] == "mentioned"

    cfg.require_mention = False
    gw.handle_update(cfg, grp_msg(text="anything goes"))
    assert rec.calls[-1][4] == "mentioned"


# ------------------------------------------------------ gate A + policy

def test_gate_a_answer_lane_acts_but_peers_stay_journal():
    v, r = classify("what is on my plate today", "peer", lane="answer")
    assert v == "act" and "answer lane" in r
    v, r = classify("what is on my plate today", "peer")
    assert v == "journal"
    v, _ = classify("okay bye", "peer", lane="answer")
    assert v == "ignore"                                       # chatter stays ignored
    v, r = classify("deploy the service", "peer", lane="observe")
    assert v == "journal"


def test_policy_peer_trust_denies_mutation_and_delegation(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[trust.peer]\nmutating = "deny"\ndeny_actions = ["task"]\n')
    pol = Policy(p)
    assert pol.decide("shell", ["touch /tmp/x"], "peer").decision == "reject"
    assert pol.decide("task", ["sub-agent"], "peer").decision == "reject"
    # and the shipped config carries the same hardening
    from jarvis.policy import DEFAULT_PATH
    assert Policy(DEFAULT_PATH).decide("shell", ["touch /tmp/x"], "peer").decision == "reject"


# ------------------------------------------------------------- relay

def test_relay_filter_is_deterministic():
    assert ChannelGateway.relay_worthy("awaiting you  abc123  shell: x")
    assert ChannelGateway.relay_worthy("(abc123 resolved or expired)")
    assert ChannelGateway.relay_worthy("[jarvis] routine 'brief' launched")
    assert not ChannelGateway.relay_worthy("j> hello there")
    assert not ChannelGateway.relay_worthy("")


def test_relay_console_line_routes_only_to_subject(tmp_path):
    cfg = ChannelConfig("telegram", relay="subject", subject="999")
    off = ChannelConfig("telegram2", relay="off", subject="999")
    gw, _, _, _, _ = make_gw(tmp_path, cfg=cfg)
    ad = FakeAdapter()
    gw.adapters["telegram"] = ad
    gw.cfgs["telegram2"] = off
    gw.relay_console_line("[jarvis] awaiting you  abc123  shell: touch /tmp/x")
    assert len(ad.sent) == 1 and ad.sent[0][0] == "999"
    assert "resolve at the JARVIS console" in ad.sent[0][1]
    gw.relay_console_line("just chatter from the console")
    assert len(ad.sent) == 1
    # relay off or channel down -> nothing
    cfg.relay = "off"
    gw.relay_console_line("[jarvis] awaiting you  abc123  shell: x")
    assert len(ad.sent) == 1


# ------------------------------------------------- console spine wiring

MINIMAL_POLICY = """
version = 1

[trust.peer]
mutating = "deny"
deny_actions = ["task"]

[channels.telegram]
enabled = false            # no threads in unit tests; gateway built, adapters not
lane = "peer"
dm_policy = "pairing"
"""


@pytest.fixture
def jc_chan(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PASSWORD", "unit-test")
    policy = tmp_path / "policy.toml"
    policy.write_text(MINIMAL_POLICY)
    console = JarvisConsole(
        policy_path=str(policy),
        journal_path=tmp_path / "journal.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        pending_root=tmp_path / "pending",
        routines_path=str(tmp_path / "routines.toml"),
        memory_dir=tmp_path / "mem",
    )
    yield console
    console.close()


def test_console_builds_gateway_from_policy(jc_chan):
    gw = jc_chan.gateway
    assert gw is not None and "telegram" in gw.cfgs
    assert gw.cfgs["telegram"].lane == "peer"
    assert gw.adapters == {}          # enabled=false -> nothing runs
    assert "telegram" in jc_chan.handle("channels")


def test_dm_answer_launches_peer_turn_and_relays_reply(jc_chan):
    jc = jc_chan
    gw = jc.gateway
    jc.kernel.create_session = lambda **kw: {"id": "ses_dm1", "title": kw.get("title", "")}
    jc.kernel.prompt = lambda sid, text, **kw: {"id": "msg_1"}

    class Ad(FakeAdapter):
        pass
    ad = Ad()
    gw.adapters["telegram"] = ad
    cfg = gw.cfgs["telegram"]

    msg = dm_msg("what is on my plate today", sender="555", name="bob")
    out = jc._channel_message(cfg, "dm", "555", msg, "policy")
    assert "background" in out
    sid = gw.dm_session("telegram", "555")
    assert sid == "ses_dm1" and jc.registry.owns(sid)
    assert jc.worker._trust[sid] == "peer"
    assert f"dm:telegram:555" == jc.registry._own[sid]["title"]
    cm = jc.journal.tail(20, kind="channel-message")[-1]
    assert cm["admission"] == "policy" and cm["peer"] == "555" and cm["flow"] == "dm"
    ts = jc.journal.tail(20, kind="turn-started")[-1]
    assert ts["session_id"] == sid and ts["lane"] == "bg"

    # the reply routes back out the same channel when the turn idles
    jc.events.append({"type": "session.text.delta",
                      "data": {"sessionID": sid, "text": "Two things on your plate."}})
    gw.note_event({"type": "session.idle", "data": {"sessionID": sid}})
    assert ad.sent and ad.sent[0][0] == "555" and "plate" in ad.sent[0][1]
    relay = jc.journal.tail(20, kind="channel-relay")[-1]
    assert relay["chars"] > 0 and relay["channel"] == "telegram"


def test_dm_answer_survives_second_message_and_failure_notice(jc_chan):
    jc = jc_chan
    gw = jc.gateway
    jc.kernel.create_session = lambda **kw: {"id": "ses_dm2", "title": ""}
    jc.kernel.prompt = lambda sid, text, **kw: {"id": "m"}

    class Boom(FakeAdapter):
        pass
    ad = Boom()
    gw.adapters["telegram"] = ad
    cfg = gw.cfgs["telegram"]
    jc._channel_message(cfg, "dm", "7", dm_msg("hi", sender="7"), "policy")
    sid = gw.dm_session("telegram", "7")
    # turn fails: the channel gets an honest failure notice, not silence
    gw.note_event({"type": "session.execution.failed", "data": {"sessionID": sid}})
    assert ad.sent and "no answer" in ad.sent[0][1]


def test_observe_lane_records_without_turn(jc_chan):
    jc = jc_chan
    gw = jc.gateway
    cfg = gw.cfgs["telegram"]
    cfg.lane = "observe"          # the record-only posture (policy choice)
    prompts = []
    jc.kernel.prompt = lambda sid, text, **kw: prompts.append(text) or {"id": "m"}
    jc.kernel.create_session = lambda **kw: {"id": "ses_x"}
    out = jc._channel_message(cfg, "dm", "7",
                              dm_msg("hi", sender="7"), "policy")
    assert out == "(recorded)"
    assert prompts == [] and gw.dm_session("telegram", "7") is None
    assert jc.journal.tail(10, kind="channel-message")[-1]["peer"] == "7"


def test_group_message_recorded_never_turns(jc_chan):
    jc = jc_chan
    gw = jc.gateway
    cfg = gw.cfgs["telegram"]
    prompts = []
    jc.kernel.prompt = lambda sid, text, **kw: prompts.append(text) or {"id": "m"}
    jc._channel_message(cfg, "group", "-100999",
                        grp_msg(text="hey jarvis what is up"), "mentioned")
    assert prompts == []
    cm = jc.journal.tail(10, kind="channel-message")[-1]
    assert cm["flow"] == "group" and cm["admission"] == "mentioned"


def test_console_pairing_approval_flow(jc_chan):
    jc = jc_chan
    gw = jc.gateway
    ad = FakeAdapter()
    gw.adapters["telegram"] = ad
    gw.handle_update(gw.cfgs["telegram"], dm_msg("open sesame", sender="321",
                                                 name="trudy"))
    assert jc.handle("channel pairings").count("pending pairing") >= 1
    code = gw.pairings.pending("telegram")[0]["code"]
    out = jc.handle("channel approve telegram " + code)
    assert "approved" in out and "trudy" in out
    assert gw.pairings.is_approved("telegram", "321")
    assert ad.sent and "Paired" in ad.sent[-1][1]
    assert jc.handle("channel forget telegram 321").startswith("revoked")
    assert not gw.pairings.is_approved("telegram", "321")
    assert "no pending pairing" in jc.handle("channel approve telegram ZZZZ9999")


def test_gateway_adopts_dm_sessions_from_journal(jc_chan, tmp_path):
    jc = jc_chan
    gw = jc.gateway
    jc.journal.add("dm-session-opened", session_id="ses_old",
                   channel="telegram", peer="42")
    gw2 = ChannelGateway([gw.cfgs["telegram"]], on_message=lambda *a: None,
                         journal=jc.journal, pairing_store=gw.pairings)
    gw2.adopt_from_journal(jc.journal.tail(10))
    assert gw2.dm_session("telegram", "42") == "ses_old"


def test_http_transport_against_real_http_server(monkeypatch):
    """The default transport is real urllib: prove it end-to-end against
    a localhost HTTP server speaking Bot API JSON (no TLS, no network)."""
    import json as _json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["path"] = self.path
            seen["payload"] = _json.loads(body)
            out = _json.dumps({"ok": True, "result": {"message_id": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):  # keep test output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        from jarvis.channels import telegram as tg
        port = server.server_address[1]
        monkeypatch.setattr(tg, "TELEGRAM_API", f"http://127.0.0.1:{port}")
        ad = TelegramAdapter("secret-token", transport=tg.http_transport("secret-token"))
        n = ad.send_message("555", "hello over real http")
        assert n == 1
        assert seen["path"] == "/botsecret-token/sendMessage"
        assert seen["payload"] == {"chat_id": "555", "text": "hello over real http"}
    finally:
        server.shutdown()


# ------------------------------------------------- whatsapp adapter (8.1)

import hashlib
import hmac as hmac_mod
import json as _json
import urllib.error
import urllib.request
from http.server import HTTPServer

from jarvis.channels.whatsapp import (WEBHOOK_PATH, WhatsAppCloudAdapter,
                                      normalize_webhook, signature_ok)


def _wa_payload(messages, statuses=None, group_id=None):
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "15550001111",
                          "phone_number_id": "PNID123"},
             "contacts": [{"profile": {"name": "Adi"}, "wa_id": "911234567890"}],
             "messages": messages}
    if group_id:
        value["group_id"] = group_id
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA1",
                       "changes": [{"field": "messages", "value": value}]}]}


def _wa_text(body="hello jarvis", sender="911234567890", mid="wamid.ABC"):
    return {"from": sender, "id": mid, "timestamp": "1699999999",
            "text": {"body": body}, "type": "text"}


class TestWhatsAppNormalize:
    def test_text_dm_becomes_private_with_sender_name(self):
        out = normalize_webhook(_wa_payload([_wa_text()]))
        assert len(out) == 1
        m = out[0]
        assert m["chat_id"] == "911234567890"
        assert m["chat_type"] == "private"
        assert m["sender_id"] == "911234567890"
        assert m["sender_name"] == "Adi"
        assert m["text"] == "hello jarvis"
        assert m["date"] == 1699999999
        assert m["message_id"] == "wamid.ABC"
        assert set(m) >= {"update_id", "reply_to_sender_id"}  # same shape as telegram

    def test_statuses_and_media_are_dropped(self):
        # no text message at all: one image + delivery statuses -> nothing
        payload = _wa_payload([{"from": "911234567890", "id": "wamid.IMG",
                                "type": "image", "image": {"id": "x"}}],
                              statuses=[{"id": "wamid.IMG",
                                         "status": "delivered"}])
        assert normalize_webhook(payload) == []

    def test_group_message_rides_the_group_jid(self):
        out = normalize_webhook(_wa_payload([_wa_text(sender="91999998888")],
                                           group_id="123-456@g.us"))
        assert out[0]["chat_id"] == "123-456@g.us"
        assert out[0]["chat_type"] == "group"
        assert out[0]["sender_id"] == "91999998888"

    def test_garbage_payload_yields_nothing(self):
        assert normalize_webhook({}) == []
        assert normalize_webhook({"entry": []}) == []
        assert normalize_webhook({"entry": [{"changes": [{}]}]}) == []


class TestWhatsAppSignature:
    def test_valid_and_invalid(self):
        secret, raw = "shhh", b'{"hello": 1}'
        good = "sha256=" + hmac_mod.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        assert signature_ok(secret, raw, good) is True
        assert signature_ok(secret, raw, "sha256=" + "0" * 64) is False
        assert signature_ok(secret, raw, None) is False
        assert signature_ok(secret, raw, "md5=abcd") is False

    def test_no_secret_accepts_and_notes_dev_mode(self):
        ad = WhatsAppCloudAdapter("tok", "PNID", transport=lambda *a: {})
        notes = "\n".join(ad.startup_notes())
        assert "NOT verified" in notes and "dev mode" in notes
        ad2 = WhatsAppCloudAdapter("tok", "PNID", app_secret="s",
                                   transport=lambda *a: {})
        assert "enforcement ON" in "\n".join(ad2.startup_notes())


class TestWhatsAppSend:
    def test_chunks_and_payload_shape(self):
        t = FakeTransport()
        ad = WhatsAppCloudAdapter("tok", "PNID", transport=t)
        body = "word " * 1200                       # 6000 chars -> 2 chunks
        assert ad.send_message("911234567890", body) == 2
        assert len(t.calls) == 2
        method, payload = t.calls[0]
        assert method == "messages"
        assert payload["messaging_product"] == "whatsapp"
        assert payload["recipient_type"] == "individual"
        assert payload["to"] == "911234567890"
        assert payload["text"]["preview_url"] is False
        assert payload["text"]["body"]
        assert len(payload["text"]["body"]) <= 4000

    def test_group_send_omits_recipient_type(self):
        t = FakeTransport()
        ad = WhatsAppCloudAdapter("tok", "PNID", transport=t)
        ad.send_message("123-456@g.us", "group note")
        _, payload = t.calls[0]
        assert "recipient_type" not in payload
        assert payload["to"] == "123-456@g.us"

    def test_graph_error_envelope_raises_adapter_error(self):
        t = FakeTransport({"messages": {"error": {"message": "bad token",
                                                  "code": 190}}})
        ad = WhatsAppCloudAdapter("tok", "PNID", transport=t)
        with pytest.raises(AdapterError, match="bad token"):
            ad.send_message("911234567890", "hi")


def _wa_cfg(**over):
    from jarvis.channels.gateway import ChannelConfig as CC
    base = dict(name="whatsapp", enabled=True, token="tok",
                options={"phone_number_id": "PNID123"})
    base.update(over)
    return CC(**base)


class TestWhatsAppFromConfig:
    def test_builds_with_phone_number_id(self):
        ad = WhatsAppCloudAdapter.from_config(_wa_cfg())
        assert isinstance(ad, WhatsAppCloudAdapter)
        assert ad.phone_number_id == "PNID123"
        assert ad.webhook_port == 8080 and ad.webhook_path == "/webhook"
        assert ad.verify_token                       # derived, 16 hex chars
        assert len(ad.verify_token) == 16

    def test_missing_phone_number_id_returns_none(self):
        cfg = _wa_cfg(options={})
        assert WhatsAppCloudAdapter.from_config(cfg) is None
        assert WhatsAppCloudAdapter.from_config(_wa_cfg(token="")) is None

    def test_bad_numbers_fall_back_safe(self):
        ad = WhatsAppCloudAdapter.from_config(_wa_cfg(
            options={"phone_number_id": "P", "webhook_port": "not-a-port",
                     "chunk_limit": None}))
        assert ad.webhook_port == 8080
        assert ad.chunk_limit == 4000

    def test_policy_verify_token_marks_the_note(self):
        ad = WhatsAppCloudAdapter.from_config(_wa_cfg(
            options={"phone_number_id": "P", "webhook_verify_token": "mytok"}))
        assert ad.verify_token == "mytok"
        assert "from policy" in "\n".join(ad.startup_notes())


class TestOptionsPassthrough:
    def test_unknown_keys_reach_options_known_do_not(self):
        cfgs = load_channel_configs({"channels": {"whatsapp": {
            "enabled": True, "lane": "peer", "phone_number_id": "P",
            "app_secret": "s", "webhook_port": 8080, "mystery": 1}}})
        cfg = cfgs[0]
        assert cfg.options == {"phone_number_id": "P", "app_secret": "s",
                               "webhook_port": 8080, "mystery": 1}

    def test_typo_dialed_option_cannot_widen_known_fields(self):
        # a typo like "lane" misspelled must not sneak into options AND
        # must still fall back to the safe lane
        cfgs = load_channel_configs({"channels": {"whatsapp": {
            "laan": "root", "dm_polcy": "allowlist"}}})
        cfg = cfgs[0]
        assert cfg.lane == "peer" and cfg.dm_policy == "pairing"
        assert set(cfg.options) == {"laan", "dm_polcy"}


class TestWhatsAppWebhookServer:
    def _adapter_and_loop(self, monkeypatch, **kw):
        from jarvis.channels import whatsapp as wamod
        monkeypatch.setattr(wamod, "WEBHOOK_PORT", 0)
        ad = WhatsAppCloudAdapter("tok", "PNID", webhook_host="127.0.0.1",
                                  webhook_port=0, **kw)
        stop, got = threading.Event(), []
        th = threading.Thread(target=ad.run_loop,
                              args=(lambda m: got.append(m), stop), daemon=True)
        th.start()
        deadline = time.time() + 5
        while ad.bound_port is None and time.time() < deadline:
            time.sleep(0.01)
        assert ad.bound_port, "webhook server never bound"
        return ad, stop, th, got

    def _post(self, port, payload, headers=None, path=WEBHOOK_PATH):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=payload,
            headers=headers or {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()

    def test_verify_handshake(self, monkeypatch):
        ad, stop, th, _ = self._adapter_and_loop(monkeypatch)
        try:
            url = f"http://127.0.0.1:{ad.bound_port}/webhook"
            q = f"?hub.mode=subscribe&hub.verify_token={ad.verify_token}&hub.challenge=424242"
            with urllib.request.urlopen(url + q, timeout=5) as r:
                assert r.read().decode() == "424242"
            try:
                urllib.request.urlopen(url + "?hub.mode=subscribe&"
                                       "hub.verify_token=WRONG&hub.challenge=x",
                                       timeout=5)
                assert False, "wrong token must not verify"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        finally:
            stop.set()
            th.join(timeout=3)

    def test_post_flows_dedupes_and_rejects_bad_signature(self, monkeypatch):
        secret = "appsec"
        ad, stop, th, got = self._adapter_and_loop(monkeypatch, app_secret=secret)
        raw = _json.dumps(_wa_payload([_wa_text()])).encode()
        sig = "sha256=" + hmac_mod.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        try:
            assert self._post(ad.bound_port, raw, {"X-Hub-Signature-256": sig})[0] == 200
            deadline = time.time() + 5
            while not got and time.time() < deadline:
                time.sleep(0.01)
            assert len(got) == 1 and got[0]["text"] == "hello jarvis"
            # Meta redelivery: same message id -> spine stays quiet
            self._post(ad.bound_port, raw, {"X-Hub-Signature-256": sig})
            time.sleep(0.3)
            assert len(got) == 1
            # bad signature -> 403, nothing delivered
            try:
                self._post(ad.bound_port, raw, {"X-Hub-Signature-256": "sha256=" + "0" * 64})
                assert False
            except urllib.error.HTTPError as e:
                assert e.code == 403
            time.sleep(0.3)
            assert len(got) == 1
        finally:
            stop.set()
            th.join(timeout=3)

    def test_dev_mode_accepts_unsigned(self, monkeypatch):
        ad, stop, th, got = self._adapter_and_loop(monkeypatch)  # no app_secret
        raw = _json.dumps(_wa_payload([_wa_text()])).encode()
        try:
            assert self._post(ad.bound_port, raw)[0] == 200
            deadline = time.time() + 5
            while not got and time.time() < deadline:
                time.sleep(0.01)
            assert len(got) == 1
        finally:
            stop.set()
            th.join(timeout=3)


class TestWhatsAppGatewayIntegration:
    def test_factory_branch(self):
        from jarvis.channels.gateway import _default_factory
        assert isinstance(_default_factory(_wa_cfg()), WhatsAppCloudAdapter)
        assert _default_factory(_wa_cfg(options={})) is None
        assert _default_factory(_wa_cfg(name="signal")) is None

    def test_unknown_wa_sender_gets_pairing_not_execution(self, tmp_path):
        gw = ChannelGateway([_wa_cfg()], on_message=lambda *a: None,
                            journal=Journal(str(tmp_path / "j.jsonl")),
                            pairing_store=PairingStore(tmp_path / "pairing.jsonl"))
        ad = WhatsAppCloudAdapter("tok", "PNID", transport=lambda *a: {})
        sends = []
        ad.send_message = lambda chat, text, reply_to=None: sends.append(text) or 1
        gw.adapters["whatsapp"] = ad
        msg = normalize_webhook(_wa_payload([_wa_text()]))[0]
        gw.handle_update(_wa_cfg(), msg)
        assert len(sends) == 1 and "pairing" in sends[0].lower()
        # and the message never reached the spine
        assert gw.dm_session("whatsapp", msg["sender_id"]) is None

    def test_allowed_wa_sender_reaches_spine(self, tmp_path):
        seen = []
        cfg = _wa_cfg(allow_from=frozenset({"911234567890"}))
        gw = ChannelGateway([cfg], on_message=lambda *a: seen.append(a),
                            journal=Journal(str(tmp_path / "j.jsonl")))
        msg = normalize_webhook(_wa_payload([_wa_text()]))[0]
        gw.handle_update(cfg, msg)
        assert len(seen) == 1
        assert seen[0][2] == "911234567890"          # peer_id
        assert seen[0][4] == "policy"                # admission reason
