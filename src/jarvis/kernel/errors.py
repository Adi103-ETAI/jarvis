"""Kernel errors -- the structured failure type the whole active layer reads."""

from __future__ import annotations


class KernelError(RuntimeError):
    """A structured error returned by, or about, the kernel."""

    def __init__(self, message: str, *, status: int = 0, tag: str = ""):
        super().__init__(message)
        self.message = message   # callers read e.message directly (console, gates)
        self.status = status
        self.tag = tag
