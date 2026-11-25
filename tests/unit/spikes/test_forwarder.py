"""A client's forwarder and its home (`spikes/forwarder.py`): what the forwarder makes of a client's
exchanges, and what a client's home holds. The gateways here are local fakes, and every key is
generated; no number is a measurement.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import anthropic
import openai
import pytest

import forwarder
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_gateway.errors import WireFormat, render_stream_event
from rhi_mock_engine.app import _responses_events, _turn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

Client = forwarder.Client
MARKER = "rhi-s2-aaaaaaaa"


def ended_by_client(held: forwarder.Exchange) -> bool:
    """Whether what became of the exchange is the client's doing: its whole answer reached the
    client, or the client closed it before its end, and the gateway did not refuse it."""
    return not forwarder.refused(held) and (held.completed or held.closed_at is not None)


# A fake gateway and the forwarder.


class Gateway(ThreadingHTTPServer):
    """Answers each route as `answer` says: (status, body chunks, seconds between chunks)."""

    def __init__(self, answer: Callable[[str], tuple[int, list[bytes], float]]) -> None:
        self.bodies: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self.do_POST()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    outer.bodies.append(json.loads(self.rfile.read(length)))
                status, chunks, gap = answer(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if gap else "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for chunk in chunks:
                        time.sleep(gap)
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.daemon_threads = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def serving(answer: Callable[[str], tuple[int, list[bytes], float]]) -> Iterator[Gateway]:
    gateway = Gateway(answer)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        yield gateway
    finally:
        gateway.shutdown()
        gateway.server_close()


def refusing(path: str) -> tuple[int, list[bytes], float]:
    # A status the SDKs do not retry, so that each request is sent once.
    cause = Cause.REQUEST_REFUSED
    if path.startswith("/v1/messages"):
        error = {"type": "error", "error": {"type": cause.value, "message": cause.sentence}}
    else:
        error = {"error": {"message": cause.sentence, "type": cause.value, "code": cause.value}}
    return (
        (cause.status, [json.dumps(error).encode()], 0.0)
        if path.startswith("/v1/") and path != "/v1/models"
        else (200, [b"{}"], 0.0)
    )


@pytest.fixture
def refusing_gateway() -> Iterator[Gateway]:
    yield from serving(refusing)


def send(url: str, body: dict[str, Any] | None = None) -> None:
    data = None if body is None else json.dumps(body).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10).read()
    except urllib.error.HTTPError:
        pass


def test_the_forwarder_names_the_mock_pool_and_records_a_refusal(refusing_gateway: Gateway) -> None:
    marker = "rhi-s2-0123abcd"
    body = {"model": "gpt-5", "stream": True, "messages": [{"role": "user", "content": f"Reply. {marker}"}]}
    with forwarder.Forwarder(refusing_gateway.url, "mock") as through:
        send(f"{through.url}/v1/models")
        send(f"{through.url}/v1/chat/completions", {**body, "messages": [], "stream": False})
        send(f"{through.url}/v1/chat/completions", body)
    # Every request reached the gateway naming the mock pool.
    assert {sent["model"] for sent in refusing_gateway.bodies} == {"mock"}
    models, _, request = through.recorded()
    assert (request.method, request.route, request.streams()) == ("POST", "/v1/chat/completions", True)
    assert marker.encode() in request.body
    assert (request.status, request.cause) == (Cause.REQUEST_REFUSED.status, Cause.REQUEST_REFUSED)
    assert forwarder.refused(request) and not ended_by_client(request)
    assert not forwarder.refused(models)


@pytest.mark.parametrize("client", [Client.OPENAI_SDK, Client.ANTHROPIC_SDK])
def test_an_sdk_reaches_the_gateway_through_its_forwarder(refusing_gateway: Gateway, client: forwarder.Client) -> None:
    # One stream through the SDK, with its own timeout and retries, as a student's program sends it.
    marker = "rhi-s2-0123abcd"
    key, prompt = generate_key(), f"Reply. {marker}"
    with forwarder.Forwarder(refusing_gateway.url, "mock") as through:
        if client is Client.OPENAI_SDK:
            route = "/v1/chat/completions"
            chat = openai.OpenAI(api_key=key, base_url=f"{through.url}/v1").chat.completions
            with pytest.raises(openai.APIStatusError):
                for _ in chat.create(model="gpt-5", messages=[{"role": "user", "content": prompt}], stream=True):
                    pass
        else:
            route = "/v1/messages"
            messages = anthropic.Anthropic(api_key=key, base_url=through.url).messages
            with (
                pytest.raises(anthropic.APIStatusError),
                messages.stream(
                    model="claude-opus", max_tokens=256, messages=[{"role": "user", "content": prompt}]
                ) as stream,
            ):
                for _ in stream.text_stream:
                    pass
    # The SDK's request reached the gateway, naming the mock pool, and its refusal is recorded.
    assert {sent["model"] for sent in refusing_gateway.bodies} == {"mock"}
    (request,) = [
        one
        for one in through.recorded()
        if one.method == "POST" and one.route == route and marker.encode() in one.body and one.streams()
    ]
    assert request.cause == Cause.REQUEST_REFUSED
    assert forwarder.refused(request)


def test_the_forwarder_records_when_a_client_gives_up() -> None:
    events = [b": keep-alive\n\n"] * 8 + [b"event: message_stop\ndata: {}\n\n"]
    for gateway in serving(lambda path: (200, events, 0.05)):
        with forwarder.Forwarder(gateway.url, "mock") as through:
            body = json.dumps({"model": "mock", "stream": True}).encode()
            request = urllib.request.Request(f"{through.url}/v1/messages", data=body)
            with urllib.request.urlopen(request, timeout=10) as answer:
                assert answer.read().endswith(b"message_stop\ndata: {}\n\n")
            answer = urllib.request.urlopen(
                urllib.request.Request(f"{through.url}/v1/messages", data=body), timeout=10
            )
            answer.read1(1)
            answer.close()
            time.sleep(0.8)
            # A stream the gateway ends with an error event did not complete.
            for broken in serving(lambda path: (200, [b": keep-alive\n\n", b"event: error\ndata: {}\n\n"], 0.05)):
                with forwarder.Forwarder(broken.url, "mock") as failing:
                    urllib.request.urlopen(urllib.request.Request(f"{failing.url}/v1/messages", data=body)).read()
                (ended_badly,) = failing.recorded()
                assert not ended_badly.completed
        whole, dropped = through.recorded()
    assert whole.completed and whole.closed_at is None
    assert not dropped.completed and dropped.closed_at is not None
    assert dropped.closed_at - dropped.sent_at < 0.4


def test_the_forwarder_sees_a_whole_responses_stream_end_in_one_read() -> None:
    # The mock's Responses stream, whose last event is longer than any one event name.
    body = {"model": "mock", "input": "Reply with one word.", "stream": True}
    payload = b"".join(frame.data for frame in _responses_events(_turn(body, None, (), 0), body))
    for gateway in serving(lambda path: (200, [payload], 0.01)):
        with forwarder.Forwarder(gateway.url, "mock") as through:
            request = urllib.request.Request(f"{through.url}/v1/responses", data=json.dumps(body).encode())
            with urllib.request.urlopen(request, timeout=10) as answer:
                assert answer.read() == payload
        (whole,) = through.recorded()
    assert whole.completed and whole.closed_at is None



class RawGateway(BaseHTTPRequestHandler):
    """Holds a stream with a keep-alive, then does what `ends` says: the gateway's own error event
    at the end of its hold, nothing at all (the connection closed before any header), or a stream
    the client gives up on. The forwarder's clock is `NOW`, which the gateway moves."""

    ends: bytes | None = None

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.ends is None:
            return  # closed before any header
        NOW[0] = 400.0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": keep-alive\n\n")
        self.wfile.flush()
        if self.ends:
            self.wfile.write(self.ends)
        else:
            time.sleep(1.0)  # a client that gives up closes first

    def log_message(self, format: str, *args: Any) -> None:
        pass


NOW = [0.0]



@pytest.mark.parametrize(
    ("route", "ends"),
    [
        ("/v1/messages", render_stream_event(Cause.WARMING, WireFormat.MESSAGES)),
        ("/v1/responses", render_stream_event(Cause.WARMING, WireFormat.RESPONSES)),
        ("/v1/chat/completions", render_stream_event(Cause.WARMING, WireFormat.CHAT_COMPLETIONS)),
        ("/v1/responses", None),
        ("/v1/responses", b""),
    ],
    ids=["messages-error", "responses-failed", "chat-error", "closed-unanswered", "client-gives-up"],
)
def test_only_a_request_the_client_ended_is_recorded_as_the_client_s_doing(route: str, ends: bytes | None) -> None:
    NOW[0] = 0.0
    handler = type("Handler", (RawGateway,), {"ends": ends})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as gateway:
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            address = f"http://127.0.0.1:{gateway.server_port}"
            with forwarder.Forwarder(address, "mock", clock=lambda: NOW[0]) as through:
                body = json.dumps({"model": "mock", "stream": True, "input": f"Reply. {MARKER}"}).encode()
                request = urllib.request.Request(f"{through.url}{route}", data=body)
                try:
                    with urllib.request.urlopen(request, timeout=10) as answer:
                        answer.read(1 if ends == b"" else -1)
                except (OSError, http.client.HTTPException):
                    pass  # the gateway closed it unanswered
                time.sleep(0.5)
            (held,) = through.recorded()
        finally:
            gateway.shutdown()
    if ends == b"":
        # The client closed it 400 seconds after it was sent, on the forwarder's clock.
        assert ended_by_client(held) and not held.completed
        assert (held.sent_at, held.closed_at) == (0.0, 400.0)
    else:
        assert not ended_by_client(held)
        assert held.cause == (None if ends is None else Cause.WARMING)
        assert forwarder.refused(held) == (ends is not None)


class CutShort(BaseHTTPRequestHandler):
    """Answers 200 with `payload`, having declared `missing` bytes more than it sends, then closes."""

    media, payload, missing = "", b"", 0

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", self.media)
        self.send_header("Content-Length", str(len(self.payload) + self.missing))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass



@pytest.mark.parametrize(
    ("route", "stream", "media", "payload", "missing"),
    [
        ("/v1/responses", True, "text/event-stream", b": keep-alive\n\nevent: response.completed\ndata: {", 0),
        ("/v1/chat/completions", False, "application/json", b'{"choices":[{"message":', 10),
        ("/v1/messages", False, "application/json", b'{"content":[]}', 10),
    ],
    ids=["unfinished-last-event", "short-unreadable-body", "short-readable-body"],
)
def test_an_answer_the_gateway_cut_short_is_not_completed(
    route: str, stream: bool, media: str, payload: bytes, missing: int
) -> None:
    handler = type("Handler", (CutShort,), {"media": media, "payload": payload, "missing": missing})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as gateway:
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            address = f"http://127.0.0.1:{gateway.server_port}"
            with forwarder.Forwarder(address, "mock", clock=lambda: 0.0) as through:
                body = json.dumps({"model": "mock", "stream": stream, "input": f"Reply. {MARKER}"})
                request = urllib.request.Request(f"{through.url}{route}", data=body.encode())
                try:
                    with urllib.request.urlopen(request, timeout=10) as answer:
                        answer.read()
                except (OSError, http.client.HTTPException):
                    pass  # the client saw the answer cut short
                time.sleep(0.2)
            (held,) = through.recorded()
        finally:
            gateway.shutdown()
    # The gateway, not the client, ended it.
    assert not held.completed
    assert not ended_by_client(held)


# The home.


def test_a_client_s_home_is_what_rhi_login_writes(tmp_path: Path) -> None:
    key, endpoint = generate_key(), "http://127.0.0.1:8080"
    profiles = {}
    for client in Client:
        home = tmp_path / client
        forwarder.write_home(home, client, endpoint, "mock", key)
        environment = {
            "HOME": str(home),
            "PATH": f"{home / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "XDG_CONFIG_HOME": str(home / ".config"),
            # The tests' import path, so that `rhi token` imports the CLI of this repository.
            "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry),
            # `rhi token` reads the key file the home holds, not a keychain of the machine.
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        }
        # `rhi token` answers with the canary key.
        token = subprocess.run(["rhi", "token"], env=environment, capture_output=True, text=True, check=True)
        assert token.stdout == f"{key}\n"
        if client is Client.CLAUDE_CODE:
            settings = json.loads((home / ".claude" / "settings.json").read_text())
            assert settings == {"apiKeyHelper": "rhi token", "env": {"ANTHROPIC_BASE_URL": endpoint}}
        elif client in (Client.CODEX_DEFAULT, Client.CODEX_PROFILE):
            profiles[client] = (home / ".codex" / "rhi.config.toml").read_text()
    assert "stream_idle_timeout_ms = 900000" in profiles[Client.CODEX_PROFILE]
    assert forwarder.without_timer(profiles[Client.CODEX_PROFILE]) == profiles[Client.CODEX_DEFAULT]
    assert "stream_idle_timeout_ms" not in profiles[Client.CODEX_DEFAULT]
