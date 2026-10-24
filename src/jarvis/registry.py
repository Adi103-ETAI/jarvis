"""Session registry — the subject rule.

Hard rule learned live in Phase 1: jarvisd may ONLY signal (interrupt /
delete / model-switch / permission-reply) sessions that JARVIS itself
created. There is always somebody else's session on this machine — the
interactive one that might be running this very conversation — and an
interrupt from inside it aborts someone else's work.
"""

from __future__ import annotations

import time

from jarvis.kernel.adapter import Kernel, KernelError


class SessionRegistry:
    def __init__(self):
        self._own: dict[str, dict] = {}

    def add(self, session_id: str, **meta) -> None:
        self._own[session_id] = {"created": time.time(), **meta}

    def remove(self, session_id: str) -> None:
        self._own.pop(session_id, None)

    def owns(self, session_id: str) -> bool:
        return session_id in self._own

    def active_ids(self) -> list[str]:
        return list(self._own.keys())

    def require_own(self, session_id: str) -> None:
        if not self.owns(session_id):
            raise KernelError(f"session '{session_id}' is not a JARVIS-owned session",
                              tag="NotOwnedSession")


class OwnedKernel(Kernel):
    """Kernel that enforces the subject rule and refuses standing-authority grants."""

    def __init__(self, registry: SessionRegistry, **kwargs):
        super().__init__(**kwargs)
        self.registry = registry

    def interrupt(self, session_id: str) -> bool:
        self.registry.require_own(session_id)
        return super().interrupt(session_id)

    def delete_session(self, session_id: str) -> None:
        self.registry.require_own(session_id)
        super().delete_session(session_id)

    def switch_model(self, session_id: str, provider_id: str, model_id: str) -> dict:
        self.registry.require_own(session_id)
        return super().switch_model(session_id, provider_id, model_id)

    def reply_permission(self, session_id: str, request_id: str, decision: str,
                         *, message: str | None = None) -> None:
        self.registry.require_own(session_id)
        if decision == "always":
            # Minting standing authority is a policy change, not a runtime
            # decision. Go edit config/policy.toml instead.
            raise KernelError("decision 'always' is not grantable at runtime",
                              tag="PolicyDenied")
        super().reply_permission(session_id, request_id, decision, message=message)
