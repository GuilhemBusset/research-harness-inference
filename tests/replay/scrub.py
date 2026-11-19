"""Refuse a capture that is not fit for a public repository (system design, section 10).

A capture is recorded traffic (`tests/captures/README.md`), and recorded traffic carries what the
machine that recorded it knew. `check` reads a capture, as the JSON value of its file, and raises
`Refused` at the first of these it finds:

| Rule | What is refused |
| --- | --- |
| `key` | a student's API key: the shape `rhi_common.keys.KEY_PATTERN` describes, anywhere in a text |
| `host` | a host outside `example.com`: in a header that names one (`host`, `:authority`, `x-forwarded-host`), or in a URL (`scheme://host`) anywhere in a text. `example.com` and its subdomains pass |
| `address` | an IP address, version 4 or 6, anywhere in a text |

**Every text** is read: each name and each string of the capture, at any depth. A body is itself
JSON, or a stream of events whose `data` lines are, and JSON can write a character as an escape; so
a string that holds JSON, and a `data:` line that does, is read again as the value it holds, and
the same rules apply to it. A body written as a list of texts is read joined end to end, as it is
replayed.

A refusal names its rule and where in the capture it was found, never the value: its message is
safe to print in a log that is public.

What no pattern can see is not checked here: a real domain written without a scheme outside a
header, the name of a student, a secret of no known shape. Those stay the job of whoever records a
capture; e-mail addresses and the other credentials are refused in every file by the hygiene
scanner (`tools/hygiene/`).

Nothing here reads a file or the network.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterator

from rhi_common.keys import KEY_PATTERN

KEY = "key"
HOST = "host"
ADDRESS = "address"

# The domain a capture is recorded under: the host itself and any name below it.
EXAMPLE_DOMAIN = "example.com"

# The headers whose value is a host, with a port or without.
HOST_HEADERS = frozenset({"host", ":authority", "x-forwarded-host"})

_KEY = re.compile(KEY_PATTERN, re.ASCII)
# A URL as far as its host: the scheme, `://`, whoever signs in, then the name or the bracketed address.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://(?:[^/?#\s\"'<>\\@]*@)?(\[[^\]\s]*\]|[A-Za-z0-9._~%-]*)")
# Four numbers with dots between them, which no further number continues on either side.
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)\d{1,3}(?:\.\d{1,3}){3}(?!\d)(?!\.\d)")
# A run of what an IPv6 address is written with, standing apart from any word around it.
_IPV6 = re.compile(r"(?<![0-9A-Za-z_:.])[0-9A-Fa-f:.]+(?![0-9A-Za-z_:])")


class Refused(ValueError):
    """A capture holds what it may not. `rule` is the rule, `where` the place; the value is in neither."""

    def __init__(self, rule: str, where: str) -> None:
        super().__init__(f"the capture is refused by the rule `{rule}`, at {where}")
        self.rule = rule
        self.where = where


def _texts(value: object, where: str) -> Iterator[tuple[str, str]]:
    """Every text of a JSON value with the place it stands at, the texts of the JSON its strings hold included."""
    if isinstance(value, dict):
        for name, inner in value.items():
            yield from _texts(name, where)
            yield from _texts(inner, f"{where}.{name}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _texts(inner, f"{where}[{index}]")
    elif isinstance(value, str):
        yield where, value
        held = _json(value)
        if held is not None:
            yield from _texts(held, where)
            return
        # At line feeds only: a string of an event's data may hold a character that Python calls a line's end.
        for line in value.split("\n"):
            if line.startswith("data:"):
                event = _json(line.removeprefix("data:"))
                if event is not None:
                    yield from _texts(event, where)


def _json(text: str) -> object:
    """The object, the array or the string that this text is as JSON; `None` for any other text."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, (dict, list, str)) else None


def _headers(value: object, where: str) -> Iterator[tuple[str, str]]:
    """The value of every field that names a host, at any depth, with the place it stands at."""
    if isinstance(value, dict):
        for name, inner in value.items():
            if isinstance(inner, str) and name.lower() in HOST_HEADERS:
                yield f"{where}.{name}", inner
            yield from _headers(inner, f"{where}.{name}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _headers(inner, f"{where}[{index}]")


def _is_example(host: str) -> bool:
    """Whether a host, with a port or without, is `example.com` or a name below it."""
    name = host.strip().partition(":")[0].rstrip(".").lower()
    return name == EXAMPLE_DOMAIN or name.endswith("." + EXAMPLE_DOMAIN)


def _has_address(text: str) -> bool:
    for found in _IPV4.finditer(text):
        if all(int(part) <= 255 for part in found.group().split(".")):
            return True
    for found in _IPV6.finditer(text):
        candidate = found.group().strip(".")
        if candidate.count(":") < 2:
            continue
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError:
            continue
        return True
    return False


def _with_whole_bodies(capture: object) -> object:
    """The capture, each body that is written as a list of texts joined end to end, as it is replayed.

    What is cut between two of the texts is in neither of them, and half a URL names another host.
    """
    if not isinstance(capture, dict):
        return capture
    whole = dict(capture)
    for part in ("request", "answer"):
        held = whole.get(part)
        body = held.get("body") if isinstance(held, dict) else None
        if isinstance(held, dict) and isinstance(body, list) and all(isinstance(text, str) for text in body):
            whole[part] = {**held, "body": "".join(body)}
    return whole


def check(capture: object) -> None:
    """Raise `Refused` if the capture holds a key, a host outside `example.com`, or an address."""
    capture = _with_whole_bodies(capture)
    for where, value in _headers(capture, "capture"):
        if not _is_example(value):
            raise Refused(HOST, where)
    for where, text in _texts(capture, "capture"):
        if _KEY.search(text):
            raise Refused(KEY, where)
        # A URL with no authority, as `file:///` is, names no host.
        if any(found.group(1) and not _is_example(found.group(1)) for found in _URL.finditer(text)):
            raise Refused(HOST, where)
        if _has_address(text):
            raise Refused(ADDRESS, where)
