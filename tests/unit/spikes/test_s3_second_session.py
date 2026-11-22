"""Spike S3's second session (`spikes/s3_second_session.py`), run on a stubbed Claude Code, and the
files it uses (`spikes/s3_second_session/`).

The stand-in for Claude Code does what each turn would through the job's real forwarder: it sends
its requests to a gateway that answers on the loopback, leaves the files the step asks for, and
prints Claude Code's events. The echo server and the PDF program are the real ones.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

import pytest

import common
import s3_second_session as job
from rhi_cli import store
from rhi_common.keys import generate_key

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BUILD = "fp8"
VERSIONS = {"claude_code": "2.1.323", "codex": "0.169.0", "openai_sdk": "3.57.0", "anthropic_sdk": "2.11.0"}
SESSION = "5f0c9e2a-0d4b-4c51-9a7e-3b2d1c0e9f88"
FOUND = "https://example.com/highs"


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


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Answers a Messages stream, and refuses a body that carries a document block."""
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if b'"document"' in body:
                status, payload = 400, json.dumps({"type": "error", "error": {"type": "invalid_request_error"}})
            else:
                status, payload = 200, 'event: message_stop\ndata: {"type": "message_stop"}\n\n'
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode())

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


class Workspace:
    """The lifecycle agent's release over any window."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        labels = {"instance": "an-instance", "instance_type": "g7e.2xlarge", "model_revision": BUILD}
        return [common.Series(labels, tuple((at, at) for at in range(int(start), int(end) + 1, 15)))]


class Ready:
    def state(self) -> str:
        return "ready"

    def wake(self) -> None:
        raise AssertionError("a ready pool is not woken")


# The stand-in for Claude Code.

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(command: job.s3.Command, body: dict[str, Any]) -> None:
    """A request as Claude Code sends it, with the key `rhi token` gives it."""
    key = (Path(command.environment["XDG_CONFIG_HOME"]) / "rhi" / store.KEY_FILE).read_text(encoding="utf-8")
    headers = {"Content-Type": "application/json", "x-api-key": key}
    request = urllib.request.Request(command.endpoint + "/v1/messages?beta=true", json.dumps(body).encode(), headers)
    try:
        _DIRECT.open(request, timeout=10).read()
    except urllib.error.HTTPError:
        pass


def calls(message: str, *names_and_inputs: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    blocks = [{"type": "tool_use", "id": f"{message}-{n}", "name": name, "input": given} for n, (name, given) in enumerate(names_and_inputs)]  # fmt: skip
    return {"type": "assistant", "parent_tool_use_id": None, "message": {"id": message, "content": blocks}}


def returned(tool_use_id: str, text: str, *, error: bool = False) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}
    return {"type": "user", "message": {"role": "user", "content": [block | {"is_error": error}]}}


def answer(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"type": "result", "is_error": error, "result": text, "session_id": SESSION}


def does_each_step(prompt: str, folder: Path) -> list[dict[str, Any]]:
    """What Claude Code does with each step's prompt when the step works."""
    if job.SKILL_NAME in prompt:
        # It follows the skill the job put in the folder.
        skill = (folder / ".claude" / "skills" / job.SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
        word = re.search(r"the word `(\w+)`", skill)
        assert word is not None
        (folder / job.SKILL_OUTPUT).write_text(f"{word[1]} {prompt.split()[-1]}\n", encoding="utf-8")
        return [calls("m1", ("Skill", {"skill": job.SKILL_NAME})), answer("The field note is written.")]
    if job.ECHO_SERVER_NAME in prompt:
        text = re.findall(r"echo-[0-9a-f]+", prompt)[0]
        return [calls("m1", (job.ECHO_TOOL, {"text": text})), returned("m1-0", text), answer(f'It returned "{text}".')]
    if job.PDF_FILE in prompt:
        word = re.search(rb"code word is (\w+)", (folder / job.PDF_FILE).read_bytes())
        assert word is not None
        return [answer(word[1].decode())]
    if "part 1" in prompt:
        (folder / job.FIGURE_FILE).write_text("<svg/>", encoding="utf-8")
        done = [returned(f"m1-{n}", "Done.") for n in range(2)]
        return [calls("m1", ("Task", {}), ("Task", {})), *done, answer("Two lines.")]
    if "part 2" in prompt:
        found = f'Web search results for query: "HiGHS"\nLinks: [{{"title": "HiGHS", "url": "{FOUND}"}}]'
        return [calls("m1", ("WebSearch", {"query": "HiGHS"})), returned("m1-0", found), answer(f"{FOUND}/. A solver.")]
    return [answer("TASK.md")]


def does_nothing(prompt: str, folder: Path) -> list[dict[str, Any]]:
    """What Claude Code does when no step but the first works: it answers, leaves no file and calls no tool."""
    if job.PDF_FILE in prompt:
        return [answer("API Error: 400", error=True)]
    return [answer("Web search is not available here: see https://example.com/help." if "part 2" in prompt else "No.")]


def stub(
    behaviour: Callable[[str, Path], list[dict[str, Any]]], seen: list[tuple[list[str], bool]], fails: str | None = None
) -> job.s3.Launch:
    def launched(command: job.s3.Command) -> job.s3.Ran:
        if fails is not None and fails in command.argv[2]:
            raise OSError("not launched")
        prompt, argv = command.argv[2], command.argv
        seen.append((argv, (command.folder / ".claude" / "skills" / job.SKILL_NAME).is_dir()))
        tools = [{"name": "Read"}, *([{"name": job.ECHO_TOOL}] if "--mcp-config" in argv else [])]
        content: list[Any] = [{"type": "text", "text": prompt}]
        if job.PDF_FILE in prompt:
            content.append({"type": "document", "source": {"type": "base64", "data": "AA=="}})
        send(command, {"tools": tools, "stream": True, "messages": [{"role": "user", "content": content}]})
        servers = [{"name": job.ECHO_SERVER_NAME, "status": "connected"}] if "--mcp-config" in argv else []
        init = {"type": "system", "subtype": "init", "session_id": SESSION, "mcp_servers": servers}
        events = [init, *behaviour(prompt, command.folder)]
        return job.s3.Ran(0, [(float(at), json.dumps(one)) for at, one in enumerate(events)])

    return launched


def ran_job(
    tmp_path: Path,
    gateway: str,
    behaviour: Callable[[str, Path], list[dict[str, Any]]],
    fails: str | None = None,
    failed: int = 0,
) -> tuple[list[dict[str, Any]], list[str], list[tuple[list[str], bool]]]:
    """The job, run once: its lines as read and as written, and each turn's command with whether the
    skill was in the folder. The turn whose prompt holds `fails` cannot be launched."""
    clock, written, seen, key = Clock(), [], [], generate_key()
    session = job.Job(
        pool="coursework", engine="vllm", endpoint=gateway, key=key, gateway=Ready(), workspace=Workspace(),
        lines=job.s3.Lines([key], wall=clock, out=written.append), work=tmp_path,
        install=lambda folder: ({"claude_code": folder / "claude"}, VERSIONS), launch=stub(behaviour, seen, fails),
        wall=clock, sleep=clock.sleep, rng=Random(3),
    )  # fmt: skip
    assert session.run() == failed
    assert not any(key in line for line in written)
    return [json.loads(line) for line in written], written, seen


def when(text: str) -> datetime:
    assert text.endswith("Z")
    return datetime.fromisoformat(text)


def steps_of(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for line in lines:
        when(line["at"])
        assert line["kind"] in set(job.Line)
    steps = {line["step"]: line for line in lines if line["kind"] == "step"}
    assert list(steps) == list(job.Step)
    for line in steps.values():
        assert when(line["started_at"]) < when(line["ended_at"])
        assert (line["build"], line["version"]) == (BUILD, VERSIONS["claude_code"])
        assert line["requests"] == (0 if line["launch_error"] else 1)
    return steps


def test_each_step_writes_its_times_its_result_its_refusals_and_its_build(tmp_path: Path, gateway: str) -> None:
    lines, _, seen = ran_job(tmp_path, gateway, does_each_step)
    steps = steps_of(lines)
    assert all(line["result_present"] is True for line in steps.values())
    assert steps["skill"]["skill_called"]
    assert {name: steps["echo"][name] for name in ("server_status", "echo_declared", "echo_called")} == {
        "server_status": "connected",
        "echo_declared": True,
        "echo_called": True,
    }
    assert (steps["pdf"]["document_block_sent"], steps["pdf"]["names_word"]) == (True, True)
    assert [steps["agents"][name] for name in ("agents_asked", "agents_started", "agents_in_one_message")] == [2, 2, 2]
    assert (steps["search"]["search_called"], steps["search"]["addresses_returned"]) == (True, 1)
    # The document block the gateway refused, during the PDF's step and no other.
    (refused,) = [line for line in lines if line["kind"] == "refused_request"]
    assert (refused["step"], refused["status"], refused["cause"]) == ("pdf", 400, "invalid_request_error")
    assert when(refused["sent_at"])
    assert [line["refused_statuses"] for line in steps.values()] == [[], [], [], [400], [], []]

    # One session: no skill and no MCP server on the first turn; the skill arrives on the second,
    # which resumes the session; the echo server on the third, and stays.
    assert [("--resume" in argv, "--mcp-config" in argv, skill) for argv, skill in seen] == [
        (False, False, False),
        (True, False, True),
        *[(True, True, True)] * 4,
    ]
    for argv, _ in seen[1:]:
        assert argv[argv.index("--resume") + 1] == SESSION
        assert "--model" not in argv and argv[-2] == "--allowedTools"
    config = json.loads(Path(seen[2][0][seen[2][0].index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert config["mcpServers"][job.ECHO_SERVER_NAME]["args"] == [str(job.ECHO_SERVER)]


def test_a_step_whose_result_is_missing_records_it_as_not_there(tmp_path: Path, gateway: str) -> None:
    lines, _, _ = ran_job(tmp_path, gateway, does_nothing)
    steps = steps_of(lines)
    assert {name: line["result_present"] for name, line in steps.items()} == {
        "open": True,
        "skill": False,
        "echo": False,
        "pdf": False,
        "agents": False,
        "search": False,
    }
    # A reply that says the search could not be made, with an address of its own, is no answer.
    assert (steps["search"]["answered"], steps["search"]["search_called"]) == (True, False)


def test_a_turn_that_cannot_be_launched_keeps_its_step_line_and_the_session_goes_on(
    tmp_path: Path, gateway: str
) -> None:
    lines, _, seen = ran_job(tmp_path, gateway, does_each_step, fails=job.PDF_FILE, failed=1)
    steps = steps_of(lines)
    assert [line["step"] for line in lines if line["kind"] == "step_failed"] == ["pdf"]
    assert (steps["pdf"]["result_present"], steps["pdf"]["launch_error"], steps["pdf"]["exit_status"]) == (
        False,
        "OSError",
        None,
    )
    assert steps["agents"]["result_present"] and steps["search"]["result_present"]
    assert len(seen) == 5


def turn_of(step: job.Step, *events: dict[str, Any], exchanges: list[Any] | None = None) -> job.Turn:
    printed = [(0.0, json.dumps(one)) for one in events]
    return job.Turn(step, 0.0, 1.0, job.s3.Ran(0, printed), exchanges or [])


def test_echo_is_quoted_only_when_the_answer_holds_all_it_returned() -> None:
    def quoted(returns: str, says: str) -> bool:
        asked = calls("m1", (job.ECHO_TOOL, {"text": "echo-a1b2"}))
        return job.echo_of(turn_of(job.Step.ECHO, asked, returned("m1-0", returns), answer(says)), "echo-a1b2", {})[
            "result_present"
        ]

    assert quoted("echo-a1b2", 'It returned "echo-a1b2".')
    assert quoted("reply echo-a1b2", "reply echo-a1b2")
    assert not quoted("reply echo-a1b2", "echo-a1b2")


def test_an_answer_after_the_pdf_needs_the_pdf_sent_or_its_text_returned() -> None:
    word = "rhi0a1b2c"

    def present(*events: dict[str, Any], exchanges: list[Any] | None = None) -> bool:
        return job.pdf_of(turn_of(job.Step.PDF, *events, exchanges=exchanges), word)["result_present"]

    assert not present(answer("The folder is ready."))
    # Read sends the PDF as a document block: the reply after it counts, even one saying it cannot read it.
    sent = {"messages": [{"role": "user", "content": [{"type": "document", "source": {"type": "base64"}}]}]}
    read = calls("m1", ("Read", {"file_path": job.PDF_FILE}))
    exchange = job.s2.Exchange("POST", "/v1/messages", json.dumps(sent).encode(), 0.5)
    assert present(read, answer("It cannot be read here."), exchanges=[exchange])
    # Its text returned by any command, which the PDF's word shows, counts.
    convert = calls("m1", ("Bash", {"command": "pdftotext ./*.pdf -"}))
    assert present(convert, returned("m1-0", f"The code word is {word}."), answer(word))
    # Naming the file, to list it or to write about it, reads none of it.
    listing = calls("m1", ("Bash", {"command": f"ls -l {job.PDF_FILE}"}))
    assert not present(listing, returned("m1-0", f"-rw-r--r-- 1 600 {job.PDF_FILE}"), answer("The file is there."))
    note = ("Write", {"file_path": "summary.txt", "content": f"Could not read {job.PDF_FILE}."})
    assert not present(calls("m1", note), returned("m1-0", "Created summary.txt"), answer("I wrote a note."))


def test_an_agent_the_harness_refused_is_asked_for_but_not_started(tmp_path: Path) -> None:
    refused = turn_of(
        job.Step.AGENTS,
        calls("m1", ("Agent", {"subagent_type": "missing-kind"})),
        returned("m1-0", "No such agent type.", error=True),
    )
    child = {"type": "assistant", "parent_tool_use_id": "m1-0", "message": {"id": "c1", "content": []}}
    running = turn_of(job.Step.AGENTS, calls("m1", ("Agent", {})), child)
    figure = tmp_path / job.FIGURE_FILE
    assert [job.agents_of(refused, figure)[name] for name in ("agents_asked", "agents_started")] == [1, 0]
    assert job.agents_of(running, figure)["agents_started"] == 1


def test_only_an_address_the_web_search_returned_is_a_search_answer() -> None:
    def search(*events: dict[str, Any]) -> dict[str, Any]:
        printed = [(0.0, json.dumps(one)) for one in events]
        return job.search_of(job.Turn(job.Step.SEARCH, 0.0, 1.0, job.s3.Ran(0, printed), []))

    asked = calls("m1", ("WebSearch", {"query": "HiGHS"}))
    links = f'Links: [{{"url": "{FOUND}"}}]'
    assert search(asked, returned("m1-0", links), answer(f"See {FOUND}."))["result_present"]
    assert not search(asked, returned("m1-0", "unavailable", error=True), answer(f"See {FOUND}."))["result_present"]
    assert not search(asked, returned("m1-0", links), answer("https://example.com/other"))["result_present"]
    # A host in any case is the same host; a path in another case may be another page.
    assert search(asked, returned("m1-0", links), answer(FOUND.replace("example.com", "Example.COM")))["result_present"]
    assert not search(asked, returned("m1-0", links), answer(FOUND.replace("highs", "HiGHS")))["result_present"]
    # An address urlsplit cannot read beside the cited page leaves the page cited.
    assert search(asked, returned("m1-0", links), answer(f"{FOUND}, or http://[::1]/demo."))["result_present"]


def test_the_echo_server_answers_a_call_with_the_text_it_was_given() -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "a b"}}},
    ]
    ran = subprocess.run(
        [sys.executable, str(job.ECHO_SERVER)],
        input="".join(json.dumps(one) + "\n" for one in messages),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    started, called = (json.loads(line) for line in ran.stdout.splitlines())
    assert (started["id"], started["result"]["protocolVersion"]) == (1, "2025-06-18")
    assert called == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": [{"type": "text", "text": "a b"}], "isError": False},
    }


def test_the_pdf_program_writes_a_pdf_that_gives_its_word(tmp_path: Path) -> None:
    path = tmp_path / job.PDF_FILE
    job.write_pdf(path, "rhi0a1b2c")
    data = path.read_bytes()
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert b"(The code word is rhi0a1b2c.)" in data
    # Each object's offset in the table is where the object is.
    offsets = re.findall(rb"(\d{10}) 00000 n", data)
    assert [data[int(at) :].split(b" ", 1)[0] for at in offsets] == [
        str(n).encode() for n in range(1, len(offsets) + 1)
    ]
