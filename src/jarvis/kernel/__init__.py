"""Kernel adapter: the stable ABI the active layer uses to talk to OpenCode.

Modules: config.py (where/who), http.py (transport + SSE), adapter.py
(the operation surface), events.py (EventPipe), errors.py (KernelError).
`from jarvis.kernel.adapter import ...` remains the canonical import;
the package re-exports the public names for convenience.
"""

from jarvis.kernel.adapter import EventPipe, Kernel, KernelError

__all__ = ["Kernel", "KernelError", "EventPipe"]
