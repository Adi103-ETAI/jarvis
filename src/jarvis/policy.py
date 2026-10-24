"""Gate policy — declarative rules, loaded from config/policy.toml.

First match wins. Every gate verdict must trace back to a rule here;
unknown situations always default to 'confirm' (human in the loop),
never to allow-by-default.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.toml"


@dataclass
class Verdict:
    decision: str            # "once" | "reject" | "confirm"
    rule: str
    note: str


class Policy:
    def __init__(self, path: str | Path | None = None):
        p = Path(path) if path else DEFAULT_PATH
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        self._forbidden = [(re.compile(r["pattern"]), r.get("note", ""))
                           for r in data.get("forbiddens", [])]
        self._benign: list[tuple[str, re.Pattern, str]] = []
        for kind, rules in (data.get("benign") or {}).items():
            for r in rules:
                self._benign.append((kind, re.compile(r["pattern"]), r.get("note", "")))
        self._trust = data.get("trust") or {}
        self._mutating_actions = set(data.get("mutating_actions") or
                                     ["shell", "edit", "write", "webfetch",
                                      "external_directory", "task"])

    def decide(self, action: str, resources: list[str], trust: str) -> Verdict:
        blob = ";".join(str(r) for r in resources) if resources else ""
        # 1) planet-scale destructive — denied regardless of trust or confirmation
        for rx, note in self._forbidden:
            if rx.search(blob):
                return Verdict("reject", "forbiddens", note)
        # 2) allowlisted read-only commands run without the human
        rules_for_action = [entry for entry in self._benign if entry[0] == action]
        if rules_for_action and resources:
            if all(any(rx.search(res) for _, rx, _ in rules_for_action) for res in resources):
                return Verdict("once", f"benign.{action}", "allowlisted read-only command")
        # 3) mutating actions: the trust level decides the plan
        if action in self._mutating_actions:
            plan = (self._trust.get(trust) or {}).get("mutating", "confirm")
            if plan == "deny":
                return Verdict("reject", f"trust.{trust}.mutating",
                               f"trust '{trust}' may never mutate")
            if plan == "confirm":
                return Verdict("confirm", f"trust.{trust}.mutating",
                               "human confirmation required")
            if plan == "once":
                return Verdict("once", f"trust.{trust}.mutating",
                               f"trust '{trust}' grants one-shot")
        # 4) everything else — human in the loop, never allow-by-default
        return Verdict("confirm", "default", "no rule matched; human in the loop")
