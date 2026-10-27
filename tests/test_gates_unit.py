"""Unit tests for the Phase 2 spine: policy, Gate A, broker, subject rule."""

import pytest

from jarvis.audit import GateLog
from jarvis.confirm import ConfirmationBroker
from jarvis.gates.intent import classify
from jarvis.kernel.adapter import KernelError
from jarvis.policy import Policy
from jarvis.registry import OwnedKernel, SessionRegistry


# ---------------------------------------------------------------- policy

def test_policy_benign_runs_without_human():
    v = Policy().decide("shell", ["echo hi"], "console")
    assert v.decision == "once" and v.rule == "benign.shell"


def test_policy_ambient_may_still_read():
    assert Policy().decide("shell", ["ls -la"], "ambient").decision == "once"


def test_policy_unknown_command_needs_human():
    v = Policy().decide("shell", ["touch /tmp/x"], "console")
    assert v.decision == "confirm"


def test_policy_ambient_never_mutates():
    v = Policy().decide("shell", ["touch /tmp/x"], "ambient")
    assert v.decision == "reject" and "ambient" in v.rule


def test_policy_forbidden_denies_every_trust_level():
    for trust in ("console", "ambient", "peer", "scheduled"):
        v = Policy().decide("shell", ["rm -rf /"], trust)
        assert v.decision == "reject", trust


def test_policy_edit_is_mutating():
    assert Policy().decide("edit", ["/home/sonic/file.txt"], "console").decision == "confirm"


# ---------------------------------------------------------------- gate A

def test_gateA_console_is_direct_intent():
    assert classify("open the editor", "console") == ("act", "typed at the JARVIS console")


def test_gateA_ambient_without_wake_is_noise():
    assert classify("it is a bit warm in here today", "ambient")[0] == "ignore"


def test_gateA_ambient_wake_plus_imperative_acts():
    assert classify("hey jarvis search flights to delhi", "ambient")[0] == "act"


def test_gateA_ambient_wake_but_unclear_confirms():
    assert classify("jarvis what a strange day", "ambient")[0] == "confirm"


def test_gateA_peer_never_acts():
    assert classify("please review this pull request", "peer")[0] == "journal"


# ---------------------------------------------------------------- broker

def test_broker_code_is_one_time():
    b = ConfirmationBroker(ttl_seconds=60)
    code = b.open("shell: touch /tmp/x")
    assert len(b.peek()) == 1 and b.peek()[0]["description"].startswith("shell")
    assert b.solve(code)["description"].startswith("shell")
    assert b.solve(code) is None          # consumed
    assert b.solve("bogus") is None       # unknown
    assert b.peek() == []


def test_broker_expiry(tmp_path):
    b = ConfirmationBroker(ttl_seconds=0.05)
    code = b.open("x")
    import time; time.sleep(0.1)
    assert b.solve(code) is None


# ---------------------------------------------------------------- audit

def test_audit_roundtrip(tmp_path):
    log = GateLog(tmp_path / "audit.jsonl")
    log.record("tool-gate", "once", "benign", action="shell")
    rows = log.tail(5)
    assert rows[-1]["verdict"] == "once" and rows[-1]["gate"] == "tool-gate"


# ---------------------------------------------------------------- subject rule

def test_subject_rule_blocks_foreign_sessions():
    reg = SessionRegistry()
    reg.add("ses_owned_1")
    ok = OwnedKernel(reg, password="unit-test")  # hermetic: no service.json needed
    with pytest.raises(KernelError) as e:
        ok.interrupt("ses_foreign_1")
    assert e.value.tag == "NotOwnedSession"


def test_subject_rule_blocks_standing_authority():
    reg = SessionRegistry()
    reg.add("ses_owned_1")
    ok = OwnedKernel(reg, password="unit-test")  # hermetic: no service.json needed
    with pytest.raises(KernelError) as e:
        ok.reply_permission("ses_owned_1", "per_x", "always")
    assert e.value.tag == "PolicyDenied"
