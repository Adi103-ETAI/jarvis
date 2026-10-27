"""Memory shared constants and helpers (stdlib only)."""

from __future__ import annotations

import re
import time

CORE_CATEGORIES = ("identity", "preference")   # always ride into turns
_TOKEN_RX = re.compile(r"[a-z0-9]+")

FACT_SOURCES = ("console",)   # v0 law: only the subject writes facts


def _now() -> float:
    return round(time.time(), 3)


def tokenize(text: str) -> list[str]:
    return _TOKEN_RX.findall((text or "").lower())
