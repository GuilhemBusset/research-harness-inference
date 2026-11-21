"""The recorder of a harness's session (`tests/replay/record.py`), put between a client and the local
stack the smoke tests run on (`tests/smoke/conftest.py`): the gateway on a loopback port, the mock
engine as its pool. Every key is drawn while the tests run, and the home directory and the user
name the recorder replaces are invented here.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

import harness
from harness import Capture, Settled

REPO = Path(__file__).resolve().parents[2]


def _module(name: str, path: Path) -> Any:
    """A module by its file: `record` is also the name of the measurement recorder (`tools/measurements/`),
    and the smoke tests' stack is in no folder of the import path."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # Its dataclasses look their module up by name while it is run.
    spec.loader.exec_module(module)
    return module


smoke = _module("smoke_stack_record", REPO / "tests" / "smoke" / "conftest.py")
record = _module("replay_record", Path(harness.__file__).with_name("record.py"))


@pytest.fixture(scope="module")
def stack() -> Generator[tuple[str, str]]:
    """The local stack's address, and the canary's key."""
    with smoke.local_stack(smoke.MOCK_POOL) as served:
        yield served


@contextmanager
def recording(upstream: str, out: Path, **options: Any) -> Generator[tuple[Any, httpx.Client]]:
    """A recorder between a client and `upstream`, replacing an invented home directory and user name unless given."""
    options = {"home": "/nowhere/of-the-test", "user": f"tester{secrets.token_hex(4)}", **options}
    session = record.Session(upstream, out, **options)
    with record.serve(session) as address, httpx.Client(base_url=address, timeout=120, trust_env=False) as client:
        yield session, client


def anthropic(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def messages(**fields: Any) -> dict[str, Any]:
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Say hello."}],
        **fields,
    }


def assert_replays(capture: Capture) -> None:
    """The replay gives the client the recorded bytes, and settles once to the capture's usage."""
    replayed = harness.replay(capture)
    assert (replayed.status, replayed.received) == (capture.status, capture.answer), capture.name
    usage = capture.usage
    assert replayed.settled == (Settled(usage.input_tokens, usage.output_tokens, True, usage.total),), capture.name


def test_a_session_is_saved_in_order_and_replays(tmp_path: Path) -> None:
    # A stack of its own, cold: its first stream is held, with the gateway's keep-alives.
    with smoke.local_stack(smoke.MOCK_POOL) as (address, key), recording(address, tmp_path) as (session, client):
        models = client.get("/v1/models", headers={"authorization": f"Bearer {key}"})
        stream = client.post("/v1/messages?beta=true", headers=anthropic(key), json=messages(stream=True))
        counted = client.post("/v1/messages/count_tokens", headers=anthropic(key), json=messages())
    assert [answer.status_code for answer in (models, stream, counted)] == [200, 200, 200]
    names = ["001.gateway", "002.json", "003.json"]
    assert [path.name for path in session.written] == names
    assert sorted(path.name for path in tmp_path.iterdir()) == names

    # Each is in the capture format, and the scrubber accepts it (`harness.load` reads it with the scrubber).
    listed, streamed, count = (harness.load(tmp_path / name, name) for name in names)
    with pytest.raises(ValueError, match="no forwarded route"):
        _ = listed.route  # Answered by the gateway itself: there is nothing to replay.
    assert listed.answer == models.content
    assert [capture.name for capture in harness.folder(tmp_path)] == ["002", "003"]

    keep_alive = b'event: ping\ndata: {"type": "ping"}\n\n'
    assert stream.content.startswith(keep_alive)
    # What the client received before the engine's stream is keep-alives only, and none is in the capture.
    assert stream.content.endswith(streamed.answer)
    held = stream.content.removesuffix(streamed.answer)
    assert streamed.streamed and held and held == keep_alive * (len(held) // len(keep_alive))
    assert not streamed.answer.startswith(keep_alive)
    assert count.usage == harness.Usage(counted.json()["input_tokens"], 0) and count.usage.input_tokens > 0
    for capture in (streamed, count):
        assert capture.headers["host"] == record.EXAMPLE_HOST
        assert_replays(capture)


def test_a_recording_states_the_profile_of_a_pool(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    with recording(address, tmp_path, pool=smoke.MOCK_POOL, profile="coursework") as (session, client):
        # Claude Code's default effort, under its own model name; then a model the local stack has no pool of,
        # which reaches the mock pool only because the recorder names that pool upstream.
        effort = client.post("/v1/messages", headers=anthropic(key), json=messages(output_config={"effort": "high"}))
        renamed = client.post("/v1/messages", headers=anthropic(key), json=messages(model="coursework"))
    assert (effort.status_code, renamed.status_code) == (200, 200)
    forwarded = []
    for path, model in zip(session.written, ("claude-sonnet-4-5", "coursework"), strict=True):
        capture = harness.load(path)
        assert capture.profile == record.profile_of("coursework")
        assert json.loads(capture.body)["model"] == model
        assert capture.forward is not None and capture.forward["model"] == "coursework"
        forwarded.append(capture.forward)
        assert_replays(capture)
    assert forwarded[0]["output_config"] == {"effort": "xhigh"}  # The profile's mapping of `high`.


def test_a_capture_the_scrubber_refuses_is_not_written(
    stack: tuple[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    address, key = stack
    body = messages(messages=[{"role": "user", "content": "Why does 192.0.2.7 not answer?"}])
    with recording(address, tmp_path) as (session, client):
        answer = client.post("/v1/messages", headers=anthropic(key), json=body)
    assert answer.status_code == 200  # The harness has its answer all the same.
    assert session.written == [] and list(tmp_path.iterdir()) == []
    assert "`address`" in capsys.readouterr().err


def test_the_home_directory_is_replaced(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    user = f"student{secrets.token_hex(4)}"
    home = str(Path("/home") / user)
    notes = f"{home}/project/notes_{user}.md"
    body = messages(
        system=f"The working directory is {home}/project. Its sessions are under -home-{user}-project.",
        messages=[
            {"role": "user", "content": "Read my notes."},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": notes}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": f"{notes}\nbackup_{user}_2026\nkey={key}"}
                ],
            },
        ],
    )
    with recording(address, tmp_path, home=home, user=user) as (session, client):
        assert client.post("/v1/messages", headers=anthropic(key), json=body).status_code == 200

    (path,) = session.written
    written = path.read_text(encoding="utf-8")
    assert user.lower() not in written.lower() and home not in written and key not in written
    sent = json.loads(harness.load(path).body)
    assert sent["system"] == "The working directory is /home/user/project. Its sessions are under -home-user-project."
    assert sent["messages"][2]["content"][0]["content"] == (
        f"/home/user/project/notes_user.md\nbackup_user_2026\nkey={record.KEY_PLACEHOLDER}"
    )
    # An answer is made neutral the same way, each event's JSON string by string.
    neutral = record.Neutral(hosts=[], home=home, user=user)
    event = "data: " + json.dumps({"text": f"{notes} of {user.upper()}"})
    assert neutral.body(f"{event}\n\n") == 'data: {"text":"/home/user/project/notes_user.md of user"}\n\n'
