"""The recorder of a harness's session (`tests/replay/record.py`), put between a client and the local
stack the smoke tests run on (`tests/smoke/conftest.py`): the gateway on a loopback port, the mock
engine as its pool. Every key is drawn while the tests run, and the home directory and the user
name the recorder replaces are invented here.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import secrets
import struct
import sys
import zlib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx
import pytest

import harness
import scrub
from harness import Capture, Settled
from rhi_common.keys import generate_key

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
    assert replayed.settled == (Settled(usage.input_tokens, usage.output_tokens, True, True),), capture.name


def test_a_session_is_saved_in_order_and_replays(tmp_path: Path) -> None:
    # A stack of its own, booked and ready: its first stream is forwarded as any other.
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

    # The gateway held nothing and wrote nothing of its own: the capture's answer is what the client received.
    assert streamed.streamed and streamed.answer == stream.content
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


def test_the_key_is_removed_from_the_request_target_when_encoded(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(4)}"
    encoded = key.replace("_", "%5F")
    with recording(address, tmp_path, user=login) as (session, client):
        target = f"/v1/messages?beta=true&note={encoded}&by=%{ord(login[0]):02x}{login[1:]}"
        assert client.post(target, headers=anthropic(key), json=messages()).status_code == 200
    (path,) = session.written
    saved = unquote(harness.load(path).target)
    assert key not in saved and login not in saved and record.KEY_PLACEHOLDER in saved


def test_a_short_user_name_renames_no_field_of_the_format(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    home = str(Path("/home") / "max")
    system = f"Working directory: {home}/project. Signed in as max."
    headers = {**anthropic(key), "x-session-max": "present"}
    with recording(address, tmp_path, home=home, user="max", profile="coursework") as (session, client):
        assert client.post("/v1/messages", headers=headers, json=messages(system=system)).status_code == 200
    (path,) = session.written
    capture = harness.load(path)
    sent = json.loads(capture.body)
    assert sent["max_tokens"] == 64 and sent["system"] == "Working directory: /home/user/project. Signed in as user."
    assert "x-session-user" in capture.headers and not any("max" in name for name in capture.headers)
    assert_replays(capture)


def test_a_session_never_writes_over_another(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    with recording(address, tmp_path) as (session, client):
        assert client.post("/v1/messages", headers=anthropic(key), json=messages()).status_code == 200
    (path,) = session.written
    kept = path.read_bytes()
    with pytest.raises(FileExistsError):
        record.Session(address, tmp_path)
    assert path.read_bytes() == kept


def test_an_answer_a_replay_would_not_give_back_is_not_replayed(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    body = {"model": smoke.MOCK_POOL, "stream": True, "messages": [{"role": "user", "content": "Hello."}]}
    with recording(address, tmp_path) as (session, client):
        # No usage chunk asked for: the gateway hides it from the client, so the answer reports no usage.
        answer = client.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"}, json=body)
    assert answer.status_code == 200 and answer.content.endswith(b"data: [DONE]\n\n")
    assert [path.name for path in session.written] == ["001.unreplayed"]
    assert harness.load(session.written[0]).answer == answer.content
    assert harness.folder(tmp_path) == []


def test_the_key_is_removed_under_any_spelling_of_the_bearer_scheme(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    encoded = f"%{ord(key[0]):02x}{key[1:]}"
    with recording(address, tmp_path) as (session, client):
        headers = {"authorization": f"bearer {key}", "anthropic-version": "2023-06-01"}
        assert client.post(f"/v1/messages?note={encoded}", headers=headers, json=messages()).status_code == 200
    (path,) = session.written
    assert key not in unquote(harness.load(path).target)


def _png() -> bytes:
    """A valid image of one pixel."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def test_an_image_in_a_tool_result_is_kept_byte_for_byte(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    encoded = base64.b64encode(_png()).decode("ascii")
    login = next(encoded[i : i + 2] for i in range(len(encoded)) if encoded[i : i + 2].isalpha())  # Found in the image.
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}}
    history = [
        {"role": "user", "content": "Read this image."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [image]}]},
    ]
    with recording(address, tmp_path, user=login, profile="coursework") as (session, client):
        assert client.post("/v1/messages", headers=anthropic(key), json=messages(messages=history)).status_code == 200
    (path,) = session.written
    capture = harness.load(path)
    assert json.loads(capture.body)["messages"][2]["content"][0]["content"][0] == image
    assert_replays(capture)


def test_the_user_name_is_removed_from_the_names_of_a_tool_input(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(4)}"
    call = {"type": "tool_use", "id": "t1", "name": "Lookup", "input": {"owners": {login: True}}}
    history = [
        {"role": "user", "content": "Look up these owners."},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Done."}]},
    ]
    with recording(address, tmp_path, user=login) as (session, client):
        assert client.post("/v1/messages", headers=anthropic(key), json=messages(messages=history)).status_code == 200
    (path,) = session.written
    sent = json.loads(harness.load(path).body)
    assert sent["messages"][1]["content"][0]["input"] == {"owners": {"user": True}} and sent["max_tokens"] == 64


def test_two_recorders_cannot_share_a_folder(stack: tuple[str, str], tmp_path: Path) -> None:
    address, _ = stack
    with recording(address, tmp_path), pytest.raises(FileExistsError):
        record.Session(address, tmp_path)
    record.Session(address, tmp_path).close()  # The first has ended, and wrote nothing: the folder is free.


def _read_png(history_image: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Read the attachment."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [history_image]}]},
    ]


def test_a_user_name_that_is_a_word_of_the_format_changes_no_block(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    data = base64.b64encode(_png()).decode("ascii")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    with recording(address, tmp_path, user="image", profile="coursework") as (_, client):
        assert (
            client.post("/v1/messages", headers=anthropic(key), json=messages(messages=_read_png(image))).status_code
            == 200
        )
    (capture,) = harness.folder(tmp_path)
    assert json.loads(capture.body)["messages"][2]["content"][0]["content"][0] == image


def test_an_attachment_whose_metadata_names_the_user_is_not_written(
    stack: tuple[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(4)}"
    text = b"Author\x00" + login.encode("ascii")
    chunk = struct.pack(">I", len(text)) + b"tEXt" + text + struct.pack(">I", zlib.crc32(b"tEXt" + text))
    png = _png()
    data = base64.b64encode(png[:-12] + chunk + png[-12:]).decode("ascii")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    with recording(address, tmp_path, user=login) as (session, client):
        assert (
            client.post("/v1/messages", headers=anthropic(key), json=messages(messages=_read_png(image))).status_code
            == 200
        )
    assert session.written == [] and list(tmp_path.iterdir()) == []
    assert "`user name`" in capsys.readouterr().err


def test_the_user_name_is_removed_from_the_json_a_string_holds(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(4)}"
    arguments = f'{{"owner":"\\u{ord(login[0]):04x}{login[1:]}"}}'  # JSON's escape of its first letter.
    body = {
        "model": smoke.MOCK_POOL,
        "input": [
            {"role": "user", "content": "Find the owner."},
            {"type": "function_call", "call_id": "c1", "name": "Lookup", "arguments": arguments},
            {"type": "function_call_output", "call_id": "c1", "output": "Done."},
        ],
    }
    with recording(address, tmp_path, user=login) as (_, client):
        assert client.post("/v1/responses", headers={"authorization": f"Bearer {key}"}, json=body).status_code == 200
    (capture,) = harness.folder(tmp_path)
    assert json.loads(json.loads(capture.body)["input"][1]["arguments"]) == {"owner": "user"}


def test_the_user_name_is_removed_from_the_names_of_a_tool_input_example(
    stack: tuple[str, str], tmp_path: Path
) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(4)}"
    schema = {"type": "object", "properties": {login: {"type": "string"}}, "required": [login]}
    tool = {"name": "Lookup", "input_schema": schema, "input_examples": [{login: "sample"}]}
    with recording(address, tmp_path, user=login) as (_, client):
        assert client.post("/v1/messages", headers=anthropic(key), json=messages(tools=[tool])).status_code == 200
    (capture,) = harness.folder(tmp_path)
    saved = json.loads(capture.body)["tools"][0]
    assert saved["input_examples"] == [{"user": "sample"}] and list(saved["input_schema"]["properties"]) == ["user"]


def test_a_refused_profile_leaves_the_folder_free(stack: tuple[str, str], tmp_path: Path) -> None:
    address, _ = stack
    with pytest.raises(ValueError, match="request profile"):
        record.Session(address, tmp_path, profile="no-such-pool")
    record.Session(address, tmp_path).close()


def test_a_refused_request_saves_no_key_from_a_name_written_twice(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    encoded = f"%{ord(key[0]):02x}{key[1:]}"
    body = '{"system":' + json.dumps(encoded) + ',"system":"Hello",' + json.dumps(messages())[1:]
    with recording(address, tmp_path) as (session, client):
        assert client.post("/v1/messages", headers=anthropic(key), content=body).status_code == 400
    (path,) = session.written
    assert key not in unquote(harness.load(path).body.decode("utf-8"))


def test_a_user_name_that_is_the_stream_terminator_keeps_it(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    body = {
        "model": smoke.MOCK_POOL,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "Hello."}],
    }
    with recording(address, tmp_path, user="done") as (_, client):
        assert (
            client.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"}, json=body).status_code
            == 200
        )
    (capture,) = harness.folder(tmp_path)
    assert capture.answer.endswith(b"data: [DONE]\n\n")
    assert_replays(capture)


def test_the_user_name_is_removed_from_a_claude_model_name_and_its_data_url(
    stack: tuple[str, str], tmp_path: Path
) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(4)}"
    payload = base64.b64encode(_png()).decode("ascii")
    image = {"type": "input_image", "image_url": f"data:image/png;name={login}.png;base64,{payload}"}
    body = {"model": smoke.MOCK_POOL, "input": [{"role": "user", "content": [image]}]}
    with recording(address, tmp_path, user=login, profile="coursework") as (session, client):
        named = client.post("/v1/messages", headers=anthropic(key), json=messages(model=f"claude-{login}"))
        attached = client.post("/v1/responses", headers={"authorization": f"Bearer {key}"}, json=body)
    assert (named.status_code, attached.status_code) == (200, 200)
    first, second = (harness.load(path) for path in session.written)
    assert json.loads(first.body)["model"] == "claude-user"  # Still a `claude-` name: the same pool.
    assert first.forward is not None and first.forward["model"] == "coursework"
    assert_replays(first)
    saved = json.loads(second.body)["input"][0]["content"][0]["image_url"]
    assert saved == f"data:image/png;name=user.png;base64,{payload}"


def _pdf(author: bytes) -> bytes:
    """A valid document of one page, whose author is written as given."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        b"<< /Author " + author + b" >>",
    ]
    pdf, offsets = b"%PDF-1.4\n", []
    for number, entry in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, entry)
    table = b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return (
        pdf
        + b"xref\n0 5\n0000000000 65535 f \n%strailer\n<< /Root 1 0 R /Info 4 0 R /Size 5 >>\nstartxref\n%d\n%%%%EOF\n"
        % (
            table,
            len(pdf),
        )
    )


@pytest.mark.parametrize("written", ["png-compressed", "pdf", "pdf-hexadecimal", "pdf-escaped"])
def test_a_file_whose_metadata_names_the_user_in_another_form_is_not_written(
    stack: tuple[str, str], tmp_path: Path, written: str
) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(8)}".encode("ascii")
    if written == "png-compressed":
        text = b"Author\x00\x00" + zlib.compress(login)
        chunk = struct.pack(">I", len(text)) + b"zTXt" + text + struct.pack(">I", zlib.crc32(b"zTXt" + text))
        png = _png()
        data = base64.b64encode(png[:-12] + chunk + png[-12:]).decode("ascii")
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
        path, headers, body = "/v1/messages", anthropic(key), messages(messages=_read_png(image))
    else:
        author = {
            "pdf": b"(" + login + b")",
            "pdf-hexadecimal": b"<FEFF" + login.decode("ascii").encode("utf-16-be").hex().encode("ascii") + b">",
            "pdf-escaped": b"(" + b"".join(b"\\%03o" % byte for byte in login) + b")",
        }[written]
        file = {"type": "input_file", "filename": "notes.pdf", "file_data": base64.b64encode(_pdf(author)).decode()}
        path, headers = "/v1/responses", {"authorization": f"Bearer {key}"}
        body = {"model": smoke.MOCK_POOL, "input": [{"role": "user", "content": [file]}]}
    with recording(address, tmp_path, user=login.decode("ascii")) as (session, client):
        assert client.post(path, headers=headers, json=body).status_code == 200
    assert session.written == [] and list(tmp_path.iterdir()) == []


def test_an_inline_file_whose_metadata_names_no_one_is_kept_byte_for_byte(
    stack: tuple[str, str], tmp_path: Path
) -> None:
    address, key = stack
    data = base64.b64encode(_pdf(b"(nobody)")).decode("ascii")
    body = {
        "model": smoke.MOCK_POOL,
        "input": [{"role": "user", "content": [{"type": "input_file", "file_data": data}]}],
    }
    with recording(address, tmp_path) as (_, client):
        assert client.post("/v1/responses", headers={"authorization": f"Bearer {key}"}, json=body).status_code == 200
    (capture,) = harness.folder(tmp_path)
    assert json.loads(capture.body)["input"][0]["content"][0]["file_data"] == data


def test_a_user_name_that_is_a_json_literal_keeps_the_arguments_json(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    arguments = json.dumps({"owner": "true", "enabled": True})
    body = {
        "model": smoke.MOCK_POOL,
        "input": [
            {"role": "user", "content": "Find the owner."},
            {"type": "function_call", "call_id": "c1", "name": "Lookup", "arguments": arguments},
            {"type": "function_call_output", "call_id": "c1", "output": "Done."},
        ],
    }
    with recording(address, tmp_path, user="true", profile="coursework") as (_, client):
        assert client.post("/v1/responses", headers={"authorization": f"Bearer {key}"}, json=body).status_code == 200
    (capture,) = harness.folder(tmp_path)
    saved = json.loads(capture.body)["input"][1]["arguments"]
    assert json.loads(saved) == {"owner": "user", "enabled": True}


def test_a_user_name_in_a_file_format_signature_keeps_the_file(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    payload = base64.b64encode(_pdf(b"(nobody)")).decode("ascii")
    document = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": payload}}
    with recording(address, tmp_path, user="pdf", profile="coursework") as (_, client):
        answer = client.post("/v1/messages", headers=anthropic(key), json=messages(messages=_read_png(document)))
    assert answer.status_code == 200
    (capture,) = harness.folder(tmp_path)
    assert json.loads(capture.body)["messages"][2]["content"][0]["content"][0]["source"]["data"] == payload


def test_an_attachment_whose_metadata_names_the_home_as_a_uri_is_not_written(
    stack: tuple[str, str], tmp_path: Path
) -> None:
    address, key = stack
    home = str(Path("/home") / f"recording {secrets.token_hex(4)}")
    text = b"Source\x00file://" + quote(home).encode("ascii") + b"/notes.png"
    chunk = struct.pack(">I", len(text)) + b"tEXt" + text + struct.pack(">I", zlib.crc32(b"tEXt" + text))
    png = _png()
    data = base64.b64encode(png[:-12] + chunk + png[-12:]).decode("ascii")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    with recording(address, tmp_path, home=home) as (session, client):
        answer = client.post("/v1/messages", headers=anthropic(key), json=messages(messages=_read_png(image)))
    assert answer.status_code == 200
    assert session.written == [] and list(tmp_path.iterdir()) == []


def test_a_pdf_author_in_nested_parentheses_naming_the_user_is_not_written(
    stack: tuple[str, str], tmp_path: Path
) -> None:
    address, key = stack
    login = f"tester{secrets.token_hex(8)}"
    payload = base64.b64encode(_pdf(f"({login} (Recorder))".encode("ascii"))).decode("ascii")
    document = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": payload}}
    with recording(address, tmp_path, user=login, profile="coursework") as (session, client):
        answer = client.post("/v1/messages", headers=anthropic(key), json=messages(messages=_read_png(document)))
    assert answer.status_code == 200
    assert session.written == [] and list(tmp_path.iterdir()) == []


def test_two_argument_names_made_one_by_the_user_name_keep_both_values(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    arguments = json.dumps({"operator": "first-entry", "user": "second-entry"})
    body = {
        "model": smoke.MOCK_POOL,
        "input": [
            {"role": "user", "content": "Find the owner."},
            {"type": "function_call", "call_id": "c1", "name": "Lookup", "arguments": arguments},
            {"type": "function_call_output", "call_id": "c1", "output": "Done."},
        ],
    }
    with recording(address, tmp_path, user="operator", profile="coursework") as (_, client):
        assert client.post("/v1/responses", headers={"authorization": f"Bearer {key}"}, json=body).status_code == 200
    (capture,) = harness.folder(tmp_path)
    saved = json.loads(json.loads(capture.body)["input"][1]["arguments"])
    assert "operator" not in saved and sorted(saved.values()) == ["first-entry", "second-entry"]


def test_schema_properties_made_one_by_the_user_name_keep_the_required_property(
    stack: tuple[str, str], tmp_path: Path
) -> None:
    address, key = stack
    schema = {
        "type": "object",
        "properties": {"operator": {"type": "integer"}, "user": {"type": "string"}},
        "required": ["operator"],
    }
    tool = {"name": "Lookup", "input_schema": schema}
    with recording(address, tmp_path, user="operator", profile="coursework") as (_, client):
        assert client.post("/v1/messages", headers=anthropic(key), json=messages(tools=[tool])).status_code == 200
    (capture,) = harness.folder(tmp_path)
    saved = json.loads(capture.body)["tools"][0]["input_schema"]
    (required,) = saved["required"]
    assert "operator" not in saved["properties"] and saved["properties"]["user"] == {"type": "string"}
    assert saved["properties"][required] == {"type": "integer"}


def test_a_key_a_stream_writes_in_two_events_is_not_saved() -> None:
    key = generate_key()
    original = harness.load(harness.CAPTURES / "mock-messages-stream.json")
    pieces = iter((key[:25], key[25:]))
    lines = []
    for line in original.answer.decode().splitlines(keepends=True):
        event = json.loads(line[6:]) if line.startswith("data: ") else {}
        if event.get("delta", {}).get("type") == "thinking_delta" and (piece := next(pieces, None)) is not None:
            event["delta"]["thinking"] = piece
            line = "data: " + json.dumps(event) + "\n"
        lines.append(line)
    assert next(pieces, None) is None
    headers = [*original.headers.items(), ("x-api-key", key)]
    answer = "".join(lines).encode()
    exchange = record.Exchange("POST", original.target.encode(), headers, original.body, 200, (), answer)
    with pytest.raises(scrub.Refused, match=scrub.KEY):
        asyncio.run(record.capture_of(exchange, record.Neutral(hosts=(), home="", user="", keys=(key,))))


def test_schema_dependencies_follow_the_neutral_property_names(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    schema = {
        "type": "object",
        "properties": {"operator": {"type": "integer"}, "user": {"type": "string"}, "note": {"type": "string"}},
        "dependentRequired": {"operator": ["note"], "note": ["operator"]},
    }
    tool = {"name": "Lookup", "input_schema": schema}
    with recording(address, tmp_path, user="operator", profile="coursework") as (_, client):
        assert client.post("/v1/messages", headers=anthropic(key), json=messages(tools=[tool])).status_code == 200
    (capture,) = harness.folder(tmp_path)
    saved = json.loads(capture.body)["tools"][0]["input_schema"]
    (renamed,) = [name for name, value in saved["properties"].items() if value == {"type": "integer"}]
    assert saved["dependentRequired"] == {renamed: ["note"], "note": [renamed]} and renamed != "operator"


def _restreamed(delta_type: str, field: str, pieces: list[str]) -> bytes:
    """The mock's streamed Messages answer, its deltas of one type writing these pieces, then nothing."""
    original = harness.load(harness.CAPTURES / "mock-messages-stream.json")
    given = iter(pieces)
    lines = []
    for line in original.answer.decode().splitlines(keepends=True):
        event = json.loads(line[6:]) if line.startswith("data: ") else {}
        if event.get("delta", {}).get("type") == delta_type:
            event["delta"][field] = next(given, "")
            line = "data: " + json.dumps(event) + "\n"
        lines.append(line)
    assert next(given, None) is None
    return "".join(lines).encode()


def _capture_of(name: str, answer: bytes, neutral: Any) -> dict[str, Any]:
    original = harness.load(harness.CAPTURES / f"{name}.json")
    exchange = record.Exchange(
        "POST", original.target.encode(), list(original.headers.items()), original.body, 200, (), answer
    )
    return asyncio.run(record.capture_of(exchange, neutral))[1]


def test_a_key_in_the_arguments_of_calls_made_side_by_side_is_not_saved() -> None:
    key = generate_key()
    half = len(key) // 2

    def chunk(calls: list[dict[str, Any]]) -> str:
        choice = {"index": 0, "delta": {"tool_calls": calls}, "finish_reason": None}
        return "data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk", "choices": [choice]}) + "\n\n"

    first = ('{"value":"' + key[:half], '{"value":"plain')
    calls = [
        {"index": i, "id": f"c{i}", "type": "function", "function": {"name": "Lookup", "arguments": piece}}
        for i, piece in enumerate(first)
    ]
    rest = [{"index": i, "function": {"arguments": piece}} for i, piece in enumerate((key[half:] + '"}', ' text"}'))]
    answer = (chunk(calls) + chunk(rest) + "data: [DONE]\n\n").encode()
    with pytest.raises(scrub.Refused, match=scrub.KEY):
        _capture_of("mock-chat-completions-stream", answer, record.Neutral(hosts=(), home="", user="", keys=(key,)))


def test_a_json_escaped_key_a_tool_input_streams_in_pieces_is_not_saved() -> None:
    key = generate_key()
    escaped = f"\\u{ord(key[0]):04x}" + key[1:]
    pieces = ['{"value":"' + escaped[:20], escaped[20:] + '"}']
    answer = _restreamed("input_json_delta", "partial_json", pieces + [""] * 3)
    with pytest.raises(scrub.Refused, match=scrub.KEY):
        _capture_of("mock-messages-stream", answer, record.Neutral(hosts=(), home="", user="", keys=(key,)))


def test_a_url_a_stream_writes_in_pieces_does_not_keep_the_endpoint_host() -> None:
    host = f"origin-{secrets.token_hex(4)}.example.com"
    answer = _restreamed("thinking_delta", "thinking", ["See https://", host[:9], host[9:] + "/help now."])
    with pytest.raises(scrub.Refused, match=scrub.HOST):
        _capture_of("mock-messages-stream", answer, record.Neutral(hosts=(host,), home="", user=""))


def test_subschemas_of_the_same_instance_follow_the_neutral_property_names(
    stack: tuple[str, str], tmp_path: Path
) -> None:
    address, key = stack
    schema = {
        "type": "object",
        "properties": {"operator": {"type": "integer"}, "user": {"type": "string"}},
        "dependentSchemas": {"user": {"required": ["operator"]}},
        "allOf": [{"required": ["operator"]}],
    }
    with recording(address, tmp_path, user="operator", profile="coursework") as (_, client):
        tools = [{"name": "Lookup", "input_schema": schema}]
        assert client.post("/v1/messages", headers=anthropic(key), json=messages(tools=tools)).status_code == 200
    (capture,) = harness.folder(tmp_path)
    saved = json.loads(capture.body)["tools"][0]["input_schema"]
    (renamed,) = [name for name, value in saved["properties"].items() if value == {"type": "integer"}]
    assert renamed != "operator" and saved["properties"]["user"] == {"type": "string"}
    assert saved["dependentSchemas"] == {"user": {"required": [renamed]}} and saved["allOf"] == [
        {"required": [renamed]}
    ]


def test_an_endpoint_host_a_stream_splits_after_a_neutral_word_is_not_saved() -> None:
    host = f"user-{secrets.token_hex(4)}.example.com"
    answer = _restreamed("thinking_delta", "thinking", ["See https://", "user", host[4:] + "/help now."])
    with pytest.raises(scrub.Refused, match=scrub.HOST):
        _capture_of("mock-messages-stream", answer, record.Neutral(hosts=(host,), home="", user=""))


def test_an_inline_document_naming_the_endpoint_host_is_not_written() -> None:
    host = f"origin-{secrets.token_hex(4)}.example.com"
    document = base64.b64encode(_pdf(f"(https://{host}/help)".encode())).decode()
    original = harness.load(harness.CAPTURES / "mock-responses.json")
    body = {"model": "mock", "input": [{"role": "user", "content": [{"type": "input_file", "file_data": document}]}]}
    exchange = record.Exchange(
        "POST", original.target.encode(), list(original.headers.items()), json.dumps(body).encode(), 200, (), b"{}"
    )
    with pytest.raises(scrub.Refused, match=scrub.HOST):
        asyncio.run(record.capture_of(exchange, record.Neutral(hosts=(host,), home="", user="")))


def test_a_property_a_schema_only_requires_keeps_a_name_of_its_own(stack: tuple[str, str], tmp_path: Path) -> None:
    address, key = stack
    schema = {"type": "object", "properties": {"user": {"type": "string"}}, "allOf": [{"required": ["operator"]}]}
    with recording(address, tmp_path, user="operator", profile="coursework") as (_, client):
        tools = [{"name": "Lookup", "input_schema": schema}]
        assert client.post("/v1/messages", headers=anthropic(key), json=messages(tools=tools)).status_code == 200
    (capture,) = harness.folder(tmp_path)
    saved = json.loads(capture.body)["tools"][0]["input_schema"]
    (required,) = saved["allOf"][0]["required"]
    assert required not in ("operator", "user") and list(saved["properties"]) == ["user"]
