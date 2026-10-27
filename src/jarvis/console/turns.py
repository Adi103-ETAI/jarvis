"""Pure turn-scan helpers over the captured kernel event list.

No imports, no state: latency measurement, failure detection, the
quota-status constants that decide failover, and model rendering.
"""

from __future__ import annotations


def first_token_seconds(events, session_id, started_wall, start_index=0):
    """Seconds from prompt dispatch to the first text delta of THIS turn.

    Pure helper over the captured event list (phase 3 gate: first-token
    latency measurement). Kernel timestamps arrive in epoch units --
    milliseconds or seconds depending on the route -- so both are handled;
    anything undatable returns None instead of a wrong number.
    """
    for e in events[start_index:]:
        if str(e.get("type", "")) != "session.text.delta":
            continue
        if (e.get("data") or {}).get("sessionID") != session_id:
            continue
        try:
            t = float(e.get("created"))
        except (TypeError, ValueError):
            return None
        if t > 1e12:              # epoch milliseconds
            t /= 1000.0
        if t < started_wall - 1:  # clock-skew guard: ignore nonsensical deltas
            return None
        return max(0.0, t - started_wall)
    return None


def turn_failed(events, session_id, start_index=0):
    """True if a kernel execution-failure event for THIS session appears in
    the captured slice (failover signal: the model died mid-turn)."""
    for e in events[start_index:]:
        if str(e.get("type", "")) != "session.execution.failed":
            continue
        if (e.get("data") or {}).get("sessionID") == session_id:
            return True
    return False


def _model_str(m):
    return str(m.get("providerID", "?")) + "/" + str(m.get("id", "?"))


# Kernel statuses that mean "this MODEL is exhausted", not "the request is
# wrong": 429 rate limit / quota, 402 payment required. Kernel-down (0) and
# bad-request (4xx others) are NOT failover material -- a different model
# would die the same way.
QUOTA_STATUSES = (429, 402)
