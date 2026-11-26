"""What a trial's forwarder, budget and supervisor do (docs/design/agent-session-evaluation.md,
"Course quality and the grader"): the answer reaches the trial as it arrives, on its route and its
pool; the student's key stays with the driver; the limits hold for the whole agent tree, calls that
overlap included; and what is kept holds no text.

The forwarder under test is the real one, over loopback. The gateway is a scripted one of this
file: every stream, prompt and key here is made up.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import select
import socket
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from evaluate import contracts
from evaluate.contracts import (
    FailureReason,
    Grade,
    Harness,
    InvalidReason,
    Limits,
    RequestOutcome,
    Route,
    Traffic,
    TrialOutcome,
)
from evaluate.forwarding import (
    OUTPUT_LIMITS,
    PLACEHOLDER_KEY,
    Budget,
    Evidence,
    Forwarder,
    Supervisor,
    run_clock,
    trial_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

MESSAGES, RESPONSES, CHAT = Route.MESSAGES, Route.RESPONSES, Route.CHAT_COMPLETIONS
PATHS = {MESSAGES: "/v1/messages?beta=true", RESPONSES: "/v1/responses", CHAT: "/v1/chat/completions"}
# The limits of the plan: 12 minutes, 32 model turns, 32,000 generated tokens.
PLAN = Limits(trial_wall_time_ms=720_000, trial_model_turns=32, trial_generated_tokens=32_000, drain_ms=60_000)
POOL = "pool-under-test"
CANARY_KEY = "made-up-canary-key"
DRIVER_SECRET = "made-up-driver-secret"
PROMPT_TEXT = "made-up text of a prompt"
ANSWER_TEXT = "made-up text of an answer"
ACCEPTED = Grade(properties=(True,), within_limits=True)
WAIT = 10.0


# The streams, in the shape of each wire format.


def event(data: object, name: str | None = None) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data)
    return (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()


def stream(route: Route, output_tokens: int | None = 5) -> tuple[bytes, bytes]:
    """A whole answer in two parts, its text and its end; it reports usage unless `output_tokens` is `None`."""
    if route is MESSAGES:
        delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": ANSWER_TEXT}}
        usage = {"type": "message_delta", "delta": {}, "usage": {"input_tokens": 12, "output_tokens": output_tokens}}
        end = event({"type": "message_stop"}, "message_stop")
        return event(delta, "content_block_delta"), (
            b"" if output_tokens is None else event(usage, "message_delta")
        ) + end
    if route is RESPONSES:
        delta = {"type": "response.output_text.delta", "delta": ANSWER_TEXT}
        usage = {} if output_tokens is None else {"usage": {"input_tokens": 12, "output_tokens": output_tokens}}
        end = event({"type": "response.completed", "response": usage}, "response.completed")
        return event(delta, "response.output_text.delta"), end
    usage = {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": output_tokens}}
    end = (b"" if output_tokens is None else event(usage)) + event("[DONE]")
    return event({"choices": [{"index": 0, "delta": {"content": ANSWER_TEXT}}]}), end


def asking(route: Route = MESSAGES, *, tools: bool = True, limit: int | None = None, text: str = PROMPT_TEXT) -> dict:
    """A request's body as a harness sends it: its own model name, and tools unless it is a side query."""
    body: dict[str, Any] = {"model": "claude-made-up", "stream": True}
    body["input" if route is RESPONSES else "messages"] = [{"role": "user", "content": text}]
    if tools:
        body["tools"] = [{"name": "run"}]
    if limit is not None:
        body[OUTPUT_LIMITS[route][0]] = limit
    return body


# The scripted gateway.


class Call:
    """One request the gateway received, and the answer it is writing."""

    def __init__(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        self._handler = handler
        self.path, self.body = handler.path, body
        self.headers = {name.lower(): value for name, value in handler.headers.items()}
        asked = handler.path.split("?")[0]
        # A path that generates nothing is of the Messages format, and carries no output limit.
        self.route = next((route for route, path in PATHS.items() if path.split("?")[0] == asked), MESSAGES)
        self.limit = min((body[name] for name in OUTPUT_LIMITS[self.route] if name in body), default=0)

    def begin(self, status: int = 200) -> None:
        self._handler.send_response(status)
        self._handler.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
        self._handler.send_header("Connection", "close")
        self._handler.end_headers()

    def write(self, chunk: bytes) -> None:
        self._handler.wfile.write(chunk)
        self._handler.wfile.flush()

    def answer(self, output_tokens: int | None = 5, *, hold: threading.Event | None = None) -> None:
        """The whole stream, its end held back until `hold` is set."""
        text, end = stream(self.route, output_tokens)
        self.begin()
        self.write(text)
        if hold is not None:
            assert hold.wait(WAIT)
        self.write(end)

    def whole(self, document: dict[str, Any]) -> bytes:
        """An answer that is not a stream: one JSON document, of a declared length."""
        payload = json.dumps(document).encode()
        self._handler.send_response(200)
        self._handler.send_header("Content-Type", "application/json")
        self._handler.send_header("Content-Length", str(len(payload)))
        self._handler.end_headers()
        self.write(payload)
        return payload

    def closed(self) -> bool:
        """Waits for the other side to close the exchange, and says whether it did."""
        readable, _, _ = select.select([self._handler.connection], [], [], WAIT)
        return bool(readable) and not self._handler.connection.recv(1, socket.MSG_PEEK)


class Gateway:
    """A local server that answers each request as `play` says, and keeps what it received."""

    def __init__(self) -> None:
        self.play: Callable[[Call], None] = Call.answer
        self.calls: list[Call] = []
        self.arrived = threading.Semaphore(0)
        self.saw_close = threading.Event()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.close_connection = True
                call = Call(self, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                gateway.calls.append(call)
                gateway.arrived.release()
                with contextlib.suppress(OSError):
                    gateway.play(call)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stall(self, call: Call) -> None:
        """Begins an answer and never ends it: it waits for the other side to give up."""
        call.begin()
        call.write(stream(call.route)[0])
        if call.closed():
            self.saw_close.set()


@pytest.fixture
def gateway() -> Generator[Gateway]:
    gateway = Gateway()
    threading.Thread(target=gateway.server.serve_forever, args=(0.05,), daemon=True).start()
    yield gateway
    gateway.server.shutdown()
    gateway.server.server_close()


class Clock:
    """The run's clock, which a test can move forward."""

    def __init__(self) -> None:
        self._real, self.skipped = run_clock(), 0

    def __call__(self) -> int:
        return self._real() + self.skipped


@contextlib.contextmanager
def forwarding(
    gateway: Gateway, harness: Harness = Harness.CLAUDE_CODE, *, turns: int = 0, tokens: int = 0, limits: Limits = PLAN
) -> Generator[Forwarder]:
    """A serving forwarder under the plan's limits, of which `turns` and `tokens` are already used."""
    budget = Budget(limits, Clock())
    for _ in range(turns):
        budget.settle(budget.admit(turn=True, wanted=1), 0)
    while tokens:
        reservation = budget.admit(turn=False, wanted=tokens)
        budget.settle(reservation, reservation.tokens)
        tokens -= reservation.tokens
    with Forwarder(gateway.url, pool=POOL, key=CANARY_KEY, harness=harness, budget=budget) as forwarder:
        yield forwarder


# The trial's side.


def send(
    forwarder: Forwarder, body: dict, route: Route = MESSAGES, *, path: str | None = None
) -> http.client.HTTPResponse:
    """Sends a request as a trial does, with the placeholder it was given, and gives the answer once it has begun."""
    address = urllib.parse.urlsplit(forwarder.url)
    connection = http.client.HTTPConnection(address.hostname or "", address.port, timeout=WAIT)
    connection.request("POST", path or PATHS[route], json.dumps(body), {"x-api-key": PLACEHOLDER_KEY})
    return connection.getresponse()


def ask(forwarder: Forwarder, body: dict, route: Route = MESSAGES, *, path: str | None = None) -> tuple[int, bytes]:
    answer = send(forwarder, body, route, path=path)
    with answer:
        return answer.status, answer.read()


# A trial as a process: it writes what it can read of its environment and its home, then asks once.
TRIAL = """
import json, os, signal, sys, urllib.request
from pathlib import Path
if "stubborn" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
home = Path(os.environ["HOME"])
files = {str(path.relative_to(home)): path.read_text() for path in home.rglob("*") if path.is_file()}
Path("seen.json").write_text(json.dumps({"pid": os.getpid(), "environment": dict(os.environ), "files": files}))
headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "content-type": "application/json"}
request = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", sys.argv[1].encode(), headers)
with urllib.request.urlopen(request, timeout=60) as answer:
    answer.read()
"""


def supervised(forwarder: Forwarder, folder: Path, *marks: str) -> tuple[Supervisor, Callable[[], Evidence]]:
    """A supervisor, and the run of the trial process in a fresh home and workspace under `folder`."""
    home, work = folder / "home", folder / "work"
    home.mkdir()
    work.mkdir()
    (folder / "trial.py").write_text(TRIAL, encoding="utf-8")
    command = [sys.executable, str(folder / "trial.py"), json.dumps(asking()), *marks]
    supervisor = Supervisor(forwarder, cancel_s=3.0, terminate_s=0.2)
    return supervisor, lambda: supervisor.run(command, env=trial_environment(forwarder.url, home), cwd=work)


def settled(forwarder: Forwarder) -> bool:
    """Whether nothing is left open: no exchange in flight, no reservation, no attempt without its end."""
    drained, budget, tally = forwarder.drained(WAIT), forwarder.budget, forwarder.evidence().tally
    return drained and not budget.open_reservations and not budget.reserved_tokens and not tally.unfinished


def fails(evidence: Evidence, reason: FailureReason) -> bool:
    """Whether the evidence fails the trial for `reason`, even when its grader accepted the work."""
    record = evidence.trial_record(execution=0, trial=0, student=0, grade=ACCEPTED)
    return not evidence.may_pass and record.outcome is TrialOutcome.FAILED and record.failure is reason


# The answer reaches the trial, and the measurements say what was sent.


@pytest.mark.parametrize("route", list(Route))
def test_a_stream_reaches_the_trial_as_it_arrives_on_its_route_and_its_pool(gateway: Gateway, route: Route) -> None:
    read_first = threading.Event()
    gateway.play = lambda call: call.answer(9, hold=read_first)
    text, end = stream(route, 9)
    with forwarding(gateway, Harness.CODEX) as forwarder:
        answer = send(forwarder, asking(route, limit=9_000 if route is MESSAGES else None), route)
        # The text is read while the gateway still holds the end back: nothing waits for the whole answer.
        first = answer.read1(len(text))
        read_first.set()
        rest = answer.read()
        assert settled(forwarder)
        evidence = forwarder.evidence()
    assert (answer.status, first + rest) == (200, text + end)
    [call] = gateway.calls
    assert (call.path, call.body["model"]) == (PATHS[route], POOL)
    # With nothing else in flight, the output limit a request declared is kept as it is; one that
    # declared none is given all that is left.
    assert call.limit == (9_000 if route is MESSAGES else 32_000)
    [measured] = evidence.requests
    assert (measured.attempt.route, measured.attempt.outcome) == (route, RequestOutcome.COMPLETED)
    assert measured.attempt.first_output_ms is not None
    assert (measured.charged_tokens, evidence.generated_tokens, evidence.model_turns) == (9, 9, 1)


def test_retries_and_side_calls_are_counted_apart_from_turns(gateway: Gateway) -> None:
    answers = iter([lambda call: call.begin(503), Call.answer, lambda call: call.answer(7), Call.answer])
    gateway.play = lambda call: next(answers)(call)
    turn, side_query = asking(), asking(tools=False, text="made-up title request")
    with forwarding(gateway, Harness.CLAUDE_CODE) as forwarder:
        statuses = [ask(forwarder, body)[0] for body in (turn, turn, side_query, turn)]
        assert settled(forwarder)
        evidence = forwarder.evidence()
    assert statuses == [503, 200, 200, 200]
    # The refused turn sent again is one turn and one retry; answered, the same body is a new turn.
    assert (evidence.model_turns, evidence.retries, evidence.side_calls) == (2, 1, 1)
    assert [one.attempt.retry for one in evidence.requests] == [0, 1, 0, 0]
    assert [one.side_call for one in evidence.requests] == [False, False, True, False]
    # A refusal generated nothing; the side call's tokens count against the cap like a turn's.
    assert [one.charged_tokens for one in evidence.requests] == [0, 5, 7, 5]
    assert evidence.generated_tokens == 17
    assert evidence.tally.failures[RequestOutcome.HTTP_REFUSAL] == 1


def test_a_request_without_tools_is_a_turn_for_a_harness_that_sends_no_side_query(gateway: Gateway) -> None:
    with forwarding(gateway, Harness.CODEX, turns=31) as forwarder:
        statuses = [ask(forwarder, asking(RESPONSES, tools=False, text=text), RESPONSES)[0] for text in ("one", "two")]
        evidence = forwarder.evidence()
    assert statuses == [200, 429]
    assert (evidence.model_turns, evidence.side_calls, len(gateway.calls)) == (32, 0, 1)
    assert fails(evidence, FailureReason.TURN_LIMIT)


def test_the_trial_cannot_read_the_drivers_credentials(
    gateway: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHI_CANARY_KEY", CANARY_KEY)
    monkeypatch.setenv("AWS_SESSION_TOKEN", DRIVER_SECRET)
    with forwarding(gateway) as forwarder:
        evidence = supervised(forwarder, tmp_path)[1]()
    assert evidence.may_pass
    assert (evidence.model_turns, evidence.open_reservations) == (1, 0)
    # The trial asked with the placeholder; the gateway was given the student's key, and only it.
    [call] = gateway.calls
    assert call.headers["authorization"] == "Bearer " + CANARY_KEY
    assert "x-api-key" not in call.headers
    seen = json.loads((tmp_path / "work" / "seen.json").read_text())
    assert seen["environment"]["ANTHROPIC_API_KEY"] == PLACEHOLDER_KEY
    assert seen["environment"]["HOME"] == str(tmp_path / "home")
    assert not {"RHI_CANARY_KEY", "AWS_SESSION_TOKEN"} & seen["environment"].keys()
    # Neither its environment, its home nor its workspace holds the key or the driver's secret.
    readable = json.dumps(seen) + "".join(path.read_text() for path in tmp_path.rglob("*") if path.is_file())
    assert CANARY_KEY not in readable
    assert DRIVER_SECRET not in readable


def test_durable_evidence_holds_no_prompt_answer_key_or_address(gateway: Gateway, tmp_path: Path) -> None:
    answers = iter([lambda call: call.begin(503), Call.answer])
    gateway.play = lambda call: next(answers)(call)
    with forwarding(gateway) as forwarder:
        assert ask(forwarder, asking())[0] == 503
        evidence = supervised(forwarder, tmp_path)[1]()
    records = [
        one.request_record(request=index, cell=0, student=0, conversation=0, turn=0, trial=3)
        for index, one in enumerate(evidence.requests)
    ]
    trial = evidence.trial_record(execution=0, trial=3, student=0, grade=ACCEPTED)
    assert (trial.outcome, trial.model_turns, trial.retries) == (TrialOutcome.PASSED, 1, 1)
    assert trial.usage is not None
    assert trial.usage.output_tokens == 5
    assert [(record.traffic, record.outcome) for record in records] == [
        (Traffic.LIVE, RequestOutcome.HTTP_REFUSAL),
        (Traffic.LIVE, RequestOutcome.COMPLETED),
    ]
    # Every record is a valid document of the public format, and reads back as itself.
    written = [contracts.dumps(document) for document in (*records, trial)]
    assert [contracts.parse(line) for line in written] == [*records, trial]
    kept = "\n".join(written) + repr(evidence)
    address = urllib.parse.urlsplit(gateway.url).netloc
    for private in (PROMPT_TEXT, ANSWER_TEXT, CANARY_KEY, PLACEHOLDER_KEY, address, str(tmp_path)):
        assert private not in kept


def test_a_token_count_goes_to_the_assigned_pool_and_an_unknown_path_goes_nowhere(gateway: Gateway) -> None:
    gateway.play = lambda call: call.whole({"input_tokens": 3})
    with forwarding(gateway) as forwarder:
        status, counted = ask(forwarder, asking(), path="/v1/messages/count_tokens")
        elsewhere, _ = ask(forwarder, asking(), path="/v1/made-up")
        evidence = forwarder.evidence()
    assert (status, json.loads(counted), elsewhere) == (200, {"input_tokens": 3}, 404)
    [call] = gateway.calls
    assert (call.path, call.body["model"]) == ("/v1/messages/count_tokens", POOL)
    assert call.headers["authorization"] == "Bearer " + CANARY_KEY
    # A count generates nothing: it is no turn and no attempt.
    assert (evidence.model_turns, evidence.requests, evidence.open_reservations) == (0, (), 0)


def test_an_answer_that_is_not_a_stream_is_measured_whole_with_the_usage_it_reports(gateway: Gateway) -> None:
    message = {"role": "assistant", "content": ANSWER_TEXT}
    usage = {"prompt_tokens": 12, "completion_tokens": 18}
    sent: list[bytes] = []
    gateway.play = lambda call: sent.append(call.whole({"choices": [{"index": 0, "message": message}], "usage": usage}))
    asked = asking(CHAT)
    del asked["stream"]
    with forwarding(gateway, Harness.SDK_TOOL_LOOP) as forwarder:
        status, received = ask(forwarder, asked, CHAT)
        assert settled(forwarder)
        evidence = forwarder.evidence()
    assert (status, [received]) == (200, sent)
    [measured] = evidence.requests
    assert measured.attempt.outcome is RequestOutcome.COMPLETED
    assert (measured.charged_tokens, evidence.generated_tokens, evidence.tally.usage_missing) == (18, 18, 0)


def test_an_answer_whose_end_does_not_reach_the_trial_is_a_failed_attempt_charged_what_it_generated(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    sendall = socket.socket.sendall
    with forwarding(gateway) as forwarder:
        port = urllib.parse.urlsplit(forwarder.url).port

        def lost(sock: socket.socket, data: Any, *flags: int) -> None:
            # The forwarder's write of the stream's last event to the trial fails.
            if sock.getsockname()[1] == port and b"message_stop" in bytes(data):
                raise BrokenPipeError
            return sendall(sock, data, *flags)

        monkeypatch.setattr(socket.socket, "sendall", lost)
        status, received = ask(forwarder, asking())
        assert settled(forwarder)
        evidence = forwarder.evidence()
    assert status == 200
    assert b"message_stop" not in received
    [measured] = evidence.requests
    assert (measured.attempt.outcome, measured.closed_by_client) == (RequestOutcome.TRUNCATED_STREAM, True)
    # The gateway sent the answer whole, with its usage: that is what the trial is charged.
    assert (measured.charged_tokens, evidence.generated_tokens) == (5, 5)


# The limits, for calls that overlap and for answers whose size is not known.


def test_of_two_children_asking_for_the_last_turn_at_once_one_is_refused(gateway: Gateway) -> None:
    release = threading.Event()
    gateway.play = lambda call: call.answer(hold=release)
    with forwarding(gateway, turns=31) as forwarder:
        budget = forwarder.budget
        first = send(forwarder, asking(text="made-up task of one child"))
        assert gateway.arrived.acquire(timeout=WAIT)
        # The first child's answer is still streaming when the second asks: it is refused at the
        # forwarder, while the first holds its reservation.
        status, _ = ask(forwarder, asking(text="made-up task of the other child"))
        assert (status, budget.open_reservations, len(gateway.calls)) == (429, 1, 1)
        release.set()
        with first:
            assert first.read() == b"".join(stream(MESSAGES))
        assert settled(forwarder)
        evidence = forwarder.evidence()
    assert (evidence.model_turns, evidence.refusals, len(gateway.calls)) == (32, 1, 1)
    assert fails(evidence, FailureReason.TURN_LIMIT)


def test_one_failed_turn_is_retried_by_one_request_and_a_second_one_takes_a_turn(gateway: Gateway) -> None:
    release = threading.Event()
    body = asking()
    with forwarding(gateway, turns=31) as forwarder:
        gateway.play = lambda call: call.begin(503)
        assert ask(forwarder, body)[0] == 503
        assert settled(forwarder)
        gateway.play = lambda call: call.answer(hold=release)
        assert gateway.arrived.acquire(timeout=WAIT)
        # Two children send the failed turn again at once: one is its retry, the other a 33rd turn.
        first = send(forwarder, body)
        assert gateway.arrived.acquire(timeout=WAIT)
        refused, _ = ask(forwarder, body)
        release.set()
        with first:
            first.read()
        assert settled(forwarder)
        evidence = forwarder.evidence()
    assert (first.status, refused, len(gateway.calls)) == (200, 429, 2)
    assert (evidence.model_turns, evidence.retries) == (32, 1)
    assert fails(evidence, FailureReason.TURN_LIMIT)


def test_a_request_waits_for_the_tokens_a_call_in_flight_holds_and_keeps_the_limit_it_declared(
    gateway: Gateway,
) -> None:
    release = threading.Event()
    generated = iter([5, 9_000])
    gateway.play = lambda call: call.answer(next(generated), hold=release)
    with forwarding(gateway) as forwarder, ThreadPoolExecutor(2) as pool:
        # The first child declares no output limit, so it holds all that is left while it runs.
        first = pool.submit(ask, forwarder, asking(text="made-up part one"))
        assert gateway.arrived.acquire(timeout=WAIT)
        second = pool.submit(ask, forwarder, asking(limit=9_000, text="made-up part two"))
        # The second is neither sent on nor refused while the first holds every token.
        assert not gateway.arrived.acquire(timeout=0.3)
        assert not second.done()
        release.set()
        assert (first.result()[0], second.result()[0]) == (200, 200)
        assert settled(forwarder)
        evidence = forwarder.evidence()
    # What the first did not generate was given back, and the second got what it asked for.
    assert [call.limit for call in gateway.calls] == [32_000, 9_000]
    assert (evidence.generated_tokens, evidence.refusals, evidence.may_pass) == (9_005, 0, True)


def test_a_request_for_several_choices_holds_the_allowance_of_each_or_is_refused(gateway: Gateway) -> None:
    # Each choice generates up to the limit and no usage is reported: the charge is the allowance.
    gateway.play = lambda call: call.answer(None)
    with forwarding(gateway, Harness.SDK_TOOL_LOOP, tokens=31_800) as forwarder:
        assert ask(forwarder, {**asking(CHAT, limit=150), "n": 2}, CHAT)[0] == 200
        assert settled(forwarder)
        # 200 tokens were left for two choices: each is limited to 100, and both are charged.
        assert (gateway.calls[0].limit, gateway.calls[0].body["n"], forwarder.budget.generated_tokens) == (
            100,
            2,
            32_000,
        )
    gateway.calls.clear()
    with forwarding(gateway, Harness.SDK_TOOL_LOOP, tokens=31_999) as forwarder:
        # One token is left: two choices of one token each do not fit, and are not sent.
        assert ask(forwarder, {**asking(CHAT, limit=1), "n": 2}, CHAT)[0] == 429
        evidence = forwarder.evidence()
    assert (gateway.calls, evidence.generated_tokens, evidence.open_reservations) == ([], 31_999, 0)
    assert fails(evidence, FailureReason.TOKEN_LIMIT)


def test_two_children_in_flight_together_cannot_generate_past_the_token_cap(gateway: Gateway) -> None:
    both = threading.Barrier(2)

    def together(call: Call) -> None:
        both.wait(WAIT)
        # Each generates all the endpoint was allowed to: the limit the forwarder wrote.
        call.answer(call.limit)

    gateway.play = together
    with forwarding(gateway, tokens=31_800) as forwarder:
        budget = forwarder.budget
        children = [asking(limit=150, text=text) for text in ("made-up part one", "made-up part two")]
        with ThreadPoolExecutor(2) as pool:
            assert [status for status, _ in pool.map(lambda body: ask(forwarder, body), children)] == [200, 200]
        assert settled(forwarder)
        # Both were at the gateway at once, and their allowances together were the 200 tokens left.
        assert sorted(call.limit for call in gateway.calls) == [50, 150]
        assert (budget.generated_tokens, budget.failure) == (32_000, None)
        # Nothing is left: the next request is refused without reaching the gateway, and the trial fails.
        assert ask(forwarder, asking(text="made-up next turn"))[0] == 429
        evidence = forwarder.evidence()
    assert (len(gateway.calls), evidence.generated_tokens, evidence.open_reservations) == (2, 32_000, 0)
    assert fails(evidence, FailureReason.TOKEN_LIMIT)


def test_missing_usage_is_recorded_as_missing_and_charged_its_allowance(gateway: Gateway) -> None:
    gateway.play = lambda call: call.answer(None)
    with forwarding(gateway, tokens=31_400) as forwarder:
        assert ask(forwarder, asking(limit=1_000))[0] == 200
        assert settled(forwarder)
        [measured] = forwarder.evidence().requests
        # The stream completed and reported nothing: no count is made up for it, and the trial is
        # charged the most it can have generated, the 600 tokens it was allowed.
        assert (measured.attempt.outcome, measured.attempt.usage) == (RequestOutcome.COMPLETED, None)
        assert (gateway.calls[0].limit, measured.charged_tokens, forwarder.budget.generated_tokens) == (
            600,
            600,
            32_000,
        )
        assert ask(forwarder, asking(text="made-up next turn"))[0] == 429
        evidence = forwarder.evidence()
    assert (evidence.tally.usage_missing, evidence.usage, len(gateway.calls)) == (1, None, 1)
    assert fails(evidence, FailureReason.TOKEN_LIMIT)


def test_a_client_disconnect_closes_the_gateways_side_and_keeps_its_charge(gateway: Gateway) -> None:
    gateway.play = gateway.stall
    with forwarding(gateway, tokens=31_400) as forwarder:
        answer = send(forwarder, asking(limit=1_000))
        assert answer.read1(1)
        answer.close()
        assert gateway.saw_close.wait(WAIT)
        assert settled(forwarder)
        [measured] = forwarder.evidence().requests
        assert measured.closed_by_client
        assert measured.attempt.outcome is RequestOutcome.TRUNCATED_STREAM
        assert (measured.allowance_tokens, measured.charged_tokens) == (600, 600)
        assert ask(forwarder, asking(text="made-up next turn"))[0] == 429
        evidence = forwarder.evidence()
    assert fails(evidence, FailureReason.TOKEN_LIMIT)


def test_an_expired_deadline_stops_the_request_and_the_trial_within_the_bound(gateway: Gateway, tmp_path: Path) -> None:
    gateway.play = gateway.stall
    expired_at: list[float] = []

    def expire(clock: Any) -> None:
        # Once the trial's request is in flight, the run's clock passes the 12 minutes.
        assert gateway.arrived.acquire(timeout=WAIT)
        expired_at.append(time.monotonic())
        clock.skipped = PLAN.trial_wall_time_ms

    with forwarding(gateway) as forwarder:
        threading.Thread(target=expire, args=(forwarder.budget.clock,), daemon=True).start()
        # The trial ignores the request to end, so it has to be killed.
        evidence = supervised(forwarder, tmp_path, "stubborn")[1]()
        took = time.monotonic() - expired_at[0]
        assert gateway.saw_close.wait(WAIT)
        assert settled(forwarder)
        # Past the deadline nothing is admitted.
        assert ask(forwarder, asking(text="made-up late turn"))[0] == 429
    assert took < 3.0
    with pytest.raises(ProcessLookupError):
        os.kill(json.loads((tmp_path / "work" / "seen.json").read_text())["pid"], 0)
    [measured] = evidence.requests
    assert (measured.cancelled, measured.attempt.failed) == (True, True)
    assert (evidence.invalid, evidence.open_reservations, evidence.tally.unfinished) == (None, 0, 0)
    assert len(gateway.calls) == 1
    assert fails(evidence, FailureReason.TIMEOUT)


def test_a_trial_whose_process_ends_past_its_deadline_is_a_timeout(gateway: Gateway, tmp_path: Path) -> None:
    with forwarding(gateway, limits=replace(PLAN, trial_wall_time_ms=80)) as forwarder:
        # The process outlives its 80 ms and ends by itself, long before the supervisor's next look.
        command = [sys.executable, "-c", "import time; time.sleep(0.3)"]
        evidence = Supervisor(forwarder, poll_s=2.0).run(
            command, env=trial_environment(forwarder.url, tmp_path), cwd=tmp_path
        )
    assert fails(evidence, FailureReason.TIMEOUT)


def test_a_deadline_that_passes_while_the_gateway_is_being_reached_settles_the_request(
    gateway: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup, release = socket.getaddrinfo, threading.Event()
    port = urllib.parse.urlsplit(gateway.url).port
    with forwarding(gateway) as forwarder:
        clock: Any = forwarder.budget.clock

        def stuck(host: Any, service: Any, *more: Any, **named: Any) -> Any:
            # The connection to the gateway does not come about, and the 12 minutes pass meanwhile.
            if service == port:
                clock.skipped = PLAN.trial_wall_time_ms
                release.wait(WAIT)
            return lookup(host, service, *more, **named)

        monkeypatch.setattr(socket, "getaddrinfo", stuck)
        try:
            evidence = supervised(forwarder, tmp_path)[1]()
        finally:
            release.set()
    # The request had no connection a cancellation could shut: it is settled all the same.
    [measured] = evidence.requests
    assert (measured.cancelled, measured.charged_tokens) == (True, 0)
    assert (evidence.invalid, evidence.open_reservations, evidence.tally.unfinished) == (None, 0, 0)
    assert gateway.calls == []
    assert fails(evidence, FailureReason.TIMEOUT)


def test_a_trial_that_gives_up_on_a_turn_the_endpoint_refused_is_an_endpoint_failure(
    gateway: Gateway, tmp_path: Path
) -> None:
    gateway.play = lambda call: call.begin(503)
    with forwarding(gateway) as forwarder:
        evidence = supervised(forwarder, tmp_path)[1]()
    assert (evidence.generated_tokens, evidence.open_reservations, evidence.tally.unfinished) == (0, 0, 0)
    assert fails(evidence, FailureReason.ENDPOINT_FAILURE)


def test_an_interrupted_or_unstartable_trial_is_invalid_not_failed(gateway: Gateway, tmp_path: Path) -> None:
    gateway.play = gateway.stall
    with forwarding(gateway) as forwarder:
        supervisor, run = supervised(forwarder, tmp_path)
        threading.Thread(target=lambda: gateway.arrived.acquire(timeout=WAIT) and supervisor.interrupt()).start()
        interrupted = run()
        assert settled(forwarder)
        unstartable = Supervisor(forwarder).run([str(tmp_path / "no-such-command")], env={}, cwd=tmp_path)
    assert (interrupted.invalid, interrupted.open_reservations) == (InvalidReason.INTERRUPTED, 0)
    assert unstartable.invalid is InvalidReason.DRIVER_ERROR
    for evidence in (interrupted, unstartable):
        record = evidence.trial_record(execution=0, trial=0, student=0, grade=ACCEPTED)
        assert (evidence.may_pass, record.outcome, record.grade) == (False, TrialOutcome.INVALID, None)
