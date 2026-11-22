"""The MCP server of spike S3's second session (`spikes/s3_second_session.py`): one tool, echo, which
returns the text it is given.

It speaks the Model Context Protocol over standard input and output, one JSON-RPC 2.0 message a
line, and uses the standard library only, so that Claude Code can start it with the job's Python
and nothing installed. It answers `initialize`, `ping`, `tools/list` and `tools/call`; a
notification gets no answer, and any other request the JSON-RPC error for an unknown method.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any

NAME = "rhi-echo"
VERSION = "1.0.0"
TOOL = "echo"
# The protocol versions it speaks, the newest last: it answers with the client's when it is one of them.
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

ECHO = {
    "name": TOOL,
    "description": "Returns the text it is given, unchanged.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The text to return."}},
        "required": ["text"],
    },
}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class Refused(Exception):
    """A request answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def result_of(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """The result of the request `method` with `params`. Refused for a method or a call it does not serve."""
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [ECHO]}
    if method == "tools/call":
        arguments = params.get("arguments")
        text = arguments.get("text") if isinstance(arguments, dict) else None
        if params.get("name") != TOOL or not isinstance(text, str):
            raise Refused(INVALID_PARAMS, "the server has one tool, echo, which takes a text")
        return {"content": [{"type": "text", "text": text}], "isError": False}
    raise Refused(METHOD_NOT_FOUND, "method not found")


def answer(line: str) -> dict[str, Any] | None:
    """The answer to one line of input; `None` for a notification."""
    try:
        message = json.loads(line)
    except ValueError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "parse error"}}
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        held = message.get("id") if isinstance(message, dict) else None
        return {"jsonrpc": "2.0", "id": held, "error": {"code": INVALID_REQUEST, "message": "invalid request"}}
    if "id" not in message:
        return None
    params = message.get("params")
    try:
        result = result_of(message["method"], params if isinstance(params, dict) else {})
    except Refused as refused:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": refused.code, "message": refused.message}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve(reader: IO[str], writer: IO[str]) -> None:
    """Answers each line of `reader` on `writer`, until `reader` ends."""
    for line in reader:
        if not line.strip():
            continue
        answered = answer(line)
        if answered is not None:
            writer.write(json.dumps(answered, separators=(",", ":")) + "\n")
            writer.flush()


if __name__ == "__main__":
    serve(sys.stdin, sys.stdout)
