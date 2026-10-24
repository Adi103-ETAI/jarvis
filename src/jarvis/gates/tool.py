"""Gate B — decides what happens to each kernel permission request.

The kernel raises permission asks for protected tools (shell, edit,
webfetch...). Gate B maps each request through policy:

    once    -> reply "once" (execute this one call)
    reject  -> reply "reject" (deny the call; the model sees a denial)
    confirm -> park it and ask the human with a one-time code

Gate B never upgrades a denial and never answers "always" — that would
mint standing authority, and standing authority is minted by policy files,
not by a model.
"""

from __future__ import annotations

from typing import Callable

from jarvis.audit import GateLog
from jarvis.policy import Policy


class ToolGate:
    def __init__(self, policy: Policy, log: GateLog | None = None):
        self.policy = policy
        self.log = log or GateLog()

    def decide(self, request: dict, trust: str = "console") -> "Verdict":
        action = str(request.get("action", "unknown"))
        resources = [str(r) for r in (request.get("resources") or [""])]
        verdict = self.policy.decide(action, resources, trust)
        self.log.record(
            "tool-gate", verdict.decision, verdict.note,
            rule=verdict.rule, action=action, trust=trust,
            resources=resources, request_id=request.get("id", ""),
        )
        return verdict
