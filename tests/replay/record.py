"""Record captures (`tests/captures/README.md`): the mock engine's own, or a harness's session.

**A harness's session.**

    uv run --locked python tests/replay/record.py session --upstream https://<endpoint> --out <folder>

serves a local recorder on a loopback port. Someone points a harness at it in place of the
endpoint. The recorder forwards every request to the endpoint as it came, and streams the answer
back unchanged. It saves each exchange in the order it arrived, as a capture numbered in that
order (`Session`):

- `<n>.json` for an exchange that the replay harness replays: an engine's answer (status 200) on a
  forwarded route of the gateway, which a replay gives back byte for byte and settles to the usage
  the answer reports;
- `<n>.unreplayed` for an engine's answer that a replay would not give back as it was recorded,
  such as a Chat Completions stream whose client did not ask for the usage chunk;
- `<n>.gateway` for every other exchange, in the same format: a route the gateway answers itself
  (`GET /v1/models`, ...), a path outside its table, or a refusal. The replay harness only reads
  `*.json` files, so it leaves these and the unreplayed ones out.

A session is written to a folder of its own: the recorder refuses a folder that already holds a file
or that another recorder is recording into, and never writes over a file.

Before a capture is written, every text of it is made neutral (`Neutral`): the endpoint's host and
the recorder's own address become `EXAMPLE_HOST`, the student key becomes `KEY_PLACEHOLDER`, the
recording machine's home directory becomes `NEUTRAL_HOME` and its user name becomes `NEUTRAL_USER`,
wherever any of them appears in a request or an answer, percent-encoded as in a URL or escaped as
in a JSON text as well; what the wire format itself writes (its field names, block types, media
types, roles, a pool's name, the `claude-` of a model's name, a stream's `[DONE]`) keeps a user name
that happens to be one of its words. The headers that carry the key are dropped. A body whose JSON
writes a name twice is rewritten as text. An image's or a document's base64 bytes are kept as they
are, and a capture whose decoded bytes hold the home directory, the user name or a key is not
written: the bytes as they are, and the texts of their metadata that are compressed (an image's
`zTXt`, a PDF's streams), in hexadecimal or escaped (a PDF's strings). Last, every text of the
capture is read once more, percent-decoded and through the JSON it holds, and a capture in which a
key or the home directory is left is not written.
The keep-alives the gateway wrote while it held a stream are left out of the answer, since the
engine never sent them. The usage is what the answer reports. Then the scrubber (`scrub.check`)
reads the capture: a capture it refuses is not written, and the recorder prints the rule and where
the scrubber found it, never the value.

`--pool <name>` sends every forwarded request to that pool of the endpoint, by writing the pool's
name in the `model` field of what goes upstream. The capture keeps the model the harness sent.
`--profile <name>` writes a profile into each replayable capture: the request profile of that pool
of `config.example.toml`, and the body the engine must receive with that profile applied.

**The mock engine's own captures.**

    uv run --locked python tests/replay/record.py

writes one capture for each request of `REQUESTS` into `tests/captures/`: the three wire formats,
each streamed and not. Nothing leaves the process. Each request is what a client would send to the
endpoint under an example host, with no key; it goes through the gateway's body filter of its
route, and what the filter gives is sent to the mock engine (`rhi_mock_engine`), in process. The
capture is then made of:

- the request, as written here;
- the body to forward: what the mock received, less the request ID and the cache salt, which the
  gateway writes for each request and each student;
- the answer: the mock's status, headers and bytes;
- the usage expected: what the mock itself counted, read from its `/metrics` and not from the
  answer the gateway will parse.

The mock stamps its answers with the time. It is recorded with its clock standing at `RECORDED_AT`,
so that recording again writes the same files until the mock or a filter changes. The scrubber
(`scrub.check`) reads each capture before it is written.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import getpass
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import tomllib
import zlib
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[2]
# Python gives a script its own folder and nothing else: the roots pytest is given go on the path.
_PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
_ROOTS = [str(REPO / root) for root in _PYPROJECT["tool"]["pytest"]["ini_options"]["pythonpath"]]
sys.path[:0] = [root for root in _ROOTS if root not in sys.path]

import httpx
import uvicorn
from prometheus_client.parser import text_string_to_metric_families
from starlette.types import Message, Receive, Scope, Send

import harness
import scrub
from rhi_common.engine_metrics import MOCK, Quantity, metric_names
from rhi_common.keys import KEY_PATTERN
from rhi_gateway import routes
from rhi_gateway.app import RequestBody
from rhi_gateway.errors import GatewayError
from rhi_gateway.hold import keep_alive
from rhi_mock_engine import app as engine

# The moment the mock's clock stands at while it is recorded: 2027-01-15, an invented one.
RECORDED_AT = 1_800_000_000
# The gateway's ID of each recorded request. It is left out of what is written.
REQUEST_ID = "0" * 32
# The mock's bearer secret for the time of a recording, in this process only: it guards nothing.
SECRET = "the-recorder-and-its-mock"

_HEADERS = {"host": "gateway.example.com", "content-type": "application/json", "user-agent": "rhi-capture-recorder"}
_MESSAGES_HEADERS = {**_HEADERS, "anthropic-version": "2023-06-01"}
_SYSTEM = "You are a coding assistant. Answer briefly."
_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
_READ_FILE = "Read a file of the working directory."

# The capture's name: the request's path, its headers and its body, as a client sends them. Some
# declare a tool, and the mock then answers with a call of it; `store` is a field of OpenAI's
# service, which the filters drop.
REQUESTS: dict[str, tuple[str, dict[str, str], dict[str, Any]]] = {
    "mock-messages": (
        "/v1/messages",
        _MESSAGES_HEADERS,
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": "Say hello."}],
        },
    ),
    "mock-messages-stream": (
        "/v1/messages?beta=true",
        _MESSAGES_HEADERS,
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 32000,
            "stream": True,
            "system": [{"type": "text", "text": _SYSTEM}],
            "tools": [{"name": "read_file", "description": _READ_FILE, "input_schema": _SCHEMA}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "What does the README say?"}]}],
        },
    ),
    "mock-chat-completions": (
        "/v1/chat/completions",
        _HEADERS,
        {
            "model": "mock",
            "store": False,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "What does the README say?"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read_file", "description": _READ_FILE, "parameters": _SCHEMA},
                }
            ],
        },
    ),
    "mock-chat-completions-stream": (
        "/v1/chat/completions",
        _HEADERS,
        {
            "model": "mock",
            "stream": True,
            # Asked for: the gateway hides the usage chunk from a client that did not ask, and the
            # client would then not receive the upstream's answer byte for byte.
            "stream_options": {"include_usage": True},
            "max_completion_tokens": 512,
            "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": "Say hello."}],
        },
    ),
    "mock-responses": (
        "/v1/responses",
        _HEADERS,
        {"model": "mock", "instructions": _SYSTEM, "input": "Say hello.", "max_output_tokens": 512},
    ),
    "mock-responses-stream": (
        "/v1/responses",
        _HEADERS,
        {
            "model": "mock",
            "stream": True,
            "store": False,
            "instructions": _SYSTEM,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "What does the README say?"}]}],
            "tools": [{"type": "function", "name": "read_file", "description": _READ_FILE, "parameters": _SCHEMA}],
        },
    ),
}


async def _at_once(seconds: float) -> None:
    """The mock's wait between two tokens: none, for a recording."""


def _counted(exposition: str, quantity: Quantity) -> int:
    """What the mock's counter of a quantity stands at, in the text of its `/metrics`."""
    sample = metric_names(MOCK)[quantity] + "_total"
    values = [
        found.value
        for family in text_string_to_metric_families(exposition)
        for found in family.samples
        if found.name == sample
    ]
    return int(values[0])


async def _record(name: str) -> dict[str, Any]:
    path, headers, body = REQUESTS[name]
    sent = json.dumps(body, separators=(",", ":")).encode("utf-8")
    route = routes.match("POST", path.encode("ascii"))
    if route is None or route.upstream_path is None:
        raise ValueError(f"{path} is no forwarded route of the gateway")

    async def receive() -> Message:
        return {"type": "http.request", "body": sent, "more_body": False}

    forwarded = await harness.filters()[route].read(
        RequestBody(receive), student=str(harness.STUDENT), request_id=REQUEST_ID
    )
    # A mock of its own for each capture, so that its counters hold this request and no other.
    mock_engine = engine.create_app(engine.Settings(secret=SECRET), sleep=_at_once)
    transport = httpx.ASGITransport(app=mock_engine)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine.example.com") as client:
        answer = await client.post(
            route.upstream_path,
            content=forwarded.body,
            headers={"authorization": f"Bearer {SECRET}", "content-type": "application/json"},
        )
        answer.raise_for_status()
        metrics = (await client.get("/metrics")).text
    lines = answer.text.splitlines(keepends=True)
    capture = {
        "request": {"method": "POST", "path": path, "headers": headers, "body": sent.decode("utf-8")},
        "forward": {
            field: value for field, value in json.loads(forwarded.body).items() if field not in harness.LEFT_OUT
        },
        "answer": {
            "status": answer.status_code,
            "headers": dict(answer.headers),
            # One text for each line of a stream, so that the file reads and compares line by line.
            "body": lines if len(lines) > 1 else answer.text,
        },
        "usage": {
            "input_tokens": _counted(metrics, Quantity.INPUT_TOKENS),
            "output_tokens": _counted(metrics, Quantity.OUTPUT_TOKENS),
        },
    }
    scrub.check(capture)
    return capture


def record() -> dict[str, dict[str, Any]]:
    """Every capture of `REQUESTS`, by its name, as the JSON value its file holds."""

    async def every() -> dict[str, dict[str, Any]]:
        return {name: await _record(name) for name in REQUESTS}

    # Only the name `time` of the mock's module is replaced, by a clock that stands still.
    with mock.patch.object(engine, "time", SimpleNamespace(time=lambda: float(RECORDED_AT))):
        return asyncio.run(every())


# What a recorded session's captures write in place of the machine's and the student's own values.
EXAMPLE_HOST = "gateway.example.com"
NEUTRAL_HOME = "/home/user"
NEUTRAL_USER = "user"
KEY_PLACEHOLDER = "the-student-key"  # Not in the shape of a key: the scrubber passes it.
# Headers left out of a capture's request: those that carry the key, the connection's own, and the
# cookie, which may hold the load balancer's stickiness. The host is written as `EXAMPLE_HOST`.
_UNRECORDED = frozenset(
    {
        *harness.KEY_HEADERS,
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "accept-encoding",
        "cookie",
    }
)
# Headers the recorder does not pass on between a harness and the endpoint: the connection's own.
# It asks the endpoint for an answer with no encoding, so that what it records is the bytes as sent.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "accept-encoding",
        "content-encoding",
    }
)
_DATA = re.compile(r"data: ?")
# A name of a JSON object that is an identifier is a field of a wire format or of a tool's schema
# (`max_tokens`, `maxLength`): the harness's makers wrote it, not the machine. The user name is not
# looked for in it, so that a short one (`max`) does not rename a field. Inside what a tool or a
# person wrote, the names are data like any text: a tool call's input, a request's metadata, and the
# names of a schema's properties (which its `required` list writes as texts).
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
# Fields whose values are what a tool or a person wrote, names included: a request's metadata, a
# tool's input examples, and a schema's instance values.
_DATA_FIELDS = frozenset({"metadata", "input_examples", "examples", "default", "const", "enum"})
# Fields whose text values the wire format enumerates (`image`, `image/png`, `assistant`, `end_turn`):
# the user name is not looked for in them, so that a login that is one of them changes no block.
_FORMAT_VALUES = frozenset({"type", "role", "media_type", "object", "stop_reason", "finish_reason", "status", "effort"})
# What a stream of Chat Completions ends with: the format's, whatever the user name.
_STREAM_END = "[DONE]"
# Headers of the wire format, names and values: the user name is not looked for in them either.
_FORMAT_HEADERS = frozenset({"content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta"})
# A key in the format of `rhi_common.keys`, its underscore percent-encoded or not.
_ANY_KEY = KEY_PATTERN.replace("_", "(?:_|%5[Ff])", 1)
_ANY_KEY_BYTES = re.compile(KEY_PATTERN.encode("ascii"))
_ANY_KEY_RE = re.compile(KEY_PATTERN)
_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")
# A data URL whose payload is base64: an image or a PDF as a harness sends one (`data:image/png;base64,...`).
_DATA_URL = re.compile(r"data:[^,]*;base64,", re.IGNORECASE)
_BASE64 = re.compile(r"[A-Za-z0-9+/=\s]*")
# What the last check of a capture decodes and reads (`Neutral.check`): a data URL anywhere in a text,
# and a text that is base64 and long enough to be a file's bytes.
_ANY_DATA_URL = re.compile(r"data:[^,\s\"']*;base64,([A-Za-z0-9+/=]+)", re.IGNORECASE)
_LONG_BASE64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_PNG = b"\x89PNG\r\n\x1a\n"
# A PDF's streams, and its strings written in hexadecimal (`<FEFF0074...>`) or with escapes (`(\164...)`).
_PDF_STREAM = re.compile(rb"stream\r?\n")
_PDF_HEX = re.compile(rb"<([0-9A-Fa-f\s]+)>")
_PDF_LITERAL = re.compile(rb"\(((?:\\.|[^\\()])*)\)", re.DOTALL)
_PDF_ESCAPE = re.compile(rb"\\([0-7]{1,3}|\r\n|.)", re.DOTALL)
_PDF_ESCAPED = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"\n": b"", b"\r": b"", b"\r\n": b""}
# The most bytes a compressed part of a file is inflated to, when it is read: no bomb fills the memory.
_INFLATED_MAX = 1 << 26
# What a model name begins with when it selects the default pool (`rhi_gateway.body.pool_named`).
_CLAUDE_PREFIX = "claude-"


def _strict_json(text: str) -> Any:
    """A JSON text's value; `ValueError` for a text that is not JSON, or an object that writes a name twice.

    A name written twice is read once by `json.loads`: what was rewritten from its value would not be
    what is written, so such a body is rewritten as text instead.
    """

    def pairs(found: list[tuple[str, Any]]) -> dict[str, Any]:
        value = dict(found)
        if len(value) != len(found):
            raise ValueError("a name written twice")
        return value

    return json.loads(text, object_pairs_hook=pairs)


def _inflated(data: bytes) -> bytes | None:
    """What a zlib stream at the start of these bytes inflates to; None if it is none."""
    try:
        return zlib.decompressobj().decompress(data, _INFLATED_MAX)
    except zlib.error:
        return None


def _png_texts(data: bytes) -> Iterator[bytes]:
    """The compressed texts of an image in PNG, inflated: `zTXt`, `iTXt` and the `iCCP` profile. Its pixels are left."""
    at = len(_PNG)
    while at + 12 <= len(data):
        size, kind = struct.unpack(">I4s", data[at : at + 8])
        chunk = data[at + 8 : at + 8 + size]
        at += 12 + size
        if kind in (b"zTXt", b"iCCP"):
            compressed = chunk.partition(b"\0")[2][1:]
        elif kind == b"iTXt":
            _, _, rest = chunk.partition(b"\0")
            if rest[:1] != b"\1":
                continue  # Not compressed: its text is in the bytes as they are.
            compressed = rest[2:].partition(b"\0")[2].partition(b"\0")[2]
        else:
            continue
        inflated = _inflated(compressed)
        if inflated is not None:
            yield inflated


def _pdf_parts(data: bytes) -> Iterator[bytes]:
    """A PDF's streams inflated, and its strings decoded from hexadecimal and from their escapes."""
    for found in _PDF_STREAM.finditer(data):
        inflated = _inflated(data[found.end() :])
        if inflated:
            yield inflated
    hexed = [re.sub(rb"\s", b"", found.group(1)) for found in _PDF_HEX.finditer(data)]
    yield b"\0".join(bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii")) for digits in hexed)

    def unescaped(found: re.Match[bytes]) -> bytes:
        escape = found.group(1)
        return bytes([int(escape, 8) & 0xFF]) if escape[:1].isdigit() else _PDF_ESCAPED.get(escape, escape)

    literals = [found.group(1) for found in _PDF_LITERAL.finditer(data) if b"\\" in found.group(1)]
    yield b"\0".join(_PDF_ESCAPE.sub(unescaped, literal) for literal in literals)


def _views(data: bytes, *, pdf: bool = False, depth: int = 0) -> Iterator[bytes]:
    """A file's bytes, then what its metadata writes in another form: compressed, in hexadecimal, escaped."""
    yield data
    if depth >= 2:
        return
    pdf = pdf or b"%PDF-" in data[:1024]
    parts = [*_png_texts(data)] if data.startswith(_PNG) else []
    if pdf:
        parts.extend(_pdf_parts(data))
    for part in parts:
        yield from _views(part, pdf=pdf, depth=depth + 1)


def _decoded(payload: str) -> bytes | None:
    """The bytes a base64 text holds; None if it holds none."""
    try:
        return base64.b64decode(re.sub(r"\s", "", payload) + "==")
    except ValueError:
        return None


def _all_texts(value: Any) -> Iterator[str]:
    """Every name and string of a JSON value, and of the JSON its strings hold, a name written twice included."""
    if isinstance(value, dict):
        for name, inner in value.items():
            yield from _all_texts(name)
            yield from _all_texts(inner)
    elif isinstance(value, (list, tuple)):  # A tuple is a name and its value, as the JSON read here keeps them.
        for inner in value:
            yield from _all_texts(inner)
    elif isinstance(value, str):
        yield value
        lines = (line.strip() for line in value.split("\n"))
        for text in dict.fromkeys([value, *(line.removeprefix("data:").strip() for line in lines)]):
            try:
                held = json.loads(text, object_pairs_hook=list)
            except (ValueError, RecursionError):
                continue
            if isinstance(held, (list, str)) and held != value:
                yield from _all_texts(held)


def _spelled(value: str) -> str:
    """A pattern of a literal value, each of its characters written as itself, percent-encoded as in a
    URL, or escaped as in a JSON text a string holds (`\\u0074`, `\\/`), the escapes in either letter case."""

    def hexed(digits: str) -> str:
        return "".join(f"[{digit}{digit.lower()}]" if digit.isalpha() else digit for digit in digits.upper())

    def spellings(char: str) -> str:
        written = [re.escape(char)]
        for variant in dict.fromkeys((char, char.lower(), char.upper())):
            written.append("".join("%" + hexed(f"{byte:02X}") for byte in variant.encode("utf-8")))
            units = variant.encode("utf-16-be")
            written.append("".join(r"\\u" + hexed(units[at : at + 2].hex()) for at in range(0, len(units), 2)))
        if char == "/":
            written.append(r"\\/")
        return f"(?:{'|'.join(written)})"

    return "".join(spellings(char) for char in value)


class Neutral:
    """What a recorded text has in place of the endpoint's host, the key, the home directory and the user name.

    Each value is replaced wherever it appears, inside a longer word as well, and with any of its
    characters percent-encoded as in a URL or escaped as in a JSON text that a string holds (a tool
    call's `arguments`), in one pass: what a replacement writes is not read again. A host and the
    user name match in any letter case. A body's JSON is rewritten string by string and name by
    name, so that no replacement falls on JSON's own syntax or escapes. What the wire format itself
    writes keeps the user name, so that a login that is also one of its words changes no block: a
    name that is an identifier, the values of the fields it enumerates (`_FORMAT_VALUES`), a
    stream's event names. Inside a tool call's input, a request's metadata, a tool's input examples
    and a schema's property names and instance values, every name is data.

    A base64 payload (an image's or a document's `data`, a `data:` URL) is kept byte for byte: a
    replacement inside it would corrupt the file, and a short user name occurs in any encoding by
    chance. It is decoded and read instead: one whose bytes hold the home directory, the user name
    (an author in an image's or a PDF's metadata, say) or a key is refused (`scrub.Refused`), and
    the capture is not written.
    A body that needs no replacement keeps its bytes.
    """

    def __init__(
        self, *, hosts: Sequence[str], home: str, user: str, keys: Sequence[str] = (), pools: Sequence[str] = ()
    ) -> None:
        def literal(values: Sequence[str]) -> str:
            # The longest first, so that a host with its port wins over the host alone. An empty one matches nothing.
            written = sorted({value for value in values if value}, key=len, reverse=True)
            return "|".join(_spelled(value) for value in written) or "(?!)"

        def in_bytes(values: Sequence[str], flags: int = 0) -> re.Pattern[bytes]:
            # As a file's metadata writes text: UTF-8, or UTF-16 of either order.
            spelled = [re.escape(value.encode(encoding)) for value in values for encoding in _ENCODINGS]
            return re.compile(b"|".join(spelled) or b"(?!)", flags)

        # The root of the file system is no home directory: replacing every `/` would write nonsense.
        homes = [home] if home.strip("/") else []
        keys = [key for key in keys if key]
        common = f"(?P<host>(?i:{literal(hosts)}))|(?P<key>{literal(keys)}|{_ANY_KEY})|(?P<home>{literal(homes)})"
        self._pattern = re.compile(f"{common}|(?P<user>(?i:{literal([user])}))")
        self._field_pattern = re.compile(common)
        self._home_bytes = in_bytes(homes)
        self._key_bytes = in_bytes(keys)
        self._user_bytes = in_bytes([user] if user else [], re.IGNORECASE)
        # What the last check looks for in every text of a capture (`check`), once its escapes are read.
        # What a replacement writes is left out of it: a home directory or a key it holds is no one's.
        self._keys_left = [key for key in keys if len(key) >= 8 and key not in KEY_PLACEHOLDER]
        self._homes_left = [value for value in homes if value not in NEUTRAL_HOME]
        # The names of the pools a request may name as its model: the format's, whatever the user name.
        self._pools = frozenset({*(pool.name for pool in harness.POOLS), *pools})

    def format(self, text: str) -> str:
        """A text the wire format writes: only a host, a key or the home directory is replaced in it."""
        return self._field_pattern.sub(self._replacement, text)

    def text(self, text: str) -> str:
        found = _DATA_URL.match(text)
        if found is not None and self._encoded(text[found.end() :]):
            # The bytes are kept; the media type is the format's, and a parameter (a file's name) is text.
            media, *parameters = text[len("data:") : found.end() - len(";base64,")].split(";")
            written = ";".join([self.format(media), *(self.text(parameter) for parameter in parameters)])
            return f"data:{written}{text[found.end() - len(';base64,') :]}"
        return self._pattern.sub(self._replacement, text)

    def model(self, model: str) -> str:
        """A request's model: a pool's name is the format's; a `claude-` name keeps its prefix, which selects its pool."""
        if model in self._pools:
            return self.format(model)
        if model.startswith(_CLAUDE_PREFIX):
            return _CLAUDE_PREFIX + self.text(model[len(_CLAUDE_PREFIX) :])
        return self.text(model)

    def _encoded(self, payload: str) -> bool:
        """Whether a text is a base64 payload, kept as it is; `scrub.Refused` if it holds the home directory or a key."""
        if not _BASE64.fullmatch(payload):
            return False
        try:
            decoded = base64.b64decode(payload)
        except ValueError:
            return False
        self.read_bytes(decoded, "a base64 payload")
        return True

    def read_bytes(self, decoded: bytes, where: str) -> None:
        """`scrub.Refused` if a file's bytes hold a key, the home directory or the user name, its metadata's
        compressed, hexadecimal and escaped texts included (`_views`)."""
        for view in _views(decoded):
            if _ANY_KEY_BYTES.search(view) or self._key_bytes.search(view):
                raise scrub.Refused(scrub.KEY, where)
            if self._home_bytes.search(view):
                raise scrub.Refused("home directory", where)
            if self._user_bytes.search(view):
                raise scrub.Refused("user name", where)

    def check(self, capture: Mapping[str, Any]) -> None:
        """`scrub.Refused` if a key or the home directory is left in a capture, however a text writes it.

        Each text is read as it is, percent-decoded, and through the JSON it holds (a name written
        twice included); a data URL, or a text that is long base64, is decoded and read as a file's bytes.
        """
        for text in _all_texts(capture):
            for view in (text, unquote(text)):
                if _ANY_KEY_RE.search(view) or any(key in view for key in self._keys_left):
                    raise scrub.Refused(scrub.KEY, "a text of the capture")
                if any(home in view for home in self._homes_left):
                    raise scrub.Refused("home directory", "a text of the capture")
            payloads = [found.group(1) for found in _ANY_DATA_URL.finditer(text)]
            if _LONG_BASE64.fullmatch(text.strip()):
                payloads.append(text)
            for payload in payloads:
                decoded = _decoded(payload)
                if decoded is not None:
                    self.read_bytes(decoded, "a base64 payload")

    def name(self, name: str, *, data: bool = False) -> str:
        """A name of a JSON object: a field of the format is renamed only for a host, a key or the home directory."""
        if not data and _IDENTIFIER.fullmatch(name):
            return self.format(name)
        return self.text(name)

    @staticmethod
    def _replacement(found: re.Match[str]) -> str:
        return {"host": EXAMPLE_HOST, "key": KEY_PLACEHOLDER, "home": NEUTRAL_HOME, "user": NEUTRAL_USER}[
            str(found.lastgroup)
        ]

    def value(self, value: Any, *, data: bool = False) -> Any:
        """A JSON value, each of its names and strings made neutral; `data` for what a tool or a person wrote."""
        if isinstance(value, list):
            return [self.value(inner, data=data) for inner in value]
        if not isinstance(value, dict):
            return self.text(value) if isinstance(value, str) else value
        neutral = {}
        for name, inner in value.items():
            if name == "data" and value.get("type") == "base64" and isinstance(inner, str) and self._encoded(inner):
                written = inner  # An image's or a document's bytes (Messages, `source`).
            elif name == "file_data" and isinstance(inner, str) and not _DATA_URL.match(inner) and self._encoded(inner):
                written = inner  # A file's bytes, in base64 with no data URL (Responses and Chat Completions).
            elif name == "model" and isinstance(inner, str) and not data:
                written = self.model(inner)
            elif name in _SCHEMA_MAPS and isinstance(inner, dict) and not data:
                written = {self.text(field): self.value(schema) for field, schema in inner.items()}
            elif name in _FORMAT_VALUES and isinstance(inner, str) and not data:
                written = self.format(inner)
            else:
                tool_input = name == "input" and str(value.get("type", "")).endswith("tool_use")
                written = self.value(inner, data=data or tool_input or name in _DATA_FIELDS)
            neutral[self.name(name, data=data)] = written
        return neutral

    def json_text(self, text: str) -> str:
        """A text that may be a JSON object or array: rewritten as JSON if it is one, as text if not."""
        try:
            held = _strict_json(text)
        except ValueError:
            return self.text(text)
        if not isinstance(held, (dict, list)):
            return self.text(text)
        neutral = self.value(held)
        return text if neutral == held else json.dumps(neutral, separators=(",", ":"), ensure_ascii=False)

    def body(self, text: str) -> str:
        """A body: JSON, or a stream whose `data:` lines are JSON, or any other text."""
        try:
            whole = _strict_json(text)
        except ValueError:
            whole = None
        if isinstance(whole, (dict, list)):
            return self.json_text(text)
        return "\n".join(self._line(line) for line in text.split("\n"))

    def _line(self, line: str) -> str:
        if line.startswith("event:"):  # A stream's event name is the format's.
            return self.format(line)
        found = _DATA.match(line)
        if found is None:
            return self.text(line)
        if line[found.end() :].rstrip("\r") == _STREAM_END:
            return line
        return found.group() + self.json_text(line[found.end() :])


@dataclass(frozen=True)
class Exchange:
    """One request of a session as the harness sent it, and the endpoint's answer as it came."""

    method: str
    target: bytes
    """The path and the query string, as the harness sent them."""
    headers: Sequence[tuple[str, str]]
    body: bytes
    status: int
    answer_headers: Sequence[tuple[str, str]]
    answer: bytes


def profile_of(pool_name: str) -> dict[str, Any]:
    """The request profile of a pool of `config.example.toml`, as a capture states it."""
    pool = next((pool for pool in harness.POOLS if pool.name == pool_name), None)
    if pool is None or not pool.request_profile:
        raise ValueError(f"config.example.toml has no pool {pool_name!r} with a request profile")
    stated: dict[str, Any] = {"request_profile": list(pool.request_profile)}
    if pool.reasoning_effort is not None:
        stated["reasoning_effort"] = dict(pool.reasoning_effort)
    return stated


def _without_keep_alives(route: routes.Route, answer: bytes) -> bytes:
    """A stream without the keep-alives the gateway wrote before the engine's first event (section 5.3)."""
    assert route.wire_format is not None
    written = keep_alive(route.wire_format).data
    while answer.startswith(written):
        answer = answer[len(written) :]
    return answer


def _usage(route: routes.Route | None, answer: bytes) -> dict[str, int]:
    """The usage an answer reports, read as its route's answers write it."""
    if route == routes.COUNT_TOKENS:
        # A token count's answer is `{"input_tokens": n}`, with no usage object.
        try:
            counted = json.loads(answer).get("input_tokens")
        except (ValueError, AttributeError):
            counted = None
        return {"input_tokens": counted if isinstance(counted, int) else 0, "output_tokens": 0}
    reported = harness.reported(answer)
    return {"input_tokens": reported.input_tokens, "output_tokens": reported.output_tokens}


async def capture_of(
    exchange: Exchange, neutral: Neutral, stated: Mapping[str, Any] | None = None
) -> tuple[bool, dict[str, Any]]:
    """The capture of one exchange, neutral and read by the scrubber, and whether the replay harness replays it.

    Raises `scrub.Refused` for what the scrubber refuses.
    """
    route = routes.match(exchange.method, exchange.target)
    replayable = route is not None and route.wire_format is not None and exchange.status == 200
    content_type = next((value for name, value in exchange.answer_headers if name.lower() == "content-type"), "")
    answer = exchange.answer
    if replayable and route is not None and content_type.split(";", 1)[0].strip().lower() == harness.STREAM_TYPE:
        answer = _without_keep_alives(route, answer)
    request_body = neutral.body(exchange.body.decode("utf-8"))
    answer_body = neutral.body(answer.decode("utf-8"))
    headers = {"host": EXAMPLE_HOST}
    for name, value in exchange.headers:
        if name.lower() in _UNRECORDED:
            continue
        written = neutral.format if name.lower() in _FORMAT_HEADERS else neutral.text
        headers[written(name.lower())] = written(value)
    # The path of a route of the gateway is the format's; a query, or a path it does not know, is text.
    path, mark, query = exchange.target.decode("latin-1").partition("?")
    target = (neutral.text(path) if route is None else neutral.format(path)) + mark + neutral.text(query)
    capture: dict[str, Any] = {
        "request": {
            "method": exchange.method,
            "path": target,
            "headers": headers,
            "body": request_body,
        }
    }
    if stated is not None and replayable and route is not None:
        sent = request_body.encode("utf-8")

        async def receive() -> Message:
            return {"type": "http.request", "body": sent, "more_body": False}

        forwarded = await harness.filters(stated)[route].read(
            RequestBody(receive), student=str(harness.STUDENT), request_id=REQUEST_ID
        )
        capture["profile"] = dict(stated)
        capture["forward"] = {
            field: value for field, value in json.loads(forwarded.body).items() if field not in harness.LEFT_OUT
        }
    lines = answer_body.splitlines(keepends=True)
    capture["answer"] = {
        "status": exchange.status,
        "headers": {"content-type": neutral.format(content_type)} if content_type else {},
        # One text for each line of a stream, so that the file reads and compares line by line.
        "body": lines if len(lines) > 1 else answer_body,
    }
    capture["usage"] = _usage(route, answer_body.encode("utf-8"))
    scrub.check(capture)
    neutral.check(capture)
    if replayable:
        # The replay runs a gateway in an event loop of its own, so in a thread of its own.
        replayable = await asyncio.to_thread(_replays, capture)
    return replayable, capture


def _replays(capture: Mapping[str, Any]) -> bool:
    """Whether a replay gives the client the recorded answer and settles once to the usage it reports."""
    read = harness.parse("recorded", capture)
    usage = read.usage
    if usage.total == 0:  # An answer that reports no usage: the gateway would answer with an error.
        return False
    try:
        replayed = harness.replay(read)
    except Exception:  # noqa: BLE001 - whatever stops a replay, the capture is not one the replay tests can replay.
        return False
    settled = (harness.Settled(usage.input_tokens, usage.output_tokens, True, usage.total),)
    return (replayed.status, replayed.received, replayed.settled) == (read.status, read.answer, settled)


def _key_presented(headers: Sequence[tuple[str, str]]) -> list[str]:
    """The key in the headers that carry one: `x-api-key`, or the credential of `authorization`.

    The scheme is read in any letter case, as the gateway reads it (`rhi_gateway.auth`); an
    `authorization` of another scheme is removed whole.
    """
    presented = []
    for name, value in headers:
        if name.lower() == "x-api-key":
            presented.append(value.strip())
        elif name.lower() == "authorization":
            scheme, _, credential = value.strip().partition(" ")
            presented.append(credential.strip() if scheme.lower() == "bearer" else value.strip())
    return presented


class Session:
    """The recorder of a session: an ASGI application to put between a harness and the endpoint.

    Every request is forwarded to `upstream` and its answer streamed back as it comes. Each exchange
    is numbered in the order its request arrived and written to `out` once its answer has ended:
    `<n>.json` when the replay harness replays it, `<n>.unreplayed` or `<n>.gateway` when not (see
    the module's text). `out` is a new folder or an empty one: a session never writes over another.
    `pool` sends the forwarded requests to that pool of the endpoint; `profile` states the request
    profile of that pool of `config.example.toml` in each replayable capture. The home directory and
    the user name replaced are the machine's own unless given.
    """

    def __init__(
        self,
        upstream: str,
        out: Path,
        *,
        pool: str | None = None,
        profile: str | None = None,
        home: str | None = None,
        user: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the upstream is an http or https URL with a host")
        # Read before the folder is held, so that a pool with no profile leaves it free.
        self._stated = None if profile is None else profile_of(profile)
        out.mkdir(parents=True, exist_ok=True)
        # The folder is held for as long as the session lasts (`close`): a second recorder, in this
        # process or another, cannot start in it, and the empty folder is checked once it is held.
        self._held: int | None = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(self._held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.close()
            raise FileExistsError(f"{out} is being recorded into by another session") from None
        if any(out.iterdir()):
            self.close()
            raise FileExistsError(f"{out} already holds files: a session is recorded into a folder of its own")
        self._upstream = upstream.rstrip("/")
        self._hosts = (parts.netloc, parts.hostname)
        self._out = out
        self._pool = pool
        self._home = str(Path.home()) if home is None else home
        self._user = getpass.getuser() if user is None else user
        self._transport = transport
        self._arrived = 0
        self.written: list[Path] = []

    def close(self) -> None:
        """Let the folder go: another session may start in it, if it is still empty."""
        if self._held is not None:
            os.close(self._held)  # The lock goes with the descriptor.
            self._held = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        self._arrived += 1
        number = self._arrived
        target = scope.get("raw_path") or scope["path"].encode("ascii")
        if scope.get("query_string"):
            target += b"?" + scope["query_string"]
        headers = [(name.decode("latin-1"), value.decode("latin-1")) for name, value in scope["headers"]]
        body = await self._request_body(receive)
        async with httpx.AsyncClient(transport=self._transport, timeout=None, trust_env=False) as client:
            sent = [(name, value) for name, value in headers if name.lower() not in _HOP_BY_HOP]
            request = client.build_request(
                scope["method"],
                self._upstream + target.decode("latin-1"),
                headers=[*sent, ("accept-encoding", "identity")],
                content=self._upstream_body(scope["method"], target, body),
            )
            answer = await client.send(request, stream=True)
            answer_headers = list(answer.headers.multi_items())
            await send(
                {
                    "type": "http.response.start",
                    "status": answer.status_code,
                    "headers": [
                        (name.encode("latin-1"), value.encode("latin-1"))
                        for name, value in answer_headers
                        if name.lower() not in _HOP_BY_HOP
                    ],
                }
            )
            received = bytearray()
            try:
                async for chunk in answer.aiter_raw():
                    received += chunk
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            finally:
                await answer.aclose()
        exchange = Exchange(scope["method"], target, headers, body, answer.status_code, answer_headers, bytes(received))
        # Written before the answer ends, so that a harness that has its answer finds its capture.
        await self._keep(number, exchange)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    async def _request_body(receive: Receive) -> bytes:
        body = bytearray()
        while True:
            message: Message = await receive()
            body += message.get("body", b"")
            if message["type"] != "http.request" or not message.get("more_body"):
                return bytes(body)

    def _upstream_body(self, method: str, target: bytes, body: bytes) -> bytes:
        """What goes upstream: the body as sent, with the pool's name as its model if a pool is given."""
        route = routes.match(method, target)
        if self._pool is None or route is None or route.wire_format is None:
            return body
        try:
            value = json.loads(body)
        except ValueError:
            return body
        if not isinstance(value, dict):
            return body
        return json.dumps({**value, "model": self._pool}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    async def _keep(self, number: int, exchange: Exchange) -> None:
        name = f"{number:03d}"
        own_host = [value for header, value in exchange.headers if header.lower() == "host"]
        neutral = Neutral(
            hosts=[*self._hosts, *own_host],
            home=self._home,
            user=self._user,
            keys=_key_presented(exchange.headers),
            pools=[self._pool] if self._pool else [],
        )
        route = routes.match(exchange.method, exchange.target)
        engine_answered = route is not None and route.wire_format is not None and exchange.status == 200
        try:
            replayable, capture = await capture_of(exchange, neutral, self._stated)
        except scrub.Refused as refused:  # Its message names the rule and the place, never the value.
            print(f"{name}: not written: {refused}", file=sys.stderr)
            return
        except (UnicodeDecodeError, GatewayError) as error:
            print(f"{name}: not written: {type(error).__name__}", file=sys.stderr)
            return
        self._out.mkdir(parents=True, exist_ok=True)
        suffix = ".json" if replayable else ".unreplayed" if engine_answered else ".gateway"
        path = self._out / f"{name}{suffix}"
        if suffix == ".unreplayed":
            print(f"{name}: a replay would not give this answer back as recorded", file=sys.stderr)
        with path.open("x", encoding="utf-8") as written:  # Never over a file already there.
            written.write(json.dumps(capture, indent=2) + "\n")
        self.written.append(path)
        print(path)


@contextmanager
def serve(app: Session, port: int = 0) -> Generator[str]:
    """The recorder served on a loopback port while the context lasts (any free one by default): its address.

    The session ends with it (`Session.close`).
    """
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", port))
        address = f"http://127.0.0.1:{listening.getsockname()[1]}"
        serving = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, name="recorder", daemon=True)
        serving.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                if not serving.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("the recorder did not start")
                time.sleep(0.01)
            yield address
        finally:
            server.should_exit = True
            serving.join(timeout=30)
            app.close()


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record the mock engine's captures, or a harness's session.")
    commands = parser.add_subparsers(dest="command")
    session = commands.add_parser("session", help="serve a recorder between a harness and the endpoint")
    session.add_argument("--upstream", required=True, help="the endpoint's URL")
    session.add_argument("--out", required=True, type=Path, help="the folder the captures are written to")
    session.add_argument("--port", type=int, default=0, help="the loopback port to listen on (any free one: 0)")
    session.add_argument("--pool", help="the pool of the endpoint every forwarded request is sent to")
    session.add_argument("--profile", help="the pool of config.example.toml whose request profile the captures state")
    given = parser.parse_args(arguments)
    if given.command is None:
        for name, capture in record().items():
            path = harness.CAPTURES / f"{name}.json"
            path.write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")
            print(path.relative_to(REPO))
        return
    recorder = Session(given.upstream, given.out, pool=given.pool, profile=given.profile)
    with serve(recorder, given.port) as address:
        print(f"Recording into {given.out}. Point the harness at {address}; stop with Ctrl-C.", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
