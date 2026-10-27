"""jarvis-mcp-gate -- the gated MCP stdio bridge.

Design law (user direction 2026-10-26): JARVIS must know EVERYTHING and use
MCP fully; the kernel 2.1.4 gate hole is real (MCP tool calls raise no
permission requests), so this bridge carries the gate instead -- with the
SAME rules as native tools (user decision 2026-10-26: policy judges what a
call DOES, not what it is named):

  * initialize / notifications / prompts / resources  -> pass through
  * tools/list                                        -> pass through
        full discovery: JARVIS sees every tool of every server
  * tools/call, forbidden content                     -> refused inline
  * tools/call, benign under Gate B rules (allowlisted shell, reads)
                                                      -> forward, audited
  * tools/call, mutating and not benign               -> parked; a one-time
    code is dropped as a file; any jarvis console (python3 -m jarvis,
    "yes CODE" / "no CODE") decides. No decision within the timeout:
    refused. Nothing auto-solves, ever.

The bridge routes JSON-RPC lines and never inspects tool results; tool
definitions are untouched. All classification is text-editable in
config/policy.toml (benign rules + [mcp] mutating_tool_matches).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

from jarvis.audit import GateLog
from jarvis.pending_store import PendingStore
from jarvis.policy import Policy

CONFIRM_TIMEOUT = 240.0


class MCPGate:
    def __init__(self, server_name, real_command, *, store_root=None,
                 policy_path=None, audit_path=None):
        self.server_name = server_name
        self.real_command = list(real_command)
        self.policy = Policy(policy_path)
        self.store = PendingStore(store_root)
        self.log = GateLog(audit_path)
        self.proc = None
        self._out_lock = threading.Lock()

    # ------------------------------------------------------- classification

    @staticmethod
    def args_text(arguments):
        """Flatten argument values into searchable command-like text. Raw JSON
        would hide values behind quotes and braces, while the forbidden
        patterns in policy.toml expect shell-style text (start of string or
        after ; & |). Leading separators are harmless for ordinary values."""
        parts = []

        def walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)
            elif isinstance(node, str):
                parts.append(node)

        walk(arguments)
        return "; ".join(parts)[:600]

    # name hints: the args of these tools carry shell-like commands
    SHELL_HINTS = ("shell", "execute", "command", "run")
    # other mutating-ish names: edits, writes, uploads, renames...
    EDIT_HINTS = ("replace", "insert", "create", "write", "patch",
                  "put_", "edit", "rename", "remove", "delete", "touch")

    def action_for(self, tool):
        name = str(tool).lower()
        if any(h in name for h in self.SHELL_HINTS):
            return "shell"
        if any(h in name for h in self.EDIT_HINTS):
            return "edit"
        return "unknown"

    def plan(self, tool, arguments):
        """Gate B rules for an MCP call: "forward" | "reject" | "park".

        Same policies as kernel-native tools (user decision 2026-10-26):
        the tool NAME only gates the question (could this mutate?); policy
        then judges content. Benign allowlisted commands run free like
        native tools, forbidden content is refused inline, and anything
        else mutating parks for a human one-time code.
        """
        blob = self.args_text(arguments)
        forbidden = self.policy.forbidden_note(blob)
        if forbidden:
            return "reject", forbidden
        name = str(tool).lower()
        mutating = any(t.lower() in name
                       for t in self.policy.mcp_mutating_tokens())
        if not mutating:
            return "forward", "mcp read-classified tool"
        verdict = self.policy.decide(self.action_for(tool),
                                     [blob] if blob else [], "console")
        mapped = {"once": "forward", "reject": "reject",
                  "confirm": "park"}.get(verdict.decision, "park")
        return mapped, verdict.rule + " (" + verdict.note + ")"

    def classify(self, tool, arguments):
        """"plan" by class term: read | mutating | reject (kept for callers
        that only need the coarse label)."""
        plan, note = self.plan(tool, arguments)
        return {"forward": "read", "park": "mutating",
                "reject": "reject"}.get(plan, "mutating"), note

    # ------------------------------------------------------------ transport

    def _emit(self, message):
        with self._out_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def _pump_stdout(self):
        """Upstream server stdout -> our stdout, forwarded verbatim."""
        try:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                sys.stdout.write(line)
                sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def _forward(self, message):
        """Client message -> upstream server stdin (the real transport pipe)."""
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:  # type: ignore[union-attr]
                sys.stderr.write(line)
                sys.stderr.flush()
        except (OSError, ValueError):
            pass

    def start_server(self):
        self.proc = subprocess.Popen(
            self.real_command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    # -------------------------------------------------------------- dispatch

    def _reply_tool_error(self, request_id, text):
        self._emit({"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": True}})

    def dispatch(self, message):
        method = message.get("method", "")
        if method != "tools/call":
            self._forward(message)  # everything except tool calls flows through
            return

        params = message.get("params") or {}
        tool = params.get("name", "")
        arguments = params.get("arguments") or {}
        request_id = message.get("id")
        plan_kind, note = self.plan(tool, arguments)

        if plan_kind == "forward":
            self.log.record("mcp-gate", "executed", note,
                            server=self.server_name, tool=tool)
            self._forward(message)
            return

        if plan_kind == "reject":
            self.log.record("mcp-gate", "rejected", note,
                            server=self.server_name, tool=tool)
            self._reply_tool_error(request_id, "JARVIS gate refused this MCP call: " + note)
            return

        # park -> a one-time code, decided by the human at a jarvis console
        payload = self.store.open(description=("mcp " + self.server_name + "." + tool),
                                  server=self.server_name, tool=tool,
                                  args=json.dumps(arguments, default=str)[:600])
        self.log.record("mcp-gate", "awaiting-human", note,
                        server=self.server_name, tool=tool, code=payload["code"])
        sys.stderr.write("[jarvis-mcp-gate] CONFIRM REQUIRED code=" + payload["code"]
                         + " server=" + self.server_name + " tool=" + tool + "\n")
        sys.stderr.flush()
        verdict = self.store.wait(payload["code"], timeout=CONFIRM_TIMEOUT)

        if verdict == "grant":
            self.log.record("mcp-gate", "approved-by-human", "code=" + payload["code"],
                            server=self.server_name, tool=tool)
            self._forward(message)  # response flows via the stdout pump
            return

        reason = ("confirmation timed out with no decision" if verdict is None
                  else "refused by human")
        self.log.record("mcp-gate", "refused", reason + " code=" + payload["code"],
                        server=self.server_name, tool=tool)
        self._reply_tool_error(request_id, "JARVIS gate refused this MCP call ("
                               + reason + "). The human can allow it with the code"
                               " flow while this turn stays open for a retry.")

    # --------------------------------------------------------------- run loop

    def run(self):
        self.start_server()
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.dispatch(message)
        finally:
            try:
                self.proc.terminate()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass


def main(argv):
    import os
    if len(argv) < 3 or argv[1] != "--":
        sys.stderr.write("usage: jarvis-mcp-gate SERVER_NAME -- <real command...>\n")
        return 2
    gate = MCPGate(argv[0], argv[2:],
                   store_root=os.environ.get("JARVIS_PENDING_ROOT") or None,
                   policy_path=os.environ.get("JARVIS_POLICY_PATH") or None,
                   audit_path=os.environ.get("JARVIS_AUDIT_PATH") or None)
    gate.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
