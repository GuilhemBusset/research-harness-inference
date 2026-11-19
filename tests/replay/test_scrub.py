"""What the capture scrubber refuses and what it lets through (`tests/replay/scrub.py`).

Every key is generated when the test runs: no file holds one. The hosts and the addresses are of
the ranges reserved for documentation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

import scrub
from rhi_common.keys import generate_key


def capture(**changes: Any) -> dict[str, Any]:
    """A clean capture of a stream, with the parts given in place of its own."""
    clean = {
        "request": {
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": {"host": "gateway.example.com", "content-type": "application/json"},
            "body": json.dumps({"model": "mock", "stream": True, "messages": [{"role": "user", "content": "Hello."}]}),
        },
        "answer": {
            "status": 200,
            "headers": {"content-type": "text/event-stream"},
            "body": ['data: {"choices":[{"index":0,"delta":{"content":"Hello."}}]}\n', "\n", "data: [DONE]\n", "\n"],
        },
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    return {**clean, **changes}


def in_a_header(text: str) -> dict[str, Any]:
    found = capture()
    found["request"]["headers"]["x-note"] = f"sent with {text}"
    return found


def in_the_request(text: str) -> dict[str, Any]:
    found = capture()
    found["request"]["body"] = json.dumps({"model": "mock", "messages": [{"role": "user", "content": f"see {text}."}]})
    return found


def escaped_in_the_request(text: str) -> dict[str, Any]:
    # The body's own JSON writes every character as an escape: the text is nowhere in the file as it reads.
    escaped = "".join(f"\\u{ord(character):04x}" for character in text)
    found = capture()
    found["request"]["body"] = '{"model":"mock","messages":[{"role":"user","content":"' + escaped + '"}]}'
    return found


def in_an_event(text: str) -> dict[str, Any]:
    event = json.dumps({"choices": [{"index": 0, "delta": {"content": json.dumps({"path": text})}}]})
    return capture(answer={"status": 200, "headers": {}, "body": [f"data: {event}\n", "\n"]})


def as_a_name(text: str) -> dict[str, Any]:
    return capture(forward={"model": "mock", "metadata": {text: True}})


def cut_in_two_in_the_answer(text: str) -> dict[str, Any]:
    # A body written as texts to join: neither text holds the whole of it.
    middle = len(text) // 2
    return capture(answer={"status": 200, "headers": {}, "body": [f"see {text[:middle]}", f"{text[middle:]}\n"]})


PLACES = [in_a_header, in_the_request, escaped_in_the_request, in_an_event, as_a_name, cut_in_two_in_the_answer]


def refused(found: dict[str, Any]) -> scrub.Refused:
    with pytest.raises(scrub.Refused) as refusal:
        scrub.check(found)
    return refusal.value


def test_a_clean_capture_passes() -> None:
    scrub.check(capture())


@pytest.mark.parametrize("place", PLACES, ids=lambda place: place.__name__)
def test_a_key_is_refused_wherever_it_stands(place: Callable[[str], dict[str, Any]]) -> None:
    key = generate_key()

    refusal = refused(place(key))

    assert refusal.rule == scrub.KEY
    # The refusal says where, and never what.
    assert key not in str(refusal)
    assert refusal.where in str(refusal)


@pytest.mark.parametrize(
    "host",
    [
        "example.org",
        "gateway.example.net:8443",
        "example.com.attacker.test",
        "notexample.com",
        "localhost",
        "",
        # A name that only ends, or only begins, as the example domain does.
        "example.com\u00e9.test",
        "attacker.test/.example.com",
    ],
)
def test_a_host_header_outside_the_example_domain_is_refused(host: str) -> None:
    found = capture()
    found["request"]["headers"] = {"Host": host}

    assert refused(found).rule == scrub.HOST


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.org/v1/messages",
        "http://example.com.attacker.test/",
        "https://example.com@attacker.test/",
        "wss://[2001:db8::1]:8443/stream",
        "http://192.0.2.7:8000/v1/models",
        # The name goes on after `example.com`, with a character that is no ASCII letter: a client reads the whole of it.
        "https://example.com\u00e9.test/path",
        "https://example.com\u00ad.attacker.test/",
        'https://example.com"attacker.test/',
        "https://example.com<attacker.test/",
        # An accent that joins the last letter is of the name, before a path and at the very end; so is a dot of another alphabet.
        "https://example.com\u0300/path",
        "https://example.com\u0300",
        "https://example.com\u3002attacker.test/",
        # A backslash: one client ends the host there, another reads what stands before it as whoever signs in.
        "https://example.com\\@attacker.test/",
        "https://attacker.test\\@example.com/",
    ],
)
@pytest.mark.parametrize("place", PLACES, ids=lambda place: place.__name__)
def test_a_url_outside_the_example_domain_is_refused(place: Callable[[str], dict[str, Any]], url: str) -> None:
    refusal = refused(place(url))

    assert refusal.rule == scrub.HOST
    assert url not in str(refusal)


@pytest.mark.parametrize(
    "address",
    [
        "192.0.2.7",
        "198.51.100.255",
        "2001:db8::1",
        "::1",
        "::ffff:192.0.2.7",
        # After a label and its colon, and with a port after the last group.
        "peer:192.0.2.7",
        "peer:2001:db8::1",
        "peer:2001:db8:0:0:0:0:0:1:8443",
    ],
)
@pytest.mark.parametrize("place", PLACES, ids=lambda place: place.__name__)
def test_an_address_is_refused_wherever_it_stands(place: Callable[[str], dict[str, Any]], address: str) -> None:
    refusal = refused(place(address))

    assert refusal.rule == scrub.ADDRESS
    assert address not in str(refusal)


@pytest.mark.parametrize(
    "text",
    [
        # Hosts of the example domain, in a URL and with a port.
        "https://example.com/docs",
        "https://Gateway.Example.COM:8443/v1/messages?beta=true",
        "(see https://docs.example.com.)",
        'say "https://example.com", then',
        "<https://example.com>, or `https://docs.example.com`!",
        'url.startswith("https://")',
        "file:///work/README.md",
        # What is written like an address and is none: a version, a time, a scope of a language, a number.
        "version 2.1.0",
        "3.14.15",
        "999.1.1.1",
        "at 12:30:45",
        "time:12:30:45",
        "peer:unavailable",
        "std::vector<int>",
        "Vec::new()",
        "Decimal::add(1)",
        "0.25",
        # What is written like a key and is none: one character short, and a package of this repository.
        "rhi_" + "a" * 45,
        "rhi_common.keys",
    ],
)
@pytest.mark.parametrize("place", PLACES, ids=lambda place: place.__name__)
def test_what_is_no_key_no_foreign_host_and_no_address_passes(
    place: Callable[[str], dict[str, Any]], text: str
) -> None:
    scrub.check(place(text))


def test_the_json_of_a_body_is_read_string_by_string() -> None:
    found = capture()
    # Two URLs of the example domain with only JSON between them: the quote ends the first.
    found["request"]["body"] = '{"base":"https://example.com","docs":"https://docs.example.com","model":"mock"}'
    scrub.check(found)

    # A name written twice: a JSON reader keeps the last value, and the file holds both.
    found["request"]["body"] = '{"base":"https://attacker.test","base":"https://example.com","model":"mock"}'
    assert refused(found).rule == scrub.HOST


def test_a_host_header_of_the_example_domain_passes() -> None:
    for host in ("example.com", "gateway.example.com", "Gateway.Example.com:8443"):
        found = capture()
        found["request"]["headers"] = {"host": host, ":authority": host, "x-forwarded-host": host}
        scrub.check(found)
