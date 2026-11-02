"""Kernel connection config -- where the kernel lives and how to authenticate.

Auth: Basic "opencode:<password>" -- password read from
~/.config/opencode/service.json (the same file the CLI itself uses),
or taken from OPENCODE_PASSWORD / the explicit argument.
Resolution order everywhere: explicit argument > env > pinned default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from jarvis.kernel.errors import KernelError

KERNEL_VERSION = "2.1.4"          # pin; bumping re-locks the API (docs/opencode-api.lock.json)
DEFAULT_BASE = "http://127.0.0.1:49374"
SERVICE_JSON = Path.home() / ".config/opencode/service.json"


def resolve_base(base_url):
    """explicit argument > OPENCODE_URL env > pinned default."""
    return (base_url or os.environ.get("OPENCODE_URL") or DEFAULT_BASE).rstrip("/")


def resolve_password(password):
    """explicit argument > OPENCODE_PASSWORD env > service.json."""
    pw = password or os.environ.get("OPENCODE_PASSWORD")
    if pw is None:
        try:
            pw = json.loads(SERVICE_JSON.read_text())["password"]
        except Exception as e:  # noqa: BLE001 - surface the real cause
            raise KernelError(f"cannot obtain kernel password: {e}") from e
    return pw
