"""Gate A — the intent gate.

Input: raw utterance text + source (console | ambient | peer | scheduled).
Output: (verdict, reason). Verdicts:
    act      — treat this as a direct instruction to the kernel
    confirm  — probably intent, unclear enough that the human should see it
    journal  — record for context, never act (peers always get this)
    ignore   — background noise
Gate A decides WHAT KIND of thing this is; it never decides HOW to do it
(routing stays in the kernel).
"""

from __future__ import annotations

WAKE_WORDS = ("jarvis", "jai", "j.a.r.v.i.s")
IMPERATIVE_MARKERS = (
    "open ", "run ", "start ", "stop ", "kill ", "launch ", "close ", "end ",
    "delete ", "create ", "make ", "write ", "fix ", "build ", "search ", "find ",
    "install ", "remove ", "send ", "play ", "move ", "rename ", "copy ",
    "pip install", "apt ", "git ", "cd ", "reboot", "shutdown",
)
CHATTER_MARKERS = ("hmm", "nevermind", "never mind", "okay bye", "goodnight", "thanks")


def classify(text: str, source: str = "console",
             lane: str | None = None) -> tuple[str, str]:
    t = (text or "").strip()
    if not t:
        return "ignore", "empty input"

    low = t.lower()

    if source == "console":
        # Typed directly at the interface we own: assume intent. Gate B still
        # checks every action this turns into.
        return "act", "typed at the JARVIS console"

    if source == "scheduled":
        return "act", "pre-approved standing job"

    if source == "peer":
        # A peer's message is never authority (autumn-bus law, ours too).
        if any(m in low for m in CHATTER_MARKERS) and len(t) < 12:
            return "ignore", "peer chatter"
        if lane == "answer":
            # Phase 8 channels: the policy-mapped answer lane launches a
            # TEXT-ONLY turn on the peer's dm session. trust 'peer' in
            # policy.toml denies every mutation and task-spawning, so
            # this verdict admits a conversation, never an execution.
            return "act", "channel answer lane (policy pre-approved; trust 'peer' denies mutation)"
        return "journal", "peer message recorded for review, never acted on"

    if source == "ambient":
        if _has_wake(low):
            if any(m in low for m in IMPERATIVE_MARKERS):
                return "act", "wake word + clear imperative"
            if any(m in low for m in CHATTER_MARKERS) and len(t) < 24:
                return "ignore", "awoke on chatter"
            return "confirm", "wake word present but instruction unclear — show the human"
        # special case: a bare question mark aimed at the room
        return "ignore", "no wake word — ambient speech"

    return "confirm", f"unknown source '{source}' — default to showing a human"


def _has_wake(low: str) -> bool:
    if any(low.startswith(w) for w in WAKE_WORDS):
        return True
    return any(f" {w} " in low or f"hey {w}" in low for w in WAKE_WORDS)
