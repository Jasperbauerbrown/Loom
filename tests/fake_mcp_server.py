"""An in-process stdio MCP server, spawned as a subprocess by the client tests.

    python tests/fake_mcp_server.py [mode]

Modes:
    normal    (default) initialize / tools/list / tools/call / ping
    paged     tools/list answers in two pages via nextCursor
    exit      writes a line to stderr and exits immediately (the failure path)
    silent    reads and answers nothing (the request-timeout path)

Tools: `fake_read` (readOnlyHint true) and `fake_echo` (no annotations; echoes its
arguments, and returns `isError` when asked to). Stdlib only, no import of loom: this
has to be a real external process for the transport to mean anything.
"""

from __future__ import annotations

import json
import sys

PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "fake_read",
        "description": "Read something harmless.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "fake_echo",
        "description": "Echo the arguments back.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "fail": {"type": "boolean"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(request_id, payload) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def error(request_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(mode: str, message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "notifications/initialized":
        return
    if request_id is None:
        return

    if method == "initialize":
        result(
            request_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": f"fake-{mode}", "version": "1.0"},
            },
        )
    elif method == "ping":
        result(request_id, {})
    elif method == "tools/list":
        if mode == "paged":
            cursor = params.get("cursor")
            if not cursor:
                result(request_id, {"tools": TOOLS[:1], "nextCursor": "page2"})
            else:
                result(request_id, {"tools": TOOLS[1:]})
        else:
            result(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "fake_read":
            result(request_id, {"content": [{"type": "text", "text": f"contents of {args.get('path')}"}]})
        elif name == "fake_echo":
            if args.get("fail"):
                result(request_id, {"content": [{"type": "text", "text": "it went wrong"}], "isError": True})
            else:
                result(
                    request_id,
                    {
                        "content": [
                            {"type": "text", "text": json.dumps(args, sort_keys=True)},
                            {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="},
                        ]
                    },
                )
        else:
            error(request_id, -32602, f"unknown tool {name}")
    elif method == "please/sample":  # a server→client request; the client must refuse
        result(request_id, {})
    else:
        error(request_id, -32601, f"unknown method {method}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    if mode == "exit":
        sys.stderr.write("fake server: refusing to start (no credentials)\n")
        sys.stderr.flush()
        return 3
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if mode == "silent":
            continue
        handle(mode, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
