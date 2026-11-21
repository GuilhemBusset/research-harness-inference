"""Spike S3's job (`spikes/s3_harness_task.py`), run on stubbed harness processes and a stubbed workspace.

Each harness is a stand-in that does what the harness would through the job's real forwarder: it
sends its requests to a gateway that answers on the loopback, leaves its folder as it chooses, and
prints what the harness prints. The SDK script is the job's own, run in the test's process. The
task's check is the real one, on what each stand-in left.
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from botocore.exceptions import ClientError

import common
import loadgen
import s3_harness_task as s3
from rhi_cli import store
from rhi_common.keys import generate_key

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPO = Path(__file__).resolve().parents[3]
REFERENCE = REPO / "spikes" / "s3_reference" / "model.py"
# The pool's instance as the workspace names it: no line may carry it.
INSTANCE = "the-instance-s-own-name"
BUILD = "fp8"
VERSIONS = {"claude_code": "2.1.323", "codex": "0.169.0", "openai_sdk": "3.57.0", "anthropic_sdk": "2.11.0"}
REFUSED_SIDE_QUERY = {"type": "error", "error": {"type": "invalid_request_error", "message": "refused"}}


class Clock:
    """A wall clock that moves a little at each reading, and as far as each sleep asks."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.now += 0.01
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


# The gateway.


def event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def answer(path: str, body: dict[str, Any]) -> tuple[int, str, bytes]:
    """The gateway's answer: Messages streams, refused when they declare no tool and ended by an
    error event when they ask for one token; Responses streams; Chat Completions with a tool call first."""
    stream = "text/event-stream"
    if path.startswith("/v1/messages"):
        if not body.get("tools"):
            return 400, "application/json", json.dumps(REFUSED_SIDE_QUERY).encode()
        if body.get("max_tokens") == 1:
            return 200, stream, event("error", {"type": "error", "error": {"type": "overloaded_error"}})
        return (
            200,
            stream,
            event("message_start", {"type": "message_start"}) + event("message_stop", {"type": "message_stop"}),
        )
    if path.startswith("/v1/responses"):
        return 200, stream, event("response.completed", {"type": "response.completed", "response": {}})
    used = any(message.get("role") == "tool" for message in body.get("messages", []))
    message: dict[str, Any] = {"role": "assistant", "content": "done"}
    if not used:
        call = {"id": "call-1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
    choice = {"index": 0, "finish_reason": "stop" if used else "tool_calls", "message": message}
    completion = {"id": "c", "object": "chat.completion", "created": 0, "model": body["model"], "choices": [choice]}
    return 200, "application/json", json.dumps(completion).encode()


@pytest.fixture
def gateway() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            status, kind, payload = answer(self.path, body)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# The harnesses' stand-ins.

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(command: s3.Command, path: str, body: dict[str, Any]) -> None:
    """A request as a harness sends it, with the key `rhi token` gives it."""
    key = (Path(command.environment["XDG_CONFIG_HOME"]) / "rhi" / store.KEY_FILE).read_text(encoding="utf-8")
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": key}
    request = urllib.request.Request(command.endpoint + path, json.dumps(body).encode(), headers)
    try:
        _DIRECT.open(request, timeout=10).read()
    except urllib.error.HTTPError:
        pass


def printed(*events: dict[str, Any]) -> list[tuple[float, str]]:
    return [(float(at), json.dumps(one)) for at, one in enumerate(events)]


def colour_of(image: Path) -> str:
    """The colour that fills a PNG the job wrote: its first pixel's."""
    data = image.read_bytes()
    start = data.index(b"IDAT") + 4
    (length,) = struct.unpack(">I", data[start - 8 : start - 4])
    pixel = tuple(zlib.decompress(data[start : start + length])[1:4])
    return next(name for name, rgb in s3.COLOURS.items() if rgb == pixel)


def claude_code(command: s3.Command) -> s3.Ran:
    prompt, folder, tools = command.argv[2], command.folder, [{"name": "Read"}]
    messages = [{"role": "user", "content": prompt}]
    if s3.IMAGE_FILE in prompt:
        colour = colour_of(folder / s3.IMAGE_FILE)
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
        result = {"type": "tool_result", "tool_use_id": "t", "content": [image]}
        send(
            command,
            "/v1/messages?beta=true",
            {"tools": tools, "messages": [*messages, {"role": "user", "content": [result]}]},
        )
        return s3.Ran(0, printed({"type": "result", "num_turns": 2, "result": colour.title()}))
    if s3.filler_name(1) in prompt:
        word = (folder / s3.filler_name(s3.FILLER_FILES)).read_text(encoding="utf-8").split()[-1]
        send(command, "/v1/messages", {"tools": tools, "stream": True, "messages": messages})
        boundary = {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {"trigger": "auto", "pre_tokens": 170_000},
        }
        turn = {"type": "assistant", "parent_tool_use_id": None, "message": {"id": "m", "role": "assistant"}}
        output = printed(turn, boundary, turn, {"type": "result", "is_error": False, "result": word})
        return s3.Ran(0, output)
    # The task: done with the reference fix, a refused side query and a stream the engine ended.
    shutil.copyfile(REFERENCE, folder / "model.py")
    send(command, "/v1/messages?beta=true", {"tools": tools, "stream": True, "messages": messages})
    send(command, "/v1/messages?beta=true", {"max_tokens": 32, "messages": [{"role": "user", "content": "a title"}]})
    send(command, "/v1/messages?beta=true", {"tools": tools, "max_tokens": 1, "stream": True, "messages": messages})
    return s3.Ran(
        0, printed({"type": "system", "subtype": "init"}, {"type": "result", "num_turns": 3, "result": "Done."})
    )


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch) -> s3.Launch:
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)

    def launched(command: s3.Command) -> s3.Ran:
        if command.harness is s3.Harness.CLAUDE_CODE:
            return claude_code(command)
        if command.harness is s3.Harness.CODEX:
            # Codex leaves the task undone, in two turns.
            input_: list[dict[str, Any]] = [{"role": "user", "content": command.argv[-1]}]
            for _ in range(2):
                send(command, "/v1/responses", {"stream": True, "input": input_})
                input_ = [*input_, {"role": "assistant", "content": "a step"}]
            return s3.Ran(0, [(0.0, "not done")])
        # The SDK script, which changes the check it was told to leave.
        (command.folder / s3.CHECK).write_text("raise SystemExit(0)\n", encoding="utf-8")
        for variable in ("OPENAI_API_KEY", "OPENAI_BASE_URL", s3.PROMPT_VARIABLE, s3.MODEL_VARIABLE):
            monkeypatch.setenv(variable, command.environment[variable])
        return s3.Ran(s3.sdk_script(command.environment, command.folder))

    return launched


# The workspace and the replay.


class Workspace:
    """The lifecycle agent's release over any window, and the engine's counters, larger after the replay."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, float]] = []

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        self.reads.append((query, end))
        if query.startswith("timestamp("):
            labels = {"instance": INSTANCE, "instance_type": "g7e.2xlarge", "model_revision": BUILD}
            samples = tuple((at, at) for at in range(int(start), int(end) + 1, 15))
            return [common.Series(labels, samples)]
        value = 1000.0 + len(self.reads)
        return [common.Series({"instance": INSTANCE}, ((end, value),))]


class Ready:
    def state(self) -> str:
        return "ready"

    def wake(self) -> None:
        raise AssertionError("a ready pool is not woken")


def ran_job(tmp_path: Path, gateway: str, launch: s3.Launch) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """The job, run once: its lines as written and as read, its keys, and what the replay was given."""
    clock, written, replayed = Clock(), [], {}
    keys = [generate_key() for _ in range(4)]

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        replayed.update(load=load, keys=list(keys))
        clock.sleep(30)
        record = loadgen.Record(0, 0, True, 200, 0.5, 0.6, 20.0, 1000, 900, 50)
        return [record]

    job = s3.Job(
        pool="coursework",
        engine="vllm",
        endpoint=gateway,
        keys=keys,
        gateway=Ready(),
        workspace=Workspace(),
        lines=s3.Lines(keys, wall=clock, out=written.append),
        work=tmp_path,
        install=lambda folder: ({"claude_code": folder / "claude", "codex": folder / "codex"}, VERSIONS),
        launch=launch,
        replay=replay,
        wall=clock,
        sleep=clock.sleep,
        rng=Random(7),
    )
    assert job.run() == 0
    return [json.loads(line) for line in written], keys, replayed | {"written": written}


def when(text: str) -> datetime:
    assert text.endswith("Z")
    return datetime.fromisoformat(text)


def test_each_harness_s_run_is_recorded_with_its_times_version_build_and_check(
    tmp_path: Path, gateway: str, launch: s3.Launch
) -> None:
    lines, keys, replayed = ran_job(tmp_path, gateway, launch)
    for line in lines:
        when(line["at"])
        assert line["kind"] in set(s3.Line)
    # No line holds a key, nor the instance's own name.
    for written in replayed["written"]:
        assert not any(key in written for key in keys)
        assert INSTANCE not in written

    runs = {line["harness"]: line for line in lines if line["kind"] == "harness_run"}
    assert set(runs) == set(s3.Harness)
    for harness, run in runs.items():
        assert run["version"] == VERSIONS[s3.VERSION_OF[harness]]
        assert run["build"] == BUILD
        assert when(run["started_at"]) < when(run["ended_at"])
    # Claude Code's own count of its turns; the others' requests that carry the run's marker.
    assert {harness: run["turns"] for harness, run in runs.items()} == {"claude_code": 3, "codex": 2, "sdk_script": 2}
    # The check, on what each left: done, not done, and not done whatever the SDK script did to the check.
    assert runs["claude_code"]["check_exit_status"] == 0
    assert runs["codex"]["check_exit_status"] != 0
    assert (runs["sdk_script"]["check_exit_status"], runs["sdk_script"]["changed"]) == (1, [s3.CHECK])


def test_claude_code_s_refusals_image_and_compaction_are_recorded(
    tmp_path: Path, gateway: str, launch: s3.Launch
) -> None:
    lines, keys, replayed = ran_job(tmp_path, gateway, launch)
    refused = [line for line in lines if line["kind"] == "refused_request"]
    # The side query the gateway refused, and the stream the engine ended after its 200.
    assert [(line["status"], line["cause"], line["declares_tools"]) for line in refused] == [
        (400, "invalid_request_error", False),
        (200, "overloaded_error", True),
    ]
    assert all(line["step"] == "task" and when(line["sent_at"]) for line in refused)

    (image,) = [line for line in lines if line["kind"] == "image_step"]
    assert (image["image_in_tool_result"], image["status"], image["accepted"], image["named"]) == (
        True,
        200,
        True,
        True,
    )
    (compaction,) = [line for line in lines if line["kind"] == "compaction"]
    assert (compaction["compacted"], compaction["next_turn_completed"], compaction["answered"]) == (True, True, True)

    # The replay: the task's session as it was answered, as the student that had sent nothing.
    (session,) = replayed["load"].sessions
    assert len(session.exchanges) == 1
    assert replayed["keys"] == [keys[3]]
    assert not any(name.lower() in ("x-api-key", "authorization") for name in session.exchanges[0].request.headers)
    start, end = (line for line in lines if line["kind"] == "engine_counters")
    (replay,) = [line for line in lines if line["kind"] == "replay"]
    assert (start["point"], end["point"]) == ("start", "end")
    assert set(start["instances"]) == set(end["instances"]) == {"instance-1"}
    assert set(end["instances"]["instance-1"]) == {"input_tokens", "cached_input_tokens"}
    # The end is read for the scrape after the replay's end.
    assert when(end["counters_at"]) == when(replay["ended_at"]) + timedelta(seconds=s3.SCRAPE_SECONDS)
    # The start is read for the replay's own start.
    assert when(start["counters_at"]) == when(replay["started_at"])


def test_a_line_that_would_hold_a_key_is_not_written() -> None:
    key, written = generate_key(), []
    lines = s3.Lines([key], out=written.append)
    with pytest.raises(ValueError, match="key"):
        lines.write(s3.Line.STEP_FAILED, step=f"a {key}")
    assert written == []


def exchange(path: str, body: dict[str, Any], at: float, status: int | None = 200, **fields: Any) -> s3.s2.Exchange:
    return s3.s2.Exchange("POST", path, json.dumps(body).encode(), at, status=status, ended_at=at + 0.5, **fields)


def claude_session(
    step: s3.Step, events: Sequence[dict[str, Any]], exchanges: Sequence[s3.s2.Exchange] = ()
) -> s3.Session:
    return s3.Session(s3.Harness.CLAUDE_CODE, step, 0.0, 9.0, s3.Ran(0, printed(*events)), list(exchanges))


def test_a_refusal_s_cause_is_kept_only_when_it_is_a_published_name() -> None:
    names = ["budget_reached", "overloaded_error", "PrivateBackendDiagnostic"]
    refused = [exchange("/v1/messages", {}, 1.0, 503, cause=name) for name in names]
    rows = s3.refusals_of(claude_session(s3.Step.TASK, [], refused))
    assert [(row["status"], row["cause"]) for row in rows] == [
        (503, "budget_reached"),
        (503, "overloaded_error"),
        (503, "other"),
    ]


def test_the_image_is_recorded_by_its_accepted_retry_and_named_only_by_its_colour_s_word() -> None:
    result = {"type": "tool_result", "content": [{"type": "image"}]}
    body = {"tools": [{"name": "Read"}], "messages": [{"role": "user", "content": [result]}]}
    retried = [exchange("/v1/messages", body, 1.0, 429), exchange("/v1/messages", body, 2.0, completed=True)]
    for answer, named in (("I cannot tell what is pictured.", False), ("Red.", True), ("red or blue", False)):
        events = [{"type": "result", "is_error": False, "result": answer}]
        row = s3.image_step_of(claude_session(s3.Step.IMAGE, events, retried), "red")
        assert (row["accepted"], row["status"], row["named"]) == (True, 200, named)


def test_the_compaction_files_hold_more_than_the_client_s_window(tmp_path: Path) -> None:
    s3.write_filler(tmp_path, "rhiabc123", Random(1))
    # A line is at most its words and four tokens more.
    estimate = sum(4 + len(line.split()) for path in tmp_path.glob("*.txt") for line in path.read_text().splitlines())
    assert estimate > s3.CLIENT_WINDOW


def test_a_turn_after_compaction_counts_as_completed_only_when_it_succeeded() -> None:
    boundary = {"type": "system", "subtype": "compact_boundary"}
    turn = {"type": "assistant", "parent_tool_use_id": None, "message": {"id": "m2", "role": "assistant"}}
    failed = [boundary, {**turn, "error": "unknown"}, {"type": "result", "is_error": True}]
    done = [boundary, turn, {"type": "result", "is_error": False, "result": "rhiabc123"}]
    usage = {"modelUsage": {"a-model": {"contextWindow": 1_000_000}}}
    neither = [turn, {"type": "result", "is_error": False, "result": "x", **usage}]
    rows = [
        s3.compaction_of(claude_session(s3.Step.COMPACTION, events), "rhiabc123") for events in (failed, done, neither)
    ]
    assert [(row["compacted"], row["next_turn_completed"]) for row in rows] == [
        (True, False),
        (True, True),
        (False, False),
    ]
    # The session that neither compacted nor was refused ended before its window.
    assert (rows[2]["window_exercised"], rows[2]["client_window"]) == (False, 1_000_000)


def test_only_a_grown_turn_refused_as_too_long_shows_the_window() -> None:
    tools = [{"name": "Read"}]
    short = {"tools": tools, "messages": [{"role": "user", "content": "Read the files."}]}
    grown = {"tools": tools, "messages": [{"role": "user", "content": "Read the files. " * 50}]}
    cases = [
        # A budget refusal on the first, short request: no evidence of the window.
        ([exchange("/v1/messages", short, 1.0, 402, cause="budget_reached")], False),
        # A grown turn refused for a budget or a limit: still none.
        ([exchange("/v1/messages", short, 1.0), exchange("/v1/messages", grown, 2.0, 429, cause="concurrency_limit")], False),
        # A grown turn refused as a body the pool could not take.
        ([exchange("/v1/messages", short, 1.0), exchange("/v1/messages", grown, 2.0, 400, cause="request_refused")], True),
        # A grown turn the gateway answered `engine_error`, as it does for a lost connection: undetermined.
        ([exchange("/v1/messages", short, 1.0), exchange("/v1/messages", grown, 2.0, 502, cause="engine_error")], False),
        ([exchange("/v1/messages", short, 1.0), exchange("/v1/messages", grown, 2.0, 502)], False),
    ]  # fmt: skip
    for exchanges, exercised in cases:
        row = s3.compaction_of(claude_session(s3.Step.COMPACTION, [], exchanges), "rhiabc123")
        assert (row["window_exercised"], row["compacted"]) == (exercised, False)
        assert (row["refused_turns"], row["refused_turn_statuses"]) == (1, [exchanges[-1].status])
        assert row["undetermined_refusals"] == (exchanges[-1].status == 502)


def test_the_recorded_session_keeps_the_token_counts_in_order() -> None:
    paths = ["/v1/messages?beta=true", "/v1/messages/count_tokens", "/v1/messages?beta=true", "/v1/messages"]
    seen = [exchange(path, {"messages": []}, float(at), completed=True) for at, path in enumerate(paths)]
    seen[-1].status = 400
    replayed = s3.recorded_session(seen).exchanges
    assert [one.request.path for one in replayed] == paths[:3]
    assert [one.pause_seconds for one in replayed] == [0.0, 0.5, 0.5]


def test_turns_count_a_retry_once_and_keep_a_stopped_run_s_turns(
    tmp_path: Path, gateway: str, launch: s3.Launch, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = iter([(429, "application/json", b'{"error":{"code":"rate_limit_exceeded"}}')])
    real = answer
    monkeypatch.setattr(sys.modules[__name__], "answer", lambda path, body: next(replies, None) or real(path, body))
    keys = [generate_key() for _ in range(4)]
    job = s3.Job(
        pool="coursework", engine="vllm", endpoint=gateway, keys=keys, gateway=Ready(), workspace=Workspace(),
        lines=s3.Lines(keys, out=lambda _: None), work=tmp_path, launch=launch,
    )  # fmt: skip
    run = job.session(s3.Harness.SDK_SCRIPT, s3.Step.TASK, tmp_path, "retry-marker")
    assert [one.status for one in run.exchanges] == [429, 200, 200]
    assert s3.turns_of(run, "retry-marker") == 2
    # Claude Code stopped after one whole turn, with no result.
    turn = {"type": "assistant", "parent_tool_use_id": None, "message": {"id": "m1", "content": [{"type": "tool_use"}]}}
    stopped = s3.Session(s3.Harness.CLAUDE_CODE, s3.Step.TASK, 0, 1, s3.Ran(None, printed(turn, turn), True), [])
    assert s3.turns_of(stopped, "task-marker") == 1


def test_a_workspace_failure_keeps_the_run_s_observations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            raise OSError

    clock, written, keys = Clock(), [], [generate_key() for _ in range(4)]
    job = s3.Job(
        pool="coursework", engine="vllm", endpoint="https://example.com", keys=keys, gateway=Ready(),
        workspace=Down(), lines=s3.Lines(keys, wall=clock, out=written.append), work=tmp_path,
        check=lambda _: 1, wall=clock, sleep=clock.sleep,
    )  # fmt: skip
    refused = exchange("/v1/messages", {}, 1.0, 400)
    monkeypatch.setattr(
        job, "session", lambda harness, step, *_: s3.Session(harness, step, clock(), clock(), s3.Ran(1), [refused])
    )
    assert job._step("task", lambda: job.task(s3.Harness.CLAUDE_CODE)) is not None
    kinds = {(line := json.loads(one))["kind"]: line for one in written}
    assert (kinds["refused_request"]["status"], kinds["harness_run"]["check_exit_status"]) == (400, 1)
    assert (kinds["harness_run"]["build"], kinds["step_failed"]["error"]) == (None, "OSError")


def test_the_replay_s_counters_exclude_traffic_scraped_before_it(tmp_path: Path) -> None:
    clock, charges, written, keys = Clock(), [], [], [generate_key() for _ in range(4)]
    first = clock()

    class Scraped:
        """Counters as the workspace holds them: the sum of what was charged before each scrape."""

        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            scrape = end - end % s3.SCRAPE_SECONDS
            value = sum(charge[2 if "cached" in query else 1] for charge in charges if charge[0] <= scrape)
            return [common.Series({"instance": INSTANCE}, ((end, value),))]

    def sleep(seconds: float) -> None:
        # Other traffic, ten tokens a second, until the replay starts.
        if not any(charge[1] == 100 for charge in charges):
            charges.extend((clock.now + at, 10, 0) for at in range(1, int(seconds) + 1))
        clock.sleep(seconds)

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        charges.append((clock(), 100, 90))
        return [loadgen.Record(0, 0, True, 200, None, None, None, 100, 90, 1)]

    job = s3.Job(
        pool="coursework", engine="vllm", endpoint="https://example.com", keys=keys, gateway=Ready(),
        workspace=Scraped(), lines=s3.Lines(keys, wall=clock, out=written.append), work=tmp_path,
        replay=replay, wall=clock, sleep=sleep,
    )  # fmt: skip
    charges.append((first, 900, 0))  # the compaction, just before
    job.replayed(claude_session(s3.Step.TASK, [], [exchange("/v1/messages", {}, 1.0, completed=True)]))
    start, end = (json.loads(one)["instances"]["instance-1"] for one in written if '"engine_counters"' in one)
    assert start["input_tokens"] >= 900
    assert (end["input_tokens"] - start["input_tokens"], end["cached_input_tokens"] - start["cached_input_tokens"]) == (
        100,
        90,
    )


def test_a_replay_that_fails_part_way_keeps_its_counters(tmp_path: Path) -> None:
    clock, written, keys = Clock(), [], [generate_key() for _ in range(4)]
    sent: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        if len(sent) == 2:
            raise httpx.ReadError("stream closed", request=request)
        return httpx.Response(200, json={"usage": {"input_tokens": 50, "output_tokens": 1}})

    job = s3.Job(
        pool="coursework", engine="vllm", endpoint="https://example.com", keys=keys, gateway=Ready(),
        workspace=Workspace(), lines=s3.Lines(keys, wall=clock, out=written.append), work=tmp_path,
        replay=partial(loadgen.replay, transport=httpx.MockTransport(respond)), wall=clock, sleep=clock.sleep,
    )  # fmt: skip
    turns = [exchange("/v1/messages", {}, float(at), completed=True) for at in (1, 2)]
    assert job._step("replay", lambda: job.replayed(claude_session(s3.Step.TASK, [], turns))) is None
    rows = [json.loads(one) for one in written]
    assert len(sent) == 2
    assert [row["point"] for row in rows if row["kind"] == "engine_counters"] == ["start", "end"]
    assert all(when(row["counters_at"]) for row in rows if row["kind"] == "engine_counters")
    assert [row["kind"] for row in rows if row["kind"] in ("replay", "step_failed")] == ["replay", "step_failed"]


def test_the_check_runs_without_the_build_s_credential_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/synthetic/locator")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    probe = "import os, sys\nsys.exit(3 if any(name.startswith('AWS_') for name in os.environ) else 0)\n"
    (tmp_path / s3.CHECK).write_text(probe, encoding="utf-8")
    assert s3.run_check(tmp_path) == 0


def test_an_error_that_stops_the_job_is_one_line_without_its_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = ClientError({"Error": {"Code": "TestError", "Message": "private diagnostic sentinel"}}, "GetSecretValue")

    def refuse() -> None:
        raise failure

    pool = type("Pool", (), {"name": "coursework", "engine": "vllm"})()
    monkeypatch.setattr(s3.config, "read_ssm", lambda: type("Deployed", (), {"pools": [pool]})())
    monkeypatch.setattr(s3.common, "canary_keys", refuse)
    written: list[str] = []
    assert s3.main({"RHI_SPIKE_ENDPOINT": "https://example.com"}, out=written.append) == 1
    (line,) = [json.loads(one) for one in written]
    assert (line["kind"], line["error"]) == ("job_failed", "ClientError") and when(line["at"])
    assert "sentinel" not in "".join(written) + capsys.readouterr().err


def test_a_stopped_harness_leaves_no_tool_running(tmp_path: Path) -> None:
    (tmp_path / "tool.py").write_text("import time, pathlib\ntime.sleep(1.5)\npathlib.Path('later').touch()\n")
    harness = "import subprocess, sys, time\nsubprocess.Popen([sys.executable, 'tool.py'])\ntime.sleep(30)\n"
    (tmp_path / "harness.py").write_text(harness)
    command = s3.Command(s3.Harness.CODEX, [sys.executable, "harness.py"], {}, tmp_path, "", 0.8)
    assert s3.launch_process(command).stopped
    time.sleep(1.5)
    assert not (tmp_path / "later").exists()
