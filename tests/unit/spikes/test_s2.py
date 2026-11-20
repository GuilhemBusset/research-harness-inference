"""Spike S2 (`spikes/s2_client_hold.py`): the verdict on recorded outcomes, what the forwarder makes
of a client's exchanges, and the pins of the harnesses (`spikes/harnesses.py`). The gateways here
are local fakes; no number is a measurement.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import harnesses
import s2_client_hold as s2
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_common.spike_keys import SpikeKeys
from rhi_reporter.measure import Verdict, measurement_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

Client, Kind = s2.Client, s2.Kind
RECORD = Path(__file__).resolve().parents[3] / "docs" / "measurements" / "s2.md"
MARKER = "rhi-s2-aaaaaaaa"
VERSIONS = {"claude_code": "2.1.323", "codex": "0.169.0", "openai_sdk": "3.57.0", "anthropic_sdk": "2.11.0"}


def holds(gave_up: set[tuple[s2.Client, s2.Kind]]) -> list[s2.HoldOutcome]:
    return [
        s2.HoldOutcome(client, kind, "1.0.0", (client, kind) not in gave_up, 300.0 if (client, kind) in gave_up else 0)
        for client, kind in s2.PAIRS
    ]


REFUSALS = [s2.RefusalOutcome(client, "1.0.0", retried=False, shown=True) for client in Client]


def test_only_codex_with_its_default_timer_aborting_passes() -> None:
    numbers = s2.numbers_of(holds({(Client.CODEX_DEFAULT, Kind.STREAM)}), REFUSALS)
    assert set(numbers) == {name.value for name in s2.Number}
    assert numbers[s2.seconds_before_giving_up(Client.CODEX_DEFAULT, Kind.STREAM)] == 300.0
    assert s2.SPIKE.verdict(numbers) is Verdict.PASS
    assert s2.SPIKE.verdict(s2.numbers_of(holds(set()), REFUSALS)) is Verdict.PASS
    names = [name.value for name in s2.Number]
    measurement_event("S2", "s2-client-hold", Verdict.PASS, numbers, names=names, commit="a" * 40, bus="bus")


@pytest.mark.parametrize("pair", [pair for pair in s2.PAIRS if pair[0] is not Client.CODEX_DEFAULT])
def test_a_client_aborting_with_the_profile_fails(pair: tuple[s2.Client, s2.Kind]) -> None:
    assert s2.SPIKE.verdict(s2.numbers_of(holds({pair}), REFUSALS)) is Verdict.FAIL
    assert s2.SPIKE.verdict(s2.numbers_of(holds({pair, (Client.CODEX_DEFAULT, Kind.STREAM)}), REFUSALS)) is Verdict.FAIL


def test_the_record_is_pending_with_a_line_per_row_and_one_per_client_for_the_402() -> None:
    text = RECORD.read_text(encoding="utf-8")
    assert re.search(r'^verdict = "pending"$', text, flags=re.MULTILINE)
    assert "[[runs]]" not in text
    lines = text.splitlines()
    for name in s2.Number:
        assert any(f"`{name}`" in line for line in lines), name
    for client, kind in s2.PAIRS:
        (line,) = [line for line in lines if f"`{s2.completed(client, kind)}`" in line]
        assert f"| {kind} |" in line
        assert VERSIONS[s2.VERSION_OF[client]] in line
    refusal_lines = [line for line in lines if "_402_retried`" in line]
    assert sorted(line.split("`")[1] for line in refusal_lines) == sorted(Client)
    for line in refusal_lines:
        client = Client(line.split("`")[1])
        assert f"`{s2.shown_402(client)}`" in line
        assert VERSIONS[s2.VERSION_OF[client]] in line


def test_the_harnesses_are_pinned(tmp_path: Path) -> None:
    for harness in harnesses.HARNESSES:
        assert harnesses.is_exact(harness.version), harness
    assert {harness.package for harness in harnesses.HARNESSES} == {"@anthropic-ai/claude-code", "@openai/codex"}
    for written in ("^2.1.323", "~2.1.323", ">=2.1.0", "2.1.x", "2.x", "*", "latest", "next", "2.1", "v2.1.323", ""):
        assert not harnesses.is_exact(written), written
        with pytest.raises(ValueError, match="exact version"):
            harnesses.Harness("claude_code", "@anthropic-ai/claude-code", written, "claude")
    assert harnesses.is_exact("0.170.0-alpha.4")

    # The job installs exactly the pins, and reports each harness's and each SDK's version.
    ran: list[list[str]] = []

    def run(argv: Any) -> str:
        ran.append(list(argv))
        return {"claude": "2.1.323 (Claude Code)\n", "codex": "codex-cli 0.169.0\n"}.get(Path(argv[0]).name, "")

    binaries = harnesses.install(tmp_path, run)
    assert ran == [harnesses.install_command(tmp_path)]
    assert ran[0][:2] == ["npm", "install"]
    assert ran[0][-2:] == ["@anthropic-ai/claude-code@2.1.323", "@openai/codex@0.169.0"]
    assert binaries == {
        "claude_code": tmp_path / "node_modules/.bin/claude",
        "codex": tmp_path / "node_modules/.bin/codex",
    }
    assert harnesses.versions(binaries, run, {"openai": "3.57.0", "anthropic": "2.11.0"}.__getitem__) == VERSIONS
    with pytest.raises(RuntimeError, match="not its pin"):
        harnesses.versions(binaries, lambda argv: "codex-cli 0.170.0", lambda name: "1.0.0")


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
    assert s2.repeats(found, wanted) == 1


def test_a_hold_counts_only_just_under_its_deadline() -> None:
    request = exchange(Client.CLAUDE_CODE, Kind.STREAM, MARKER, sent_at=100.0)
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
    if path.startswith("/v1/messages"):
        error = {"type": "error", "error": {"type": "budget_reached", "message": Cause.BUDGET_REACHED.sentence}}
    else:
        error = {
            "error": {"message": Cause.BUDGET_REACHED.sentence, "type": "budget_reached", "code": "budget_reached"}
        }
    return (
        (402, [json.dumps(error).encode()], 0.0)
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


@pytest.mark.parametrize("retries", [0, 1])
def test_the_forwarder_confirms_the_402_and_counts_its_repeats(refusing_gateway: Gateway, retries: int) -> None:
    marker = "rhi-s2-0123abcd"
    body = {"model": "gpt-5", "stream": True, "messages": [{"role": "user", "content": f"Reply. {marker}"}]}
    with s2.Forwarder(refusing_gateway.url, "mock") as forwarder:
        send(f"{forwarder.url}/v1/models")
        send(f"{forwarder.url}/v1/chat/completions", {**body, "messages": [], "stream": False})
        for _ in range(1 + retries):
            send(f"{forwarder.url}/v1/chat/completions", body)
    # Every request reached the gateway naming the mock pool.
    assert {sent["model"] for sent in refusing_gateway.bodies} == {"mock"}
    attempt = s2.Attempt(1, f"Error: {Cause.BUDGET_REACHED.sentence}", 1.0, forwarder.recorded())
    outcome = s2.refusal_outcome(Client.OPENAI_SDK, "3.57.0", attempt, marker)
    assert (outcome.retried, outcome.shown) == (retries > 0, True)
    assert not s2.refusal_outcome(
        Client.OPENAI_SDK, "3.57.0", s2.Attempt(1, "Error 402", 1.0, attempt.exchanges), marker
    ).shown
    # A client whose request never met the 402 cannot answer the question.
    with pytest.raises(s2.NotExercised):
        s2.refusal_outcome(Client.OPENAI_SDK, "3.57.0", attempt, "rhi-s2-ffffffff")


@pytest.mark.parametrize("client", [Client.OPENAI_SDK, Client.ANTHROPIC_SDK])
def test_an_sdk_script_meets_the_402_through_its_forwarder(
    refusing_gateway: Gateway, client: s2.Client, tmp_path: Path
) -> None:
    marker = "rhi-s2-0123abcd"
    running = s2.Process(
        client, Kind.STREAM, generate_key(), f"Reply. {marker}", folder=tmp_path, gateway=refusing_gateway.url,
        model="mock", binaries={}, clock=time.monotonic,
    )  # fmt: skip
    while not running.done():
        time.sleep(0.1)
    outcome = s2.refusal_outcome(client, "1.0.0", running.result(), marker)
    assert (outcome.retried, outcome.shown) == (False, True)


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
    def __init__(self, pool: FakePool, client: s2.Client, kind: s2.Kind, key: str, prompt: str, spent: str) -> None:
        self.pool, self.client, self.kind = pool, client, kind
        self.marker = prompt.split()[-1]
        self.sent_at = pool.clock[0]
        self.refused = key == spent
        if not self.refused:
            pool.begin()

    def done(self) -> bool:
        return self.refused or self.pool.state() == s2.READY

    def result(self) -> s2.Attempt:
        if self.refused:
            request = exchange(self.client, self.kind, self.marker, status=402, cause="budget_reached")
            return s2.Attempt(1, Cause.BUDGET_REACHED.sentence, 0.0, [request])
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
    keys = SpikeKeys(tuple(generate_key() for _ in Client), generate_key())
    started: list[tuple[s2.Client, s2.Kind, float]] = []

    def start(client: s2.Client, kind: s2.Kind, key: str, prompt: str) -> s2.Started:
        started.append((client, kind, clock[0]))
        return FakeClient(pool, client, kind, key, prompt, keys.spent)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    run = s2.Run(pool, s2.Deadlines(420, 240), start, keys, VERSIONS, clock=lambda: clock[0], sleep=sleep, log=print)
    numbers = run.measure()
    assert s2.SPIKE.verdict(numbers) is Verdict.PASS
    assert numbers[s2.completed(Client.CODEX_DEFAULT, Kind.STREAM)] == 0
    assert numbers[s2.seconds_before_giving_up(Client.CODEX_DEFAULT, Kind.STREAM)] == 300.0
    assert numbers[s2.shown_402(Client.CLAUDE_CODE)] == 1
    # First the 402, one request for each client; then the holds.
    assert [client for client, _, _ in started[: len(Client)]] == list(Client)
    streams = [at for _, kind, at in started[len(Client) :] if kind is Kind.STREAM]
    assert len(streams) == 10  # every stream client twice: the first cold start was too short
    # The calls start 180 seconds after the wake: 400 measured, less 220.
    calls = [at for _, kind, at in started if kind is Kind.CALL]
    assert len(calls) == 2 and calls[0] - pool.woken[0] == 180.0
