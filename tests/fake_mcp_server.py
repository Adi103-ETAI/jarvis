"""Tiny MCP stdio server double for the bridge tests: line-JSON.
One read-classified tool and one mutating-classified tool; every call
echoes executed-<name> so the tests can trace it."""
import json
import sys

TOOLS = [
    {"name": "read_notes", "description": "read-only tool",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "execute_command", "description": "mutating tool",
     "inputSchema": {"type": "object", "properties": {}}},
]


def emit(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        if rid is None:
            continue  # notification: no reply
        method = req.get("method", "")
        if method == "initialize":
            emit({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"}}})
        elif method == "tools/list":
            emit({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name", "?")
            args = json.dumps(params.get("arguments") or {}, sort_keys=True)
            emit({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": "executed " + name + " " + args}],
                "isError": False}})
        else:
            emit({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601,
                            "message": "no such method: " + method}})


if __name__ == "__main__":
    main()