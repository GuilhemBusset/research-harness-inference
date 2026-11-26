"""A scripted stand-in for the pool, for the tests that drive a student harness without a model
(docs/design/agent-session-evaluation.md, "Course quality and the grader").

A harness that works makes its next tool action from the answer it was really given. To show that
without a model, a test puts this server behind the trial's forwarder (`evaluate.forwarding`) in
place of the gateway and gives it a script. It answers on the three routes that generate, in each
one's own wire format, as a stream of events or as one JSON document, whichever the request asked
for. No cloud account, model or key is involved: every answer is made here.

**The script** is a list of conversations (`Conversation`). Each prompt given in one conversation
is a `Phase`: the first prompt, then each follow-up. A phase is told by a word its prompt holds,
its marker, which the test makes up, and a request belongs to the conversation whose markers its
prompts hold; a delegated worker has a prompt, and so a conversation, of its own. A phase is a
list of steps, and a step is a function from the results of the step before it to what the model
says next: a text that ends the turn (`Say`), or one or several tool calls.

**A step is served only to a request that carries what came before it.** Each tool call is given
an id of this server's. A request is answered with step *k* of a phase only when it carries a
result for every call of the steps before *k*, by those ids, and the step is given the results of
step *k - 1* as that request holds them: what the next action is made from is what the client sent
back. A follow-up phase is served only to a request that still carries the markers of the phases
before it and a result for each of their calls. A request that names a later phase without them is
refused (400) and noted in `faults` as `context_missing`.

**The tool calls** are written for the tools the request declares, since each harness names its
own. `Shell` is a command line for the harness's shell tool, `Read` and `Write` use the shell tool
too, or the file tools of the SDK tool loop where there is no shell tool, `Delegate` starts a
worker with Claude Code's agent tool, and `Raw` is a call written as given, a malformed one
included. The arguments follow the schema the tool was declared with.

**What it keeps** (`seen`): for each request, its route, the model and the key it arrived with,
and the conversation, phase and step it was answered as. A request that declares no tool is a
side query and is answered with a word. It needs the standard library and `evaluate.contracts`.
"""

from __future__ import annotations

import json
import shlex
import threading
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Self

from evaluate.contracts import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

ROUTES = {
    "/v1/messages": Route.MESSAGES,
    "/v1/responses": Route.RESPONSES,
    "/v1/chat/completions": Route.CHAT_COMPLETIONS,
}
# The shell tool of each harness, and Claude Code's tools that start a worker, by their names.
SHELL_TOOLS = ("Bash", "bash", "shell", "shell_command", "exec_command")
AGENT_TOOLS = ("Agent", "Task")
# The labels of `faults`.
CONTEXT_MISSING = "context_missing"
UNKNOWN_CONVERSATION = "unknown_conversation"
NO_TOOL = "no_tool"


@dataclass(frozen=True)
class Say:
    """A text that ends the turn."""

    text: str


@dataclass(frozen=True)
class Shell:
    """A command line for the harness's shell tool."""

    command: str


@dataclass(frozen=True)
class Read:
    """Reads a file of the working folder."""

    path: str


@dataclass(frozen=True)
class Write:
    """Writes a file of the working folder."""

    path: str
    text: str


@dataclass(frozen=True)
class Delegate:
    """Starts a worker with that prompt."""

    prompt: str


@dataclass(frozen=True)
class Raw:
    """A tool call as written: its name, and its arguments as the text the answer carries."""

    name: str
    arguments: str


type Use = Shell | Read | Write | Delegate | Raw
type Step = Callable[[Sequence[str]], Say | Use | Sequence[Use]]


@dataclass(frozen=True)
class Phase:
    """One prompt of a conversation, told by `marker`, a word it holds, and what the model does
    with it."""

    marker: str
    steps: Sequence[Step]


@dataclass(frozen=True)
class Conversation:
    """The prompts one agent is given, in order: its first, then each follow-up."""

    phases: Sequence[Phase]


@dataclass(frozen=True)
class Seen:
    """One request the pool received. `conversation`, `phase` and `step` say what it was answered
    as; they are `None` for a side query, a request that generates nothing and one that was refused."""

    route: Route | None
    model: object
    authorization: str | None
    conversation: int | None = None
    phase: int | None = None
    step: int | None = None


@dataclass(frozen=True)
class _Asked:
    """A generating request, read in its wire format."""

    stream: bool
    tools: Mapping[str, Mapping[str, Any]]
    """The schema of each tool the request declares, by its name."""
    prompts: str
    """What the user wrote, every prompt of the conversation joined."""
    results: Mapping[str, str]
    """The text of each tool result, by the id of its call."""


def _text(content: object) -> str:
    """The text of a message's content: a string, or the parts of it that hold text."""
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    texts = [part.get("text") for part in parts if isinstance(part, dict)]
    return "\n".join(text for text in texts if isinstance(text, str))


def _read(route: Route, body: Mapping[str, Any]) -> _Asked:
    declared = [tool for tool in body.get("tools") or [] if isinstance(tool, dict)]
    prompts: list[str] = []
    results: dict[str, str] = {}
    tools: dict[str, Mapping[str, Any]] = {}
    if route is Route.MESSAGES:
        tools = {tool["name"]: tool.get("input_schema") or {} for tool in declared if "name" in tool}
        for message in body.get("messages") or []:
            content = message.get("content")
            blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
            for block in blocks:
                if block.get("type") == "tool_result":
                    results[str(block.get("tool_use_id"))] = _text(block.get("content"))
            if message.get("role") == "user":
                own = [block for block in blocks if block.get("type") == "text"]
                prompts.append(content if isinstance(content, str) else _text(own))
    elif route is Route.RESPONSES:
        tools = {tool["name"]: tool.get("parameters") or {} for tool in declared if tool.get("type") == "function"}
        given = body.get("input")
        for item in [{"role": "user", "content": given}] if isinstance(given, str) else given or []:
            if item.get("type") in ("function_call_output", "custom_tool_call_output"):
                output = item.get("output")
                results[str(item.get("call_id"))] = output if isinstance(output, str) else _text(output)
            elif item.get("role") == "user":
                prompts.append(_text(item.get("content")))
    else:
        functions = [tool.get("function") or {} for tool in declared]
        tools = {function["name"]: function.get("parameters") or {} for function in functions if "name" in function}
        for message in body.get("messages") or []:
            if message.get("role") == "tool":
                results[str(message.get("tool_call_id"))] = _text(message.get("content"))
            elif message.get("role") == "user":
                prompts.append(_text(message.get("content")))
    return _Asked(body.get("stream") is True, tools, "\n".join(prompts), results)


# The tool calls, for the tools a request declares.


def _properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The schema of each argument a tool's schema names; an argument's own is empty when it is none."""
    held = schema.get("properties")
    properties = held if isinstance(held, dict) else {}
    return {name: one if isinstance(one, dict) else {} for name, one in properties.items()}


def _filled(schema: Mapping[str, Any], given: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments for a tool of that schema: `given`, and something for whatever else it requires."""
    properties = _properties(schema)
    empty = {"string": "scripted", "integer": 1, "number": 1, "boolean": False, "array": [], "object": {}}
    filled = dict(given)
    for name in schema.get("required") or []:
        kind = properties.get(name, {}).get("type")
        if name not in filled and isinstance(kind, str) and kind in empty:
            filled[name] = empty[kind]
    return filled


def _command(schema: Mapping[str, Any], line: str) -> dict[str, Any]:
    """A command line as the arguments of a shell tool: under the name its schema gives a command,
    and as the words of a shell's call where the schema asks for a list."""
    properties = _properties(schema)
    name = next((name for name in ("command", "cmd") if name in properties), "command")
    words = properties.get(name, {}).get("type") == "array"
    return _filled(schema, {name: ["bash", "-lc", line] if words else line})


def _call(use: Use, tools: Mapping[str, Mapping[str, Any]]) -> tuple[str, str] | None:
    """A tool call's name and its arguments as text; `None` when the request declares no tool for it."""
    if isinstance(use, Raw):
        return use.name, use.arguments
    if isinstance(use, Delegate):
        name = next((name for name in AGENT_TOOLS if name in tools), None)
        given = {"description": "scripted worker", "prompt": use.prompt, "subagent_type": "general-purpose"}
        return None if name is None else (name, json.dumps(_filled(tools[name], given)))
    shell = next((name for name in SHELL_TOOLS if name in tools), None)
    if shell is not None:
        if isinstance(use, Read):
            line = f"cat -- {shlex.quote(use.path)}"
        elif isinstance(use, Write):
            line = f"printf %s {shlex.quote(use.text)} > {shlex.quote(use.path)}"
        else:
            line = use.command
        return shell, json.dumps(_command(tools[shell], line))
    if isinstance(use, Read) and "read_file" in tools:
        return "read_file", json.dumps({"path": use.path})
    if isinstance(use, Write) and "write_file" in tools:
        return "write_file", json.dumps({"path": use.path, "content": use.text})
    return None


# The answers, in each wire format: as one document, and as the stream of events that builds it.

type Part = str | tuple[str, str, str]
"""A part of an answer: a text, or a tool call's id, name and arguments."""


def _event(data: object, name: str | None = None) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data)
    return (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()


def _named(events: Sequence[dict[str, Any]]) -> list[bytes]:
    return [_event(event, event["type"]) for event in events]


def _parsed(arguments: str) -> object:
    """A call's arguments as a value, for the formats that carry them as one in a document."""
    try:
        return json.loads(arguments)
    except ValueError:
        return {}


def _messages(parts: Sequence[Part], model: str) -> tuple[dict[str, Any], list[bytes]]:
    calls = any(not isinstance(part, str) for part in parts)
    content: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if isinstance(part, str):
            block: dict[str, Any] = {"type": "text", "text": part}
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": part}
        else:
            block = {"type": "tool_use", "id": part[0], "name": part[1], "input": _parsed(part[2])}
            start, delta = {**block, "input": {}}, {"type": "input_json_delta", "partial_json": part[2]}
        content.append(block)
        events += [
            {"type": "content_block_start", "index": index, "content_block": start},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ]
    stop = "tool_use" if calls else "end_turn"
    document = {
        "id": "msg_scripted",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": len(parts)},
    }
    opening = {**document, "content": [], "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
    ending = {"stop_reason": stop, "stop_sequence": None}
    events = [
        {"type": "message_start", "message": opening},
        *events,
        {"type": "message_delta", "delta": ending, "usage": {"output_tokens": len(parts)}},
        {"type": "message_stop"},
    ]
    return document, _named(events)


def _responses(parts: Sequence[Part], model: str) -> tuple[dict[str, Any], list[bytes]]:
    items: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if isinstance(part, str):
            text = {"type": "output_text", "text": part, "annotations": []}
            item = {
                "type": "message",
                "id": f"msg_{index}",
                "role": "assistant",
                "status": "completed",
                "content": [text],
            }
            at = {"item_id": item["id"], "output_index": index, "content_index": 0}
            inner = [
                {"type": "response.content_part.added", **at, "part": {**text, "text": ""}},
                {"type": "response.output_text.delta", **at, "delta": part},
                {"type": "response.output_text.done", **at, "text": part},
                {"type": "response.content_part.done", **at, "part": text},
            ]
            opened = {**item, "status": "in_progress", "content": []}
        else:
            item = {
                "type": "function_call",
                "id": f"fc_{index}",
                "call_id": part[0],
                "name": part[1],
                "arguments": part[2],
                "status": "completed",
            }
            at = {"item_id": item["id"], "output_index": index}
            inner = [
                {"type": "response.function_call_arguments.delta", **at, "delta": part[2]},
                {"type": "response.function_call_arguments.done", **at, "arguments": part[2]},
            ]
            opened = {**item, "status": "in_progress", "arguments": ""}
        items.append(item)
        events += [
            {"type": "response.output_item.added", "output_index": index, "item": opened},
            *inner,
            {"type": "response.output_item.done", "output_index": index, "item": item},
        ]
    document = {
        "id": "resp_scripted",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": model,
        "output": items,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": len(parts),
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 1 + len(parts),
        },
    }
    opening = {**document, "status": "in_progress", "output": [], "usage": None}
    events = [
        {"type": "response.created", "response": opening},
        *events,
        {"type": "response.completed", "response": document},
    ]
    return document, _named([{**event, "sequence_number": number} for number, event in enumerate(events)])


def _chat(parts: Sequence[Part], model: str) -> tuple[dict[str, Any], list[bytes]]:
    texts = [part for part in parts if isinstance(part, str)]
    calls = [
        {"id": part[0], "type": "function", "function": {"name": part[1], "arguments": part[2]}}
        for part in parts
        if not isinstance(part, str)
    ]
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
    if calls:
        message["tool_calls"] = calls
    finish = "tool_calls" if calls else "stop"
    usage = {"prompt_tokens": 1, "completion_tokens": len(parts), "total_tokens": 1 + len(parts)}
    document = {"id": "chatcmpl-scripted", "object": "chat.completion", "created": 0, "model": model}
    chunk = {**document, "object": "chat.completion.chunk"}
    deltas: list[dict[str, Any]] = [{"role": "assistant", "content": ""}]
    deltas += [{"content": text} for text in texts]
    deltas += [{"tool_calls": [{"index": index, **call}]} for index, call in enumerate(calls)]
    events = [_event({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}) for delta in deltas]
    events += [
        _event({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}),
        _event({**chunk, "choices": [], "usage": usage}),
        _event("[DONE]"),
    ]
    choice = {"index": 0, "message": message, "finish_reason": finish}
    return {**document, "choices": [choice], "usage": usage}, events


_FORMATS = {Route.MESSAGES: _messages, Route.RESPONSES: _responses, Route.CHAT_COMPLETIONS: _chat}


# The pool.


class Pool:
    """A local server that answers as its script says, inside its `with` block. `url` is what a
    forwarder is given as the gateway's address."""

    def __init__(self, conversations: Sequence[Conversation], *, model: str = "scripted-pool") -> None:
        self._conversations = tuple(conversations)
        self.model = model
        self.seen: list[Seen] = []
        self.faults: list[str] = []
        self._lock = threading.Lock()
        # What each step served was answered with, so that a request sent again gets the same
        # answer: the parts of the answer, by conversation, phase and step.
        self._served: dict[tuple[int, int, int], list[Part]] = {}
        pool = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                pool._serve(self)

            def do_POST(self) -> None:
                pool._serve(self)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def turns(self, conversation: int = 0) -> list[tuple[int | None, int | None]]:
        """The phase and step of each request answered as that conversation, in order."""
        with self._lock:
            return [(one.phase, one.step) for one in self.seen if one.conversation == conversation]

    # Serving.

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        handler.close_connection = True
        raw = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        route = ROUTES.get(urllib.parse.urlsplit(handler.path).path) if handler.command == "POST" else None
        seen = Seen(route, body.get("model") if isinstance(body, dict) else None, handler.headers.get("Authorization"))
        if route is None or not isinstance(body, dict):
            # What generates nothing: a token count, the list of models.
            with self._lock:
                self.seen.append(seen)
            listed = {"object": "list", "data": [{"id": self.model, "object": "model", "default": True}]}
            self._send(handler, HTTPStatus.OK, listed if handler.command == "GET" else {"input_tokens": 1})
            return
        asked = _read(route, body)
        parts, where = self._answer(asked)
        with self._lock:
            self.seen.append(seen if where is None else Seen(seen.route, seen.model, seen.authorization, *where))
        if parts is None:
            refusal = {"type": "error", "error": {"type": "invalid_request_error", "message": CONTEXT_MISSING}}
            self._send(handler, HTTPStatus.BAD_REQUEST, refusal)
            return
        document, events = _FORMATS[route](parts, self.model)
        if not asked.stream:
            self._send(handler, HTTPStatus.OK, document)
            return
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for event in events:
            handler.wfile.write(event)
            handler.wfile.flush()

    def _send(self, handler: BaseHTTPRequestHandler, status: int, document: object) -> None:
        payload = json.dumps(document).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)

    def _fault(self, label: str) -> None:
        with self._lock:
            self.faults.append(label)

    def _ids(self, where: tuple[int, int, int]) -> list[str] | None:
        """The ids of the calls a step was answered with; `None` for a step not served yet."""
        with self._lock:
            parts = self._served.get(where)
        return None if parts is None else [part[0] for part in parts if not isinstance(part, str)]

    def _answer(self, asked: _Asked) -> tuple[list[Part] | None, tuple[int, int, int] | None]:
        """The parts of the answer to a request, and the conversation, phase and step it is; no
        parts for a request that is refused."""
        if not asked.tools:
            return ["ok"], None
        found = [
            (index, conversation)
            for index, conversation in enumerate(self._conversations)
            if any(phase.marker in asked.prompts for phase in conversation.phases)
        ]
        if not found:
            self._fault(UNKNOWN_CONVERSATION)
            return ["ok"], None
        index, conversation = found[0]
        phase = max(at for at, one in enumerate(conversation.phases) if one.marker in asked.prompts)
        # A later prompt is answered only with the conversation before it: every earlier prompt,
        # and a result for every call made for it.
        for earlier in range(phase):
            steps = range(len(conversation.phases[earlier].steps))
            issued = [self._ids((index, earlier, step)) or [] for step in steps]
            carried = all(one in asked.results for ids in issued for one in ids)
            if conversation.phases[earlier].marker not in asked.prompts or not carried:
                self._fault(CONTEXT_MISSING)
                return None, None
        # The step: the first whose predecessor's calls the request does not all carry a result for.
        step, given = 0, []
        for after in range(1, len(conversation.phases[phase].steps)):
            ids = self._ids((index, phase, after - 1))
            if not ids or any(one not in asked.results for one in ids):
                break
            step, given = after, [asked.results[one] for one in ids]
        where = (index, phase, step)
        with self._lock:
            parts = self._served.get(where)
        if parts is None:
            parts = list(self._parts(conversation.phases[phase].steps[step](given), asked, where))
            with self._lock:
                parts = self._served.setdefault(where, parts)
        return parts, where

    def _parts(self, action: Say | Use | Sequence[Use], asked: _Asked, where: tuple[int, int, int]) -> Iterator[Part]:
        if isinstance(action, Say):
            yield action.text
            return
        uses = [action] if isinstance(action, Shell | Read | Write | Delegate | Raw) else list(action)
        for number, use in enumerate(uses):
            call = _call(use, asked.tools)
            if call is None:
                self._fault(NO_TOOL)
                yield "no tool"
            else:
                yield ("call_" + "_".join(map(str, (*where, number))), *call)
