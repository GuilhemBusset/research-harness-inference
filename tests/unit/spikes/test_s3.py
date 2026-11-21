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
import threading
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

import pytest

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
        output = printed({"type": "assistant"}, boundary, {"type": "assistant"}, {"type": "result", "result": word})
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
            input_ = [{"role": "user", "content": command.argv[-1]}]
            for _ in range(2):
                send(command, "/v1/responses", {"stream": True, "input": input_})
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
    assert when(start["counters_at"]) == when(replay["started_at"])


def test_a_line_that_would_hold_a_key_is_not_written() -> None:
    key, written = generate_key(), []
    lines = s3.Lines([key], out=written.append)
    with pytest.raises(ValueError, match="key"):
        lines.write(s3.Line.STEP_FAILED, step=f"a {key}")
    assert written == []
