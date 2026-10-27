"""PendingStore -- file-backed cross-process confirmations.

The in-process ConfirmationBroker serves jarvisd-native gating. MCP tool
calls arrive in a SEPARATE process (jarvis-mcp-gate), so its confirmation
channel must cross process boundaries: the bridge drops a pending file,
any jarvis console (python3 -m jarvis) resolves it with a one-time code,
and the bridge waits for the decision file. Nothing here auto-solves.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path


class PendingStore:
    # Orphaned confirmations (bridge killed mid-wait, human never decided)
    # are marked timeout and dropped from the visible queue after this long.
    STALE_AFTER = 3600.0

    def __init__(self, root=None, stale_after=None):
        # Cross-process shared location: the bridge process (spawned by the
        # kernel, arbitrary cwd) and any jarvis console must agree on the root,
        # so the default lives under HOME, not in the current directory.
        self.root = Path(root) if root else Path.home() / ".jarvis" / "pending"
        self.stale_after = (stale_after if stale_after is not None
                            else self.STALE_AFTER)
        self.root.mkdir(parents=True, exist_ok=True)

    def _item(self, code):
        return self.root / (code + ".json")

    def _decision(self, code):
        return self.root / (code + ".decide")

    # ---------------------------------------------------------------- open

    def open(self, description, server=None, tool=None, args=None):
        """Create a pending confirmation file; returns its payload dict."""
        code = secrets.token_hex(3)
        payload = {"code": code, "description": description, "server": server,
                   "tool": tool, "args": args, "ts": round(time.time(), 3),
                   "status": "pending"}
        self._item(code).write_text(json.dumps(payload))
        return payload

    # ---------------------------------------------------------------- read

    def list(self):
        """All still-pending confirmations, oldest first.

        Orphaned items -- a bridge that died before any human decided -- are
        marked "timeout" once they exceed stale_after seconds and no longer
        count as awaiting. Nothing auto-solves: this only retires lost rows.
        """
        out = []
        now = time.time()
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("status") != "pending":
                continue
            if self._decision(payload["code"]).exists():
                continue  # a decision was recorded faster than the wait loop
            if now - payload.get("ts", 0) > self.stale_after:
                self._mark(path, "timeout")
                continue
            out.append(payload)
        out.sort(key=lambda p: p.get("ts", 0))
        return out

    def _mark(self, path_or_code, status):
        """Best-effort status rewrite (unknown code -> nothing)."""
        if isinstance(path_or_code, str):
            path = self._item(path_or_code)
        else:
            path = path_or_code
        try:
            payload = json.loads(path.read_text())
            payload["status"] = status
            payload["resolved_ts"] = round(time.time(), 3)
            path.write_text(json.dumps(payload))
        except (OSError, json.JSONDecodeError):
            pass

    # ------------------------------------------------------------- resolve

    def decide(self, code, accept):
        """Reject/accept a pending confirmation from any process."""
        if not self._item(code).exists():
            return False
        self._decision(code).write_text("grant" if accept else "deny")
        return True

    def wait(self, code, timeout=240.0):
        """Used by the bridge: poll for the human decision.

        Returns "grant" / "deny" / None (timeout or unknown code).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            fd = self._decision(code)
            if fd.exists():
                verdict = fd.read_text().strip()
                payload = self._item(code)
                if payload.exists():
                    try:
                        data = json.loads(payload.read_text())
                        data["status"] = verdict
                        data["resolved_ts"] = round(time.time(), 3)
                        payload.write_text(json.dumps(data))
                    except (OSError, json.JSONDecodeError):
                        pass
                fd.unlink()
                return verdict
            if not self._item(code).exists():
                return None
            time.sleep(0.3)
        item = self._item(code)
        if item.exists():
            try:
                data = json.loads(item.read_text())
                data["status"] = "timeout"
                item.write_text(json.dumps(data))
            except (OSError, json.JSONDecodeError):
                pass
        return None

    def lookup(self, code):
        p = self._item(code)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
