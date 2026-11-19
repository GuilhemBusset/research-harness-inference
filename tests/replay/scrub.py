"""Refuse a capture that is not fit for a public repository (system design, section 10).

A capture is recorded traffic (`tests/captures/README.md`), and recorded traffic carries what the
machine that recorded it knew. `check` reads a capture, as the JSON value of its file, and raises
`Refused` at the first of these it finds:

| Rule | What is refused |
| --- | --- |
| `key` | a student's API key: the shape `rhi_common.keys.KEY_PATTERN` describes, anywhere in a text |
| `host` | a host outside `example.com`: in a header that names one (`host`, `:authority`, `x-forwarded-host`), or in a URL (`scheme://host`) anywhere in a text. `example.com` and its subdomains pass |
| `address` | an IP address, version 4 or 6, anywhere in a text, after a label and its colon (`peer:`) or before a port as well |

**A host passes only as it is written for the example domain**: `example.com` or a name below it,
in ASCII letters, digits, hyphens and underscores, with a port or without. Any other writing is
refused, a name that only continues `example.com` with another character included. The host of a
URL is everything after `://`, and after whoever signs in, up to the first `/`, `?`, `#`,
backslash, `<`, `>` or white space: no other character ends it, since a client reads it as part of
the name. Only the punctuation that closes a sentence is left out, at the very end.

**Every text** is read: each name and each string of the capture, at any depth. A body is itself
JSON, or a stream of events whose `data` lines are, and JSON can write a character as an escape; so
a string that holds JSON, and a `data:` line that does, is read again as the value it holds, and
the same rules apply to it: every name and every string of it, a name written twice included. A
URL is looked for in those strings and not in the JSON that holds them, where a quote ends it. A
body written as a list of texts is read joined end to end, as it is replayed.

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
# A host of the example domain as it may be written, with a port or without. ASCII only: no other letter is one of these.
_EXAMPLE = re.compile(r"(?:[a-z0-9_-]+\.)*" + re.escape(EXAMPLE_DOMAIN) + r"\.?(?::[0-9]*)?", re.ASCII | re.IGNORECASE)
# A URL as far as its authority: `://`, whatever the scheme before it, then everything up to what ends the authority.
_URL = re.compile(r"://([^/?#\\\s<>]*)")
# Four numbers with dots between them, which no further number continues on either side.
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)\d{1,3}(?:\.\d{1,3}){3}(?!\d)(?!\.\d)")
# A run of what an IPv6 address is written with. The address may be only a part of it: see `_has_address`.
_IPV6 = re.compile(r"[0-9A-Fa-f:.]+")
_WORD = re.compile(r"[0-9A-Za-z_]", re.ASCII)
# An IPv6 address has at most eight groups, and `::` at one end writes one more colon.
_IPV6_PIECES = 9


class Refused(ValueError):
    """A capture holds what it may not. `rule` is the rule, `where` the place; the value is in neither."""

    def __init__(self, rule: str, where: str) -> None:
        super().__init__(f"the capture is refused by the rule `{rule}`, at {where}")
        self.rule = rule
        self.where = where


class _Object(list[tuple[str, object]]):
    """A JSON object as it is written: each name with its value, a name written twice kept twice."""


def _texts(value: object, where: str) -> Iterator[tuple[str, str, str]]:
    """Every text of a JSON value, the texts of the JSON its strings hold included.

    Each comes as the place it stands at, the text, and the part of it that is read as plain
    text: what is left of it once the JSON it holds is taken out, to be read string by string.
    """
    if isinstance(value, (dict, _Object)):
        for name, inner in value.items() if isinstance(value, dict) else value:
            yield from _texts(name, where)
            yield from _texts(inner, f"{where}.{name}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _texts(inner, f"{where}[{index}]")
    elif isinstance(value, str):
        held = _json(value)
        if held is not None:
            # The strings first: a URL in one of them is refused as a host before the address it may name.
            yield from _texts(held, where)
            yield where, value, ""
            return
        plain = []
        # At line feeds only: a string of an event's data may hold a character that Python calls a line's end.
        for line in value.split("\n"):
            event = _json(line.removeprefix("data:")) if line.startswith("data:") else None
            if event is None:
                plain.append(line)
            else:
                yield from _texts(event, where)
        yield where, value, "\n".join(plain)


def _json(text: str) -> object:
    """The object, the array or the string that this text is as JSON; `None` for any other text."""
    try:
        value = json.loads(text, object_pairs_hook=_Object)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, (list, str)) else None


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
    """Whether a host, with a port or without, is written as `example.com` or as a name below it."""
    return _EXAMPLE.fullmatch(host.strip()) is not None


def _has_foreign_url(text: str) -> bool:
    """Whether a plain text holds a URL whose host is not of the example domain."""
    for found in _URL.finditer(text):
        # Whoever signs in stands before the last `@`; the host is all that follows, whatever it is written with.
        host = found.group(1).rpartition("@")[2]
        # What closes a sentence or a quotation after the URL is not of the name; a letter of any alphabet is.
        while host and not host[-1].isalnum():
            host = host[:-1]
        # A URL with no authority, as `file:///` is, names no host.
        if host and not _is_example(host):
            return True
    return False


def _is_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def _has_ipv6(text: str) -> bool:
    """Whether a text holds an IPv6 address, alone or with a label before it or a port after it.

    The address is looked for in each run of the characters one is written with, as the whole run
    and as any of its groups in a row: `peer:` before it and `:8443` after it are of the same run.
    An end of the address that is an end of the run stands apart from any word (`std::vector` holds
    no address), and an end inside the run stands at a colon.
    """
    for found in _IPV6.finditer(text):
        pieces = found.group().split(":")
        if len(pieces) < 3:
            continue
        word_before = found.start() > 0 and _WORD.match(text[found.start() - 1]) is not None
        word_after = found.end() < len(text) and _WORD.match(text[found.end()]) is not None
        for start in range(len(pieces) - 2):
            if start == 0 and word_before:
                continue
            for end in range(start + 3, min(start + _IPV6_PIECES, len(pieces)) + 1):
                if end == len(pieces) and word_after:
                    continue
                if _is_ipv6(":".join(pieces[start:end]).strip(".")):
                    return True
    return False


def _has_address(text: str) -> bool:
    for found in _IPV4.finditer(text):
        if all(int(part) <= 255 for part in found.group().split(".")):
            return True
    return _has_ipv6(text)


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
    for where, text, plain in _texts(capture, "capture"):
        if _KEY.search(text):
            raise Refused(KEY, where)
        if _has_foreign_url(plain):
            raise Refused(HOST, where)
        if _has_address(text):
            raise Refused(ADDRESS, where)
