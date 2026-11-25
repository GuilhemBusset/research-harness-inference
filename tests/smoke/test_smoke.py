"""The smoke tests both stacks answer (system design, sections 6.2 and 8.2, step 6).

They run against the stack of `conftest.py`: a deployed endpoint when the pipeline's verification
gives one, the local stack everywhere else. Two tests cannot assert the same thing on both:

- **The forbidden routes.** The gateway answers a path outside its table with a 404 and the cause
  `not_found`, and the local stack's gateway is reached directly, so both are required there. A
  deployed stack's public load balancer forwards only the paths of the gateway's table and answers
  every other path with its own fixed 404, which carries no cause: the status alone is required.
- **The pool nobody has booked.** A request starts no machine, and a pool is served only in its
  session (sections 5.3 and 5.6): `no-session` asserts that a request for a pool without one is
  answered 503 with `no_session`. The local stack's pool is unbooked until a test takes the
  fixture `booked`, and this test is written before those, so there it always runs on an unbooked
  pool. On a deployed stack the verification runs it alone, before it books the mock pool, and
  leaves it out of every other run (`scripts/pipeline/verify.py`).

Every stream test takes `booked`: the pool is in a session and reads ready before the first
stream, on the local stack because the fixture books it, on a deployed one because the
verification did. One task of a deployed gateway may read the pool a moment after another: a
stream that such a task answers 503, with a cause of a pool that cannot serve, is asked again for
that moment (`Stack.stream`), and the test still needs the whole stream and its usage.

Each test carries its fixed ID (`smoke_id`), listed in `ids.py`. The last tests are about the
smoke tests themselves, and run against no stack: the IDs, the collection of `deployed/`, and what
the environment asks for.
"""

from __future__ import annotations

import ast
import json
import os
import runpy
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from rhi_reporter.report import build_report, statuses_of

SMOKE = Path(__file__).resolve().parent
REPO = SMOKE.parents[1]
DEPLOYED = SMOKE / "deployed"
SMOKE_TEST_IDS: tuple[str, ...] = runpy.run_path(str(SMOKE / "ids.py"))["SMOKE_TEST_IDS"]

PROMPT = "Say hello."


def messages_body(pool: str) -> dict[str, Any]:
    return {"model": pool, "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": PROMPT}]}


def chat_body(pool: str) -> dict[str, Any]:
    return {
        "model": pool,
        "stream": True,
        # Chat Completions reports a stream's usage only to a request that asks for it.
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": PROMPT}],
    }


def responses_body(pool: str) -> dict[str, Any]:
    return {"model": pool, "stream": True, "input": PROMPT}


def data_of(stream: Any) -> list[tuple[str | None, Any]]:
    """The events of a stream that carry JSON, as their name and their data. Comments are left out."""
    read = []
    for event in stream.events:
        if not event.comment and event.data.startswith("{"):
            read.append((event.name, json.loads(event.data)))
    return read


def messages_usage(stream: Any) -> tuple[bool, int, int]:
    """Whether a Messages stream ended with its last event, and the input and output tokens it reported.

    In this format `input_tokens` leaves out what was read from the cache or written to it, which the
    usage counts beside it: the input is their sum, and a request repeated whole may have all of it cached.
    """
    events = data_of(stream)
    parts = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    starts = [
        sum(data["message"]["usage"].get(part) or 0 for part in parts)
        for name, data in events
        if name == "message_start"
    ]
    deltas = [data["usage"]["output_tokens"] for name, data in events if name == "message_delta"]
    ended = bool(events) and events[-1][0] == "message_stop"
    return ended, sum(starts), max(deltas, default=0)


def chat_usage(stream: Any) -> tuple[bool, int, int]:
    """The same of a Chat Completions stream, which ends with `[DONE]` after its usage chunk."""
    usages = [data["usage"] for _, data in data_of(stream) if data.get("usage")]
    ended = bool(stream.events) and stream.events[-1].data == "[DONE]"
    usage = usages[-1] if usages else {}
    return ended, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def responses_usage(stream: Any) -> tuple[bool, int, int]:
    """The same of a Responses stream, whose last event, `response.completed`, carries the usage."""
    events = data_of(stream)
    ended = bool(events) and events[-1][0] == "response.completed"
    usage = events[-1][1]["response"].get("usage") or {} if ended else {}
    return ended, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


# --- What both stacks answer ---------------------------------------------------------------------


@pytest.mark.smoke_id("hello")
def test_the_hello_route_answers(stack: Any) -> None:
    assert stack.request("HEAD", "/api/hello", keyed=False).status_code == 200


@pytest.mark.smoke_id("no-key")
def test_a_request_without_a_key_is_refused(stack: Any) -> None:
    answer = stack.request("GET", "/v1/models", keyed=False)
    assert answer.status_code == 401
    assert answer.json()["error"]["type"] == "invalid_key"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # The routes an engine answers without its secret, and the two only its own host calls.
        pytest.param("POST", "/invocations", marks=pytest.mark.smoke_id("forbidden-invocations")),
        pytest.param("POST", "/pause", marks=pytest.mark.smoke_id("forbidden-pause")),
        pytest.param("POST", "/tokenize", marks=pytest.mark.smoke_id("forbidden-tokenize")),
        pytest.param("GET", "/metrics", marks=pytest.mark.smoke_id("forbidden-metrics")),
        pytest.param("GET", "/health", marks=pytest.mark.smoke_id("forbidden-health")),
    ],
)
def test_a_forbidden_route_is_not_found(stack: Any, method: str, path: str) -> None:
    # With the canary's key: no key lets a request through to one of these routes.
    answer = stack.request(method, path)
    assert answer.status_code == 404
    if not stack.deployed:
        # The gateway itself answered. On a deployed stack the public load balancer did, with its
        # fixed 404, which carries no cause.
        assert answer.json()["error"]["type"] == "not_found"


@pytest.mark.smoke_id("no-session")
def test_a_request_to_a_pool_nobody_has_booked_is_answered_no_session(stack: Any) -> None:
    # Before any test that takes `booked`: the pool has no session, and the request starts nothing.
    body = {"model": stack.pool, "max_tokens": 64, "messages": [{"role": "user", "content": PROMPT}]}
    answer = stack.request("POST", "/v1/messages", json=body)
    assert answer.status_code == 503
    assert answer.json()["error"]["type"] == "no_session"


@pytest.mark.parametrize(
    ("path", "body", "usage_of"),
    [
        pytest.param("/v1/messages", messages_body, messages_usage, marks=pytest.mark.smoke_id("stream-messages")),
        pytest.param(
            "/v1/chat/completions", chat_body, chat_usage, marks=pytest.mark.smoke_id("stream-chat-completions")
        ),
        pytest.param("/v1/responses", responses_body, responses_usage, marks=pytest.mark.smoke_id("stream-responses")),
    ],
)
def test_a_stream_completes_with_its_usage(
    booked: Any,
    path: str,
    body: Callable[[str], dict[str, Any]],
    usage_of: Callable[[Any], tuple[bool, int, int]],
) -> None:
    stream = booked.stream(path, body(booked.pool))
    assert stream.status == 200
    ended, input_tokens, output_tokens = usage_of(stream)
    assert ended, "the stream did not end with the last event of its wire format"
    assert input_tokens > 0 and output_tokens > 0


# --- The smoke tests themselves ------------------------------------------------------------------

# Collects the folders given as arguments and prints, as the last line, each test's node ID with
# the arguments of its `smoke_id` markers. Run in a process of its own, so that the collection has
# an environment of its own. The configuration is the repository's, whichever folder is collected.
_COLLECT = """
import json, sys
import pytest

class Collected:
    tests = None

    def pytest_collection_finish(self, session):
        self.tests = [
            [item.nodeid, [list(marker.args) for marker in item.iter_markers("smoke_id")]] for item in session.items
        ]

collected = Collected()
pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "-c", sys.argv[1], *sys.argv[2:]], plugins=[collected])
print()
print(json.dumps(collected.tests))
"""


def collect(folder: Path, *, endpoint: bool) -> list[tuple[str, list[list[object]]]]:
    """The tests pytest collects in `folder`, with their `smoke_id` markers, with an endpoint given or not."""
    environ = {name: value for name, value in os.environ.items() if not name.startswith("RHI_SMOKE_")}
    if endpoint:
        # Nothing is run, so nothing is reached: collection needs no key and no network.
        environ["RHI_SMOKE_ENDPOINT"] = "https://smoke.example.com"
    done = subprocess.run(
        [sys.executable, "-c", _COLLECT, str(REPO / "pyproject.toml"), str(folder)],
        cwd=REPO,
        env=environ,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    tests = json.loads(done.stdout.splitlines()[-1]) if done.stdout.strip() else None
    assert tests is not None, f"the collection of {folder} did not finish:\n{done.stdout}\n{done.stderr}"
    return [(node, markers) for node, markers in tests]


def id_problems(tests: Sequence[tuple[str, list[list[object]]]], listed: Sequence[str]) -> list[str]:
    """What is wrong between the tests' IDs and the list: a test without one ID, an ID two tests
    share, an ID the list lacks, and an ID of the list that no test carries or that is listed twice."""
    problems: list[str] = []
    carried: Counter[str] = Counter()
    for node, markers in tests:
        if len(markers) != 1 or len(markers[0]) != 1 or not isinstance(markers[0][0], str):
            problems.append(f"{node} does not carry one ID")
            continue
        carried[markers[0][0]] += 1
        if markers[0][0] not in listed:
            problems.append(f"{node} carries an ID that is not listed")
    problems += [f"the ID {name} is carried by {count} tests" for name, count in carried.items() if count > 1]
    problems += [f"the ID {name} is listed twice" for name, count in Counter(listed).items() if count > 1]
    problems += [f"the listed ID {name} is carried by no test" for name in listed if name not in carried]
    return problems


@pytest.mark.smoke_id("ids-listed")
def test_ids_are_unique_and_listed(tmp_path: Path) -> None:
    # With an endpoint given, so that the deployed-only tests are among the collected.
    tests = collect(SMOKE, endpoint=True)
    assert any("/deployed/" in node for node, _ in tests)
    assert id_problems(tests, SMOKE_TEST_IDS) == []

    # The reporter's filter keeps every ID of the list: each is whole in a status description.
    failure = {"stage": "Rhi", "action": "Verify", "category": "Build", "state": "FAILED", "commit": "1a2b3c4d" * 5}
    report = build_report(
        failure, failed_tests=SMOKE_TEST_IDS, logical_ids=(), smoke_test_ids=SMOKE_TEST_IDS, lifecycle_stages=()
    )
    assert (report.tests, report.dropped) == (tuple(sorted(SMOKE_TEST_IDS)), 0)
    assert len(statuses_of(report)) == 1 + len(SMOKE_TEST_IDS)

    # The same check refuses a copy of the folder with a fault planted in it, under deployed/ too.
    copy = tmp_path / "smoke"
    shutil.copytree(SMOKE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    planted = (
        "import pytest\n\n\n"
        "def test_without_an_id() -> None:\n    pass\n\n\n"
        '@pytest.mark.smoke_id("never-listed-{where}")\ndef test_with_an_unlisted_id() -> None:\n    pass\n\n\n'
        '@pytest.mark.smoke_id("hello")\ndef test_with_another_test_s_id() -> None:\n    pass\n'
    )
    (copy / "test_planted.py").write_text(planted.format(where="here"), encoding="utf-8")
    (copy / "deployed" / "test_planted.py").write_text(planted.format(where="deployed"), encoding="utf-8")
    problems = id_problems(collect(copy, endpoint=True), SMOKE_TEST_IDS)
    for file in ("test_planted.py", "deployed/test_planted.py"):
        assert any(file + "::test_without_an_id does not carry one ID" in problem for problem in problems)
        assert any(
            file + "::test_with_an_unlisted_id carries an ID that is not listed" in problem for problem in problems
        )
    assert "the ID hello is carried by 3 tests" in problems
    assert len(problems) == 5


def defined_tests(folder: Path) -> set[str]:
    """Every test function the test files of `folder` define, as `<file>::<function>`."""
    defined = set()
    for file in sorted(folder.glob("test_*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        defined |= {
            f"{file.name}::{node.name}"
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_")
        }
    return defined


@pytest.mark.smoke_id("deployed-need-endpoint")
def test_deployed_tests_need_an_endpoint() -> None:
    defined = defined_tests(DEPLOYED)
    assert defined, "the deployed-only tests exist"

    assert collect(DEPLOYED, endpoint=False) == []

    # A node's ID is its file's path, `::`, its function and, in brackets, its parameters.
    collected = {node.partition("[")[0].rpartition("/")[2] for node, _ in collect(DEPLOYED, endpoint=True)}
    assert collected == defined


@pytest.mark.smoke_id("environment")
def test_the_environment_names_the_stack_and_the_pool(read_stack_target: Callable[[Mapping[str, str]], Any]) -> None:
    key = "the-canary-s-key"
    # No endpoint: the local stack, and the mock pool unless another is named.
    local = read_stack_target({})
    assert (local.endpoint, local.key, local.pool) == (None, None, "mock")
    assert read_stack_target({"RHI_SMOKE_POOL": "coursework"}).pool == "coursework"
    # A key alone does not name a stack to reach.
    assert read_stack_target({"RHI_SMOKE_KEY": key}).endpoint is None

    given = {"RHI_SMOKE_ENDPOINT": "https://api.example.com/", "RHI_SMOKE_KEY": key}
    deployed = read_stack_target(given)
    assert (deployed.endpoint, deployed.key, deployed.pool) == ("https://api.example.com", key, "mock")
    assert read_stack_target(given | {"RHI_SMOKE_POOL": "coursework"}).pool == "coursework"
    # An address without a scheme is reached over HTTPS.
    assert read_stack_target(given | {"RHI_SMOKE_ENDPOINT": "api.example.com"}).endpoint == "https://api.example.com"

    # An endpoint without its key is refused by the variable's name, never tested as the local stack.
    with pytest.raises(ValueError, match="RHI_SMOKE_KEY") as refused:
        read_stack_target({"RHI_SMOKE_ENDPOINT": "https://api.example.com"})
    assert "api.example.com" not in str(refused.value)
