"""Spike S2 (`spikes/s2_client_hold.py`): the verdict on recorded outcomes, what the forwarder makes
of a client's exchanges, and the pins of the harnesses (`spikes/harnesses.py`). The gateways here
are local fakes; no number is a measurement.
"""

from __future__ import annotations

import dataclasses
import http.client
import importlib.util
import json
import re
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import harnesses
import s2_client_hold as s2
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_common.spike_keys import SpikeKeys
from rhi_gateway.errors import WireFormat, render_stream_event
from rhi_mock_engine.app import _responses_events, _turn
from rhi_reporter.measure import BUS_VARIABLE, Run, Verdict, measurement_event, read_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

Client, Kind = s2.Client, s2.Kind
RECORD = Path(__file__).resolve().parents[3] / "docs" / "measurements" / "s2.md"
MARKER = "rhi-s2-aaaaaaaa"
VERSIONS = {
    "claude_code": "2.1.323",
    "codex": "0.169.0",
    "opencode": "1.19.5",
    "pi": "1.5.2",
    "openai_sdk": "3.57.0",
    "anthropic_sdk": "2.11.0",
}
PACKAGES = {
    "claude_code": ("@anthropic-ai/claude-code", "claude"),
    "codex": ("@openai/codex", "codex"),
    "opencode": ("opencode-ai", "opencode"),
    "pi": ("@earendil-works/pi-coding-agent", "pi"),
}


def holds(gave_up: set[tuple[s2.Client, s2.Kind]]) -> list[s2.HoldOutcome]:
    return [
        s2.HoldOutcome(client, kind, "1.0.0", (client, kind) not in gave_up, 300.0 if (client, kind) in gave_up else 0)
        for client, kind in s2.PAIRS
    ]


def test_only_codex_with_its_default_timer_aborting_passes() -> None:
    numbers = s2.numbers_of(holds({(Client.CODEX_DEFAULT, Kind.STREAM)}))
    assert set(numbers) == {name.value for name in s2.Number}
    assert numbers[s2.seconds_before_giving_up(Client.CODEX_DEFAULT, Kind.STREAM)] == 300.0
    assert s2.SPIKE.verdict(numbers) is Verdict.PASS
    assert s2.SPIKE.verdict(s2.numbers_of(holds(set()))) is Verdict.PASS
    names = [name.value for name in s2.Number]
    measurement_event("S2", "s2-client-hold", Verdict.PASS, numbers, names=names, commit="a" * 40, bus="bus")


@pytest.mark.parametrize("pair", [pair for pair in s2.PAIRS if pair[0] is not Client.CODEX_DEFAULT])
def test_a_client_aborting_with_the_profile_fails(pair: tuple[s2.Client, s2.Kind]) -> None:
    assert s2.SPIKE.verdict(s2.numbers_of(holds({pair}))) is Verdict.FAIL
    assert s2.SPIKE.verdict(s2.numbers_of(holds({pair, (Client.CODEX_DEFAULT, Kind.STREAM)}))) is Verdict.FAIL


def assert_a_record_of_s2(text: str) -> None:
    """A record of S2: pending until a run is in, a line per row, and each row of a run labelled
    with the client, its version and the kind of call."""
    data = tomllib.loads(text.split("+++")[1])
    runs, rows = data.get("runs", []), data.get("rows", [])
    if not runs:
        assert data["verdict"] == "pending"
    for run in runs:
        if run["verdict"] != "pending":
            assert sorted(row["name"] for row in rows if row["labels"]["run"] == run["run"]) == sorted(s2.Number)
    for row in rows:
        client = next(client for client in Client if row["name"].startswith(f"{client}_"))
        assert row["labels"]["client"] == client and row["labels"]["version"], row
        assert row["labels"]["call"] == row["name"].removeprefix(f"{client}_").split("_")[0], row
    lines = text.splitlines()
    for name in s2.Number:
        assert any(f"`{name}`" in line for line in lines), name
    for client, kind in s2.PAIRS:
        (line,) = [line for line in lines if f"`{s2.completed(client, kind)}`" in line]
        assert f"| {kind} |" in line
        assert VERSIONS[s2.VERSION_OF[client]] in line


def test_the_record_is_pending_with_a_line_per_row() -> None:
    text = RECORD.read_text(encoding="utf-8")
    assert_a_record_of_s2(text)
    if "[[runs]]" not in text:
        assert re.search(r'^verdict = "pending"$', text, flags=re.MULTILINE)


def test_the_record_still_reads_once_a_run_is_written_into_it() -> None:
    # The run as `tools/measurements/record.py` writes it, then labelled as the record's steps say.
    spec = importlib.util.spec_from_file_location("record", RECORD.parents[2] / "tools" / "measurements" / "record.py")
    assert spec is not None and spec.loader is not None
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    data, prose = record.read(RECORD.read_text(encoding="utf-8"))
    numbers = s2.numbers_of(holds({(Client.CODEX_DEFAULT, Kind.STREAM)}))
    units = {name: "s" if name.endswith("_seconds_before_giving_up") else "count" for name in numbers}
    run = Run("S2", "s2-client-hold", s2.SPIKE.verdict(numbers), numbers)
    record.with_run(data, run, date(2026, 11, 20), "a" * 40, units)
    for row in data["rows"]:
        client = next(client for client in Client if row["name"].startswith(f"{client}_"))
        call = row["name"].removeprefix(f"{client}_").split("_")[0]
        row["labels"] |= {"client": client.value, "version": VERSIONS[s2.VERSION_OF[client]], "call": call}
    data["verdict"] = record.verdict_of([run["verdict"] for run in data["runs"]])
    assert data["verdict"] == "pass"
    assert_a_record_of_s2(record.written(data) + prose)


def test_the_harnesses_are_pinned(tmp_path: Path) -> None:
    assert len(harnesses.HARNESSES) == 4
    for harness in harnesses.HARNESSES:
        assert harnesses.is_exact(harness.version), harness
    assert {harness.name: (harness.package, harness.command) for harness in harnesses.HARNESSES} == PACKAGES
    for name, (package, command) in PACKAGES.items():
        for written in ("^1.5.2", "~1.5.2", ">=1.5.0", "1.5.x", "1.x", "*", "latest", "next", "1.5", "v1.5.2", ""):
            assert not harnesses.is_exact(written), written
            with pytest.raises(ValueError, match="exact version"):
                harnesses.Harness(name, package, written, command)
    assert harnesses.is_exact("0.170.0-alpha.4")

    # The job installs exactly the pins, and reports each harness's and each SDK's version.
    ran: list[list[str]] = []

    def run(argv: Any) -> str:
        ran.append(list(argv))
        printed = {"claude": "2.1.323 (Claude Code)\n", "codex": "codex-cli 0.169.0\n", "opencode": "1.19.5\n"}
        return (printed | {"pi": "1.5.2\n"}).get(Path(argv[0]).name, "")

    binaries = harnesses.install(tmp_path, run)
    assert ran == [harnesses.install_command(tmp_path)]
    assert ran[0][:2] == ["npm", "install"]
    assert ran[0][-5:] == [
        f"node@{harnesses.NODE_VERSION}",
        "@anthropic-ai/claude-code@2.1.323",
        "@openai/codex@0.169.0",
        "opencode-ai@1.19.5",
        "@earendil-works/pi-coding-agent@1.5.2",
    ]
    assert binaries == {
        name: tmp_path / ("bin" if name == "pi" else "node_modules/.bin") / command
        for name, (_, command) in PACKAGES.items()
    }
    assert harnesses.versions(binaries, run, {"openai": "3.57.0", "anthropic": "2.11.0"}.__getitem__) == VERSIONS
    with pytest.raises(RuntimeError, match="not its pin"):
        harnesses.versions(binaries, lambda argv: "codex-cli 0.170.0", lambda name: "1.0.0")


def test_pi_runs_on_the_pinned_node_not_the_machine_s(tmp_path: Path) -> None:
    # Pi starts on no Node older than 22.19, and the job's machine starts on an older one: what runs
    # `pi` runs Pi's script with the Node the job installed.
    installed = tmp_path / "harnesses" / "node_modules" / ".bin"
    installed.mkdir(parents=True)
    node = installed / "node"
    node.write_text('#!/bin/sh\necho "pinned node: $*"\n', encoding="utf-8")
    node.chmod(0o755)
    (installed / "pi").write_text("#!/usr/bin/env node\n", encoding="utf-8")

    binaries = harnesses.install(tmp_path / "harnesses", lambda argv: "")
    printed = harnesses.run_command([str(binaries["pi"]), "--version"])
    assert printed == f"pinned node: {installed / 'pi'} --version\n"
    assert harnesses.is_exact(harnesses.NODE_VERSION)
    assert int(harnesses.NODE_VERSION.split(".")[0]) >= 22


# The exchanges.


def exchange(client: s2.Client, kind: s2.Kind, marker: str, *, sent_at: float = 0.0, **given: Any) -> s2.Exchange:
    body = json.dumps({"model": "mock", "stream": kind is Kind.STREAM, "input": f"Reply. {marker}"}).encode()
    return dataclasses.replace(s2.Exchange("POST", s2.ROUTE_OF[client], body, sent_at), **given)


def test_a_client_s_outcome_is_its_request_s() -> None:
    attempt = s2.Attempt(0, "", 500.0)
    done = exchange(Client.CODEX_PROFILE, Kind.STREAM, MARKER, completed=True, status=200, ended_at=420.0)
    assert s2.hold_outcome(Client.CODEX_PROFILE, Kind.STREAM, "0.169.0", attempt, done).completed
    # A drop at 300 seconds is the give-up, whatever a repeat of the request did after.
    dropped = exchange(Client.CODEX_DEFAULT, Kind.STREAM, MARKER, sent_at=10.0, status=200, closed_at=310.0)
    outcome = s2.hold_outcome(Client.CODEX_DEFAULT, Kind.STREAM, "0.169.0", attempt, dropped)
    assert (outcome.completed, outcome.seconds_before_giving_up) == (False, 300.0)
    # A client that failed after its whole answer did not complete.
    failed = s2.hold_outcome(Client.OPENAI_SDK, Kind.CALL, "3.57.0", s2.Attempt(1, "", 500.0), done)
    assert not failed.completed

    # The request is the first on the client's route with the marker, of the kind measured.
    model_list = s2.Exchange("GET", "/v1/models", b"", 0.0)
    other_kind = exchange(Client.OPENAI_SDK, Kind.CALL, MARKER)
    wanted = exchange(Client.OPENAI_SDK, Kind.STREAM, MARKER)
    unmarked = exchange(Client.OPENAI_SDK, Kind.STREAM, "rhi-s2-bbbbbbbb")
    found = [model_list, other_kind, unmarked, wanted, exchange(Client.OPENAI_SDK, Kind.STREAM, MARKER)]
    assert s2.intended(found, Client.OPENAI_SDK, Kind.STREAM, MARKER) is wanted


def test_a_hold_counts_only_just_under_its_deadline() -> None:
    request = exchange(Client.CLAUDE_CODE, Kind.STREAM, MARKER, sent_at=100.0, status=200, completed=True)
    assert s2.exercised(request, 100.0 + 400, 420)
    assert not s2.exercised(request, 100.0 + 420, 420)  # the gateway ended it at its deadline
    assert not s2.exercised(request, 100.0 + 300, 420)  # a pool ready too soon
    assert not s2.exercised(request, None, 420)
    # Calls start 20 seconds under their deadline before the readiness foreseen from the last cold start.
    assert s2.call_delay(400.0, 240) == 180.0
    assert s2.call_delay(100.0, 240) == 0.0


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
    with s2.Forwarder(refusing_gateway.url, "mock") as forwarder:
        send(f"{forwarder.url}/v1/models")
        send(f"{forwarder.url}/v1/chat/completions", {**body, "messages": [], "stream": False})
        send(f"{forwarder.url}/v1/chat/completions", body)
    # Every request reached the gateway naming the mock pool.
    assert {sent["model"] for sent in refusing_gateway.bodies} == {"mock"}
    request = s2.intended(forwarder.recorded(), Client.OPENAI_SDK, Kind.STREAM, marker)
    assert request is not None
    assert (request.status, request.cause) == (Cause.REQUEST_REFUSED.status, Cause.REQUEST_REFUSED)
    assert s2.refused(request) and not s2.ended_by_client(request)


@pytest.mark.parametrize("client", [Client.OPENAI_SDK, Client.ANTHROPIC_SDK])
def test_an_sdk_script_reaches_the_gateway_through_its_forwarder(
    refusing_gateway: Gateway, client: s2.Client, tmp_path: Path
) -> None:
    marker = "rhi-s2-0123abcd"
    running = s2.Process(
        client, Kind.STREAM, generate_key(), f"Reply. {marker}", folder=tmp_path, gateway=refusing_gateway.url,
        model="mock", binaries={}, clock=time.monotonic,
    )  # fmt: skip
    while not running.done():
        time.sleep(0.1)
    attempt = running.result()
    request = s2.intended(attempt.exchanges, client, Kind.STREAM, marker)
    assert request is not None and request.cause == Cause.REQUEST_REFUSED
    assert attempt.exit_code != 0 and Cause.REQUEST_REFUSED.sentence in attempt.output


def test_the_forwarder_records_when_a_client_gives_up() -> None:
    events = [b": keep-alive\n\n"] * 8 + [b"event: message_stop\ndata: {}\n\n"]
    for gateway in serving(lambda path: (200, events, 0.05)):
        with s2.Forwarder(gateway.url, "mock") as forwarder:
            body = json.dumps({"model": "mock", "stream": True}).encode()
            request = urllib.request.Request(f"{forwarder.url}/v1/messages", data=body)
            with urllib.request.urlopen(request, timeout=10) as answer:
                assert answer.read().endswith(b"message_stop\ndata: {}\n\n")
            answer = urllib.request.urlopen(
                urllib.request.Request(f"{forwarder.url}/v1/messages", data=body), timeout=10
            )
            answer.read1(1)
            answer.close()
            time.sleep(0.8)
            # A stream the gateway ends with an error event did not complete.
            for broken in serving(lambda path: (200, [b": keep-alive\n\n", b"event: error\ndata: {}\n\n"], 0.05)):
                with s2.Forwarder(broken.url, "mock") as failing:
                    urllib.request.urlopen(urllib.request.Request(f"{failing.url}/v1/messages", data=body)).read()
                (ended_badly,) = failing.recorded()
                assert not ended_badly.completed
        whole, dropped = forwarder.recorded()
    assert whole.completed and whole.closed_at is None
    assert not dropped.completed and dropped.closed_at is not None
    assert dropped.closed_at - dropped.sent_at < 0.4


def test_the_forwarder_sees_a_whole_responses_stream_end_in_one_read() -> None:
    # The mock's Responses stream, whose last event is longer than any one event name.
    body = {"model": "mock", "input": "Reply with one word.", "stream": True}
    payload = b"".join(frame.data for frame in _responses_events(_turn(body, None, (), 0), body))
    for gateway in serving(lambda path: (200, [payload], 0.01)):
        with s2.Forwarder(gateway.url, "mock") as forwarder:
            request = urllib.request.Request(f"{forwarder.url}/v1/responses", data=json.dumps(body).encode())
            with urllib.request.urlopen(request, timeout=10) as answer:
                assert answer.read() == payload
        (whole,) = forwarder.recorded()
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
    ("client", "ends"),
    [
        (Client.CLAUDE_CODE, render_stream_event(Cause.WARMING, WireFormat.MESSAGES)),
        (Client.CODEX_PROFILE, render_stream_event(Cause.WARMING, WireFormat.RESPONSES)),
        (Client.OPENAI_SDK, render_stream_event(Cause.WARMING, WireFormat.CHAT_COMPLETIONS)),
        (Client.CODEX_PROFILE, None),
        (Client.CODEX_DEFAULT, b""),
    ],
    ids=["messages-error", "responses-failed", "chat-error", "closed-unanswered", "client-gives-up"],
)
def test_only_a_request_the_client_ended_is_a_hold(client: s2.Client, ends: bytes | None) -> None:
    NOW[0] = 0.0
    handler = type("Handler", (RawGateway,), {"ends": ends})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as gateway:
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            with s2.Forwarder(f"http://127.0.0.1:{gateway.server_port}", "mock", clock=lambda: NOW[0]) as forwarder:
                body = json.dumps({"model": "mock", "stream": True, "input": f"Reply. {MARKER}"}).encode()
                request = urllib.request.Request(f"{forwarder.url}{s2.ROUTE_OF[client]}", data=body)
                try:
                    with urllib.request.urlopen(request, timeout=10) as answer:
                        answer.read(1 if ends == b"" else -1)
                except (OSError, http.client.HTTPException):
                    pass  # the gateway closed it unanswered
                time.sleep(0.5)
            (held,) = forwarder.recorded()
        finally:
            gateway.shutdown()
    # The pool became ready just under the stream's deadline, 7 minutes after the request.
    if ends == b"":
        assert held.closed_at is not None
        assert s2.exercised(held, 410.0, 420)
        assert s2.hold_outcome(client, Kind.STREAM, "1.0.0", s2.Attempt(1, "", 500.0), held).seconds_before_giving_up
    else:
        assert not s2.ended_by_client(held)
        assert not s2.exercised(held, 410.0, 420)
        assert held.cause == (None if ends is None else Cause.WARMING)


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
    ("client", "kind", "media", "payload", "missing"),
    [
        (Client.CODEX_PROFILE, Kind.STREAM, "text/event-stream",
         b": keep-alive\n\nevent: response.completed\ndata: {", 0),
        (Client.OPENAI_SDK, Kind.CALL, "application/json", b'{"choices":[{"message":', 10),
        (Client.ANTHROPIC_SDK, Kind.CALL, "application/json", b'{"content":[]}', 10),
    ],
    ids=["unfinished-last-event", "short-unreadable-body", "short-readable-body"],
)  # fmt: skip
def test_an_answer_the_gateway_cut_short_is_not_a_hold(
    client: s2.Client, kind: s2.Kind, media: str, payload: bytes, missing: int
) -> None:
    handler = type("Handler", (CutShort,), {"media": media, "payload": payload, "missing": missing})
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as gateway:
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            with s2.Forwarder(f"http://127.0.0.1:{gateway.server_port}", "mock", clock=lambda: 0.0) as forwarder:
                body = json.dumps({"model": "mock", "stream": kind is Kind.STREAM, "input": f"Reply. {MARKER}"})
                request = urllib.request.Request(f"{forwarder.url}{s2.ROUTE_OF[client]}", data=body.encode())
                try:
                    with urllib.request.urlopen(request, timeout=10) as answer:
                        answer.read()
                except (OSError, http.client.HTTPException):
                    pass  # the client saw the answer cut short
                time.sleep(0.2)
            (held,) = forwarder.recorded()
        finally:
            gateway.shutdown()
    # The pool became ready just under the deadline after the request: the gateway, not the client, ended it.
    deadline = 420 if kind is Kind.STREAM else 240
    assert not held.completed
    assert not s2.ended_by_client(held)
    assert not s2.exercised(held, deadline - 10.0, deadline)


@pytest.mark.parametrize("given", [{}, {"RHI_RUN_LABEL": "profile"}], ids=["default-label", "owner-label"])
def test_two_builds_of_one_commit_post_apart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, given: dict[str, str]
) -> None:
    spec = importlib.util.spec_from_file_location("record", RECORD.parents[2] / "tools" / "measurements" / "record.py")
    assert spec is not None and spec.loader is not None
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    statuses: list[dict[str, str]] = []

    class Events:
        def put_events(self, *, Entries: list[dict[str, str]]) -> dict[str, int]:
            for entry in Entries:
                for status in read_event({"detail": json.loads(entry["Detail"])}):
                    when = f"2026-11-20T18:00:{len(statuses):02d}Z"
                    statuses.append({"context": status.context, "state": status.state,
                                     "description": status.description, "created_at": when})  # fmt: skip
            return {}

    pool = SimpleNamespace(name="mock", engine="mock", start_delay_seconds=380, hold_seconds=420,
                           hold_seconds_non_streaming=240)  # fmt: skip
    monkeypatch.setattr(s2.config, "read_ssm", lambda: SimpleNamespace(pools=[pool]))
    monkeypatch.setattr(s2.common, "canary_keys", lambda: SpikeKeys(tuple(generate_key() for _ in Client)))
    monkeypatch.setattr(s2.common.boto3, "client", lambda name: Events())
    monkeypatch.setattr(s2.harnesses, "install", lambda folder: {})
    monkeypatch.setattr(s2.harnesses, "versions", lambda binaries: VERSIONS)
    measured = s2.numbers_of(holds({(Client.CODEX_DEFAULT, Kind.STREAM)}))
    environ = {"RHI_SPIKE_ENDPOINT": "https://example.com", BUS_VARIABLE: "bus", s2.common.COMMIT_VARIABLE: "a" * 40}
    environ |= given

    # A measured build, then one that was not measured, of the same commit and environment,
    # the owner's label given to both or to neither.
    monkeypatch.setattr(s2.Run, "measure", lambda run: measured)
    assert s2.main(environ) is Verdict.PASS
    monkeypatch.setattr(s2.Run, "measure", lambda run: (_ for _ in ()).throw(s2.NotExercised("no hold")))
    with pytest.raises(s2.NotExercised):
        s2.main(environ)
    runs = sorted(record.runs_of(statuses, "a" * 40), key=lambda found: found[0].verdict is Verdict.NOT_MEASURED)
    assert [run.verdict for run, _ in runs] == [Verdict.PASS, Verdict.NOT_MEASURED]
    assert runs[0][0].numbers == measured
    assert runs[0][0].label != runs[1][0].label
    # A label the owner gives starts the run's.
    assert all(run.label.startswith(f"{given.get('RHI_RUN_LABEL', 's2-client-hold')}-") for run, _ in runs)


def test_a_client_s_home_is_what_rhi_login_writes(tmp_path: Path) -> None:
    key, endpoint = generate_key(), "http://127.0.0.1:8080"
    binaries = {harness.name: Path(f"/job/{harness.command}") for harness in harnesses.HARNESSES}
    profiles = {}
    for client in Client:
        home = tmp_path / client
        s2.write_home(home, client, endpoint, "mock", key)
        argv, environment = s2.client_command(
            client, KINDS_FIRST[client], home=home, endpoint=endpoint, model="mock", key=key, prompt="Reply.",
            binaries=binaries,
        )  # fmt: skip
        # `rhi token` answers with the canary key.
        token = subprocess.run(["rhi", "token"], env=environment, capture_output=True, text=True, check=True)
        assert token.stdout == f"{key}\n"
        if client is Client.CLAUDE_CODE:
            settings = json.loads((home / ".claude" / "settings.json").read_text())
            assert settings == {"apiKeyHelper": "rhi token", "env": {"ANTHROPIC_BASE_URL": endpoint}}
            assert argv[0] == "/job/claude" and argv[argv.index("--model") + 1] == "mock"
            assert environment["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
        elif client in (Client.CODEX_DEFAULT, Client.CODEX_PROFILE):
            profiles[client] = (home / ".codex" / "rhi.config.toml").read_text()
            assert argv[:4] == ["/job/codex", "exec", "--profile", "rhi"]
            assert environment["CODEX_HOME"] == str(home / ".codex")
        else:
            assert key in environment.values() and endpoint in "".join(environment.values())
            assert argv[-3:] == [s2.SDK_FLAG, client, "stream"]
    assert "stream_idle_timeout_ms = 900000" in profiles[Client.CODEX_PROFILE]
    assert s2.without_timer(profiles[Client.CODEX_PROFILE]) == profiles[Client.CODEX_DEFAULT]
    assert "stream_idle_timeout_ms" not in profiles[Client.CODEX_DEFAULT]


KINDS_FIRST = {client: kinds[0] for client, kinds in s2.KINDS.items()}


# A whole run, on a fake clock.


class FakePool:
    """The gateway's status of a mock pool that starts in the given seconds after each cold start."""

    def __init__(self, clock: list[float], cold_starts: list[float]) -> None:
        self.clock, self.cold_starts, self.ready_at = clock, cold_starts, None
        self.woken: list[float] = []

    def begin(self) -> None:
        if self.ready_at is None:
            self.ready_at = self.clock[0] + self.cold_starts.pop(0)

    def state(self) -> str:
        if self.ready_at is None:
            return s2.COLD
        return s2.READY if self.clock[0] >= self.ready_at else "warming"

    def wake(self) -> None:
        self.woken.append(self.clock[0])
        self.begin()


class FakeClient(s2.Started):
    def __init__(self, pool: FakePool, client: s2.Client, kind: s2.Kind, key: str, prompt: str) -> None:
        self.pool, self.client, self.kind = pool, client, kind
        self.marker = prompt.split()[-1]
        self.sent_at = pool.clock[0]
        pool.begin()

    def done(self) -> bool:
        return self.pool.state() == s2.READY

    def result(self) -> s2.Attempt:
        self.pool.ready_at = None  # the pool goes cold again before the next attempt
        gives_up = self.client is Client.CODEX_DEFAULT
        request = exchange(
            self.client, self.kind, self.marker, sent_at=self.sent_at, status=200, completed=not gives_up,
            closed_at=self.sent_at + 300 if gives_up else None,
        )  # fmt: skip
        return s2.Attempt(0, "answer", self.pool.clock[0], [request])


def test_a_run_repeats_a_hold_that_missed_and_aims_the_calls_at_the_measured_start() -> None:
    clock = [0.0]
    # A first cold start too short to count, then one just under 7 minutes; the calls' cold start after a wake.
    pool = FakePool(clock, [300.0, 400.0, 400.0])
    keys = SpikeKeys(tuple(generate_key() for _ in Client))
    started: list[tuple[s2.Client, s2.Kind, float]] = []

    def start(client: s2.Client, kind: s2.Kind, key: str, prompt: str) -> s2.Started:
        started.append((client, kind, clock[0]))
        return FakeClient(pool, client, kind, key, prompt)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    run = s2.Run(pool, s2.Deadlines(420, 240), start, keys, VERSIONS, clock=lambda: clock[0], sleep=sleep, log=print)
    numbers = run.measure()
    assert s2.SPIKE.verdict(numbers) is Verdict.PASS
    assert numbers[s2.completed(Client.CODEX_DEFAULT, Kind.STREAM)] == 0
    assert numbers[s2.seconds_before_giving_up(Client.CODEX_DEFAULT, Kind.STREAM)] == 300.0
    # The holds alone: no client is started but for a hold.
    streams = [at for _, kind, at in started if kind is Kind.STREAM]
    assert len(streams) == 10  # every stream client twice: the first cold start was too short
    # The calls start 180 seconds after the wake: 400 measured, less 220.
    calls = [at for _, kind, at in started if kind is Kind.CALL]
    assert len(calls) == 2 and calls[0] - pool.woken[0] == 180.0


class Refused(FakeClient):
    """A client whose request the gateway refused at once, 400 `request_refused`."""

    def __init__(self, pool: FakePool, client: s2.Client, kind: s2.Kind, key: str, prompt: str) -> None:
        self.pool, self.client, self.kind = pool, client, kind
        self.marker, self.sent_at = prompt.split()[-1], pool.clock[0]

    def done(self) -> bool:
        return True

    def result(self) -> s2.Attempt:
        request = exchange(
            self.client, self.kind, self.marker, sent_at=self.sent_at, status=Cause.REQUEST_REFUSED.status,
            cause=Cause.REQUEST_REFUSED.value, ended_at=self.sent_at + 1,
        )  # fmt: skip
        return s2.Attempt(1, "", self.sent_at + 1, [request])


def test_a_request_the_gateway_refused_is_not_a_hold() -> None:
    clock = [0.0]
    # Every cold start lasts just under 7 minutes, started by the other clients' requests.
    pool = FakePool(clock, [400.0] * s2.ATTEMPTS)
    keys = SpikeKeys(tuple(generate_key() for _ in Client))

    def start(client: s2.Client, kind: s2.Kind, key: str, prompt: str) -> s2.Started:
        made = Refused if client is Client.CLAUDE_CODE else FakeClient
        return made(pool, client, kind, key, prompt)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    run = s2.Run(pool, s2.Deadlines(420, 240), start, keys, VERSIONS, clock=lambda: clock[0], sleep=sleep, log=print)
    # Codex with its default timer, which gave up 300 seconds into a held stream, was measured on the
    # first cold start; Claude Code, refused each time, never was.
    with pytest.raises(s2.NotExercised, match="for claude_code$"):
        run.holds(Kind.STREAM)
