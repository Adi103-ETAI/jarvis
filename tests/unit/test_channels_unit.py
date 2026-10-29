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
