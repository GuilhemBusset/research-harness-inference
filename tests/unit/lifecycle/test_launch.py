"""The launch of an instance (`rhi_lifecycle.launch`; system design, section 4.3), on one stand-in
for everything around the agent: the instance metadata and the engine, where the agent opens a
URL; Auto Scaling, the release parameter and the pool's secret, as SDK clients; the host module;
and a clock that only the stand-in moves. No test here needs AWS, docker or a wait, and the one
socket is a server of the test's own, on the loopback interface."""

from __future__ import annotations

import asyncio
import copy
import http.server
import json
import threading
import urllib.request
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from botocore.exceptions import ClientError

from rhi_common import launch_settings
from rhi_lifecycle import release
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.launch import (
    ENGINE_URL,
    METADATA_URL,
    NO_ACTION,
    Engine,
    Launch,
    Metadata,
    Outcome,
    fetch,
    identity,
    settings_fingerprint,
)
from rhi_mock_engine.app import Settings, create_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NAME = "/rhi/pools/coursework/release"
IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/rhi/engine@sha256:{'a' * 64}"
# Made-up values, built here: neither is an instance or a credential.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])
TOKEN = b"the-session-token"

VALUE: dict[str, Any] = {
    "engine_image": IMAGE,
    "model_revision": None,
    "engine_flags": ["--max-model-len", "65536"],
    "files": {"bucket": "artifacts", "key": "releases/coursework.tar", "sha256": "b" * 64},
    "settings": {
        "pool": "coursework",
        "group": "rhi-coursework",
        "launch_hook": "launch",
        "termination_hook": "termination",
        "log_group": "/rhi/coursework",
        "workspace_url": "https://aps-workspaces.example.com/api/v1/remote_write",
        "heartbeat_table": "rhi-heartbeats",
        "secret_id": "rhi/coursework/engine",
    },
}

# The arguments of a vllm pool's engine that the tests of the fingerprint vary: the model, in a
# bucket named after the placeholder account, and the step budget.
MODEL = f"s3://rhi-models-123456789012-us-east-1/coursework/{'c' * 40}/"
STEP_BUDGET = "--max-num-batched-tokens"

IN_SERVICE = "InService"
# A hook with no timeout of its own waits this long (evidence E10), four times what the launch
# hook has: the test's clock stops there.
AN_HOUR = 3600


class StillWaiting(Exception):
    """The agent was still waiting when the test stopped the world."""


def refusal(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, operation)


class World(Host):
    """An instance prepared with the release of `VALUE`, and everything around its agent. `calls`
    holds the steps in the order they happened; `completions` what reached Auto Scaling."""

    def __init__(self, folder: Path, *, on_disk: bool, measured: int | None = None) -> None:
        super().__init__(ecr=object())
        self.now = 0.0
        self.calls: list[str] = []
        self.completions: list[dict[str, str]] = []
        self.target: str | None = IN_SERVICE
        self.parameter: dict[str, Any] | None = copy.deepcopy(VALUE)
        if self.parameter is not None and measured is not None:
            self.parameter["measured_start_seconds"] = measured
        self.held = folder / "release.json"
        self.held.write_text(json.dumps(VALUE))
        # The host: whether its docker holds the image, and how a pull and a start go.
        self.on_disk = on_disk
        self.pull_seconds = 120.0
        self.pull_fails = False
        self.pull_limits: list[float | None] = []
        self.start_fails = False
        # The engine's flags, as the boot program wrote them for the unit, and what docker holds of
        # the engine's container: the flags it was started with, None while none runs. `asked`
        # says whether docker can be asked.
        self.flags: tuple[str, ...] = tuple(VALUE["engine_flags"])
        self.launched: tuple[str, ...] | None = None
        self.asked = True
        # The record of a launch under way: whether the host keeps one, and one that its steps
        # passed, whether each can be written, and what the two were at each of the steps named.
        self.on_record = False
        self.passed = False
        self.record_fails = False
        self.pass_fails = False
        self.recorded: list[tuple[str, bool, bool]] = []
        # The error code of Auto Scaling's answer to a completion, when it takes none.
        self.refuses: str | None = None
        # The engine: healthy this long after its start (None: never), and its answer to the
        # warm-up request (None: none, the request stays unanswered).
        self.started_at: float | None = None
        self.start_seconds: float | None = 0.0
        self.warm_up: tuple[int, bytes] | None = (200, b'{"choices": []}')

    def launch(self) -> Launch:
        return Launch(
            NAME,
            metadata=Metadata(self.fetch),
            autoscaling=self,
            ssm=self,
            secrets=self,
            dynamodb=self,
            host=self,
            engine=Engine(self.fetch),
            held=self.held,
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now >= AN_HOUR:
            raise StillWaiting

    # The host module.

    def holds(self, image: str, timeout: float | None = None) -> bool:
        return image == IMAGE and self.on_disk

    def pull(self, image: str, timeout: float | None = None) -> None:
        self.calls.append("pull")
        self.pull_limits.append(timeout)
        if self.pull_fails or image != IMAGE:
            raise HostError("pull", 1)
        if timeout is not None and self.pull_seconds > timeout:
            # The host module stops a pull that is still running at its time limit.
            self.now += timeout
            raise HostError("pull")
        self.now += self.pull_seconds
        self.on_disk = True

    def start_engine(self, timeout: float | None = None) -> None:
        self.calls.append("start")
        self.recorded.append(("start", self.on_record, self.passed))
        if self.start_fails:
            raise HostError("start", 1)
        self.started_at = self.now
        # The unit gives the container the flags that are on the host at this start.
        self.launched = self.flags

    def engine_arguments(self) -> tuple[str, ...] | None:
        if not self.asked:
            raise HostError("inspect")
        return self.launched

    def begin_launch(self) -> None:
        if self.record_fails:
            raise HostError("record", 1)
        self.on_record = True

    def pass_launch(self) -> None:
        if self.pass_fails:
            raise HostError("record", 1)
        self.passed = True

    def end_launch(self) -> None:
        self.on_record = self.passed = False

    # The SDK's clients.

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        if request != {"Name": NAME} or self.parameter is None:
            raise refusal("GetParameter")
        return {"Parameter": {"Name": NAME, "Value": json.dumps(self.parameter)}}

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        if request != {"SecretId": VALUE["settings"]["secret_id"]}:
            raise refusal("GetSecretValue")
        return {"SecretString": SECRET}

    def complete_lifecycle_action(self, **request: str) -> dict[str, Any]:
        self.calls.append("complete")
        self.recorded.append(("complete", self.on_record, self.passed))
        if self.refuses is not None:
            raise ClientError({"Error": {"Code": self.refuses}}, "CompleteLifecycleAction")
        self.completions.append(request)
        return {}

    # The heartbeats table, where a launch names the stage it has reached: tests/unit/lifecycle/test_stages.py.

    def Table(self, name: str) -> World:
        return self

    def put_item(self, **request: Any) -> dict[str, Any]:
        return {}

    # The instance metadata and the engine: what answers when the agent opens a URL.

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        url, method = request.full_url, request.get_method()
        if url == f"{METADATA_URL}/api/token":
            asked = method == "PUT" and request.get_header("X-aws-ec2-metadata-token-ttl-seconds")
            return (200, TOKEN) if asked else (400, b"")
        if url.startswith(f"{METADATA_URL}/meta-data/"):
            # IMDSv2: no item without the session token.
            if request.get_header("X-aws-ec2-metadata-token") != TOKEN.decode():
                return 401, b""
            items = {"autoscaling/target-lifecycle-state": self.target, "instance-id": INSTANCE}
            item = items.get(url.removeprefix(f"{METADATA_URL}/meta-data/"))
            return (404, b"") if item is None else (200, item.encode())
        if self.started_at is None:
            return None  # nothing listens before the engine's start
        if (url, method) == (f"{ENGINE_URL}/health", "GET"):
            self.calls.append("health")
            healthy = self.start_seconds is not None and self.now - self.started_at >= self.start_seconds
            return (200 if healthy else 503), b""
        if (url, method) == (f"{ENGINE_URL}/v1/chat/completions", "POST"):
            self.calls.append("warm-up")
            if self.warm_up is None:
                # The agent gives the request no time limit: it would wait on, as the test's world cannot.
                assert timeout is None
                raise StillWaiting
            if request.get_header("Authorization") != f"Bearer {SECRET}":
                return 401, b""
            assert isinstance(request.data, bytes)
            body = json.loads(request.data)
            if body.get("model") != VALUE["settings"]["pool"] or not body.get("messages"):
                return 400, b""
            return self.warm_up
        return 404, b""


@pytest.fixture(autouse=True)
def recorded_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release check stays the real one, and is written down among the steps."""
    real = release.check

    def check(ssm: Any, name: str, path: Path) -> release.Verdict:
        ssm.calls.append("check")
        return real(ssm, name, path)

    monkeypatch.setattr(release, "check", check)


def completion(result: str) -> dict[str, str]:
    """The call that completes the launch hook the settings name, for this instance."""
    return {
        "AutoScalingGroupName": "rhi-coursework",
        "LifecycleHookName": "launch",
        "InstanceId": INSTANCE,
        "LifecycleActionResult": result,
    }


def test_a_launch_pulls_checks_starts_waits_warms_up_and_only_then_completes(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=False)
    world.start_seconds = 4.0  # not healthy at once: the agent asks again

    assert world.launch().run() is Outcome.CONTINUED

    assert world.calls == ["pull", "check", "start", "health", "health", "health", "warm-up", "complete"]
    assert world.completions == [completion("CONTINUE")]


def test_a_launch_pulls_no_image_its_disk_holds(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=True)

    assert world.launch().run() is Outcome.CONTINUED

    assert world.calls == ["check", "start", "health", "warm-up", "complete"]
    assert world.completions == [completion("CONTINUE")]


def test_a_launch_keeps_no_deadline_of_its_own(tmp_path: Path) -> None:
    # The pull alone outlasts the hook's 15 minutes, and twice the pool's measured start: the agent
    # gives it no time limit and abandons nothing. Only the hook's own timeout bounds the launch.
    world = World(tmp_path, on_disk=False, measured=200)
    world.pull_seconds = 1000.0
    world.start_seconds = 100.0

    assert world.launch().run() is Outcome.CONTINUED

    assert world.pull_limits == [None]
    assert world.completions == [completion("CONTINUE")]
    assert world.now >= 1100


def never_healthy(world: World) -> None:
    world.start_seconds = None


def unanswered_warm_up(world: World) -> None:
    world.warm_up = None


@pytest.mark.parametrize(
    ("silence", "steps"),
    [
        pytest.param(never_healthy, ["check", "start", "health"], id="health-route-never-answers"),
        pytest.param(unanswered_warm_up, ["check", "start", "health", "warm-up"], id="warm-up-request-unanswered"),
    ],
)
def test_an_engine_that_never_answers_is_left_to_the_hooks_timeout(
    silence: Callable[[World], None], steps: list[str], tmp_path: Path
) -> None:
    world = World(tmp_path, on_disk=True, measured=200)
    silence(world)

    # The agent still waits when the test stops the world, far past the hook's timeout.
    with pytest.raises(StillWaiting):
        world.launch().run()

    # It completed nothing, so the instance never entered service: the launch hook's timeout
    # abandons it, by the hook's default result (tests/synth/test_pool_hooks.py).
    assert list(dict.fromkeys(world.calls)) == steps
    assert world.completions == []


def another_release(world: World) -> None:
    assert world.parameter is not None
    world.parameter["engine_flags"] = ["--max-model-len", "32768"]


def failed_pull(world: World) -> None:
    world.pull_fails = True


def failed_start(world: World) -> None:
    world.start_fails = True


def failed_warm_up(world: World) -> None:
    world.warm_up = (500, b"")


@pytest.mark.parametrize(
    ("on_disk", "failure", "steps"),
    [
        pytest.param(False, another_release, ["pull", "check"], id="release-differs"),
        pytest.param(False, failed_pull, ["pull"], id="pull-fails"),
        pytest.param(True, failed_start, ["check", "start"], id="engine-does-not-start"),
        pytest.param(True, failed_warm_up, ["check", "start", "health", "warm-up"], id="warm-up-request-fails"),
    ],
)
def test_failures_abandon(on_disk: bool, failure: Callable[[World], None], steps: list[str], tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=on_disk, measured=200)
    failure(world)

    assert world.launch().run() is Outcome.ABANDONED

    # The steps up to the one that failed, then one completion.
    assert world.calls == [*steps, "complete"]
    assert world.completions == [completion("ABANDON")]


def releasing(world: World, *flags: str) -> World:
    """`world`, whose pool's release now carries `flags`: in the parameter, in the record on disk
    and among the flags the boot program wrote, which the next start of the engine takes."""
    assert world.parameter is not None
    world.parameter["engine_flags"] = list(flags)
    world.held.write_text(json.dumps({**VALUE, "engine_flags": list(flags)}))
    world.flags = flags
    return world


def test_two_launches_of_one_image_and_model_with_different_step_budgets_have_different_fingerprints(
    tmp_path: Path,
) -> None:
    found = []
    for budget in ("4096", "8192"):
        (tmp_path / budget).mkdir()
        world = releasing(World(tmp_path / budget, on_disk=True), MODEL, "--max-model-len", "65536", STEP_BUDGET, budget)
        assert world.launch().run() is Outcome.CONTINUED
        current = release.read_current(world, NAME)
        assert current is not None
        found.append((identity(current.release), settings_fingerprint(world)))

    (first, small), (second, large) = found
    # The image digest and the model revision tell the two launches apart no more than before.
    assert first == second
    assert launch_settings.UNAVAILABLE not in (small, large)
    assert small != large


def test_the_fingerprint_is_the_running_engines_whatever_the_release_says_since(tmp_path: Path) -> None:
    world = releasing(World(tmp_path, on_disk=True), MODEL, STEP_BUDGET, "4096")
    # No engine runs yet: what the release wants is no evidence.
    assert settings_fingerprint(world) == launch_settings.UNAVAILABLE
    assert world.launch().run() is Outcome.CONTINUED
    launched = settings_fingerprint(world)
    assert launched == launch_settings.fingerprint([MODEL, STEP_BUDGET, "4096"])

    # The pool's release gets another step budget: the parameter, the record on disk and the
    # flags for the next start all say so. The engine that runs was started with none of them.
    releasing(world, MODEL, STEP_BUDGET, "8192")
    assert settings_fingerprint(world) == launched
    # Only another start gives an engine the new budget.
    world.start_engine()
    assert settings_fingerprint(world) == launch_settings.fingerprint([MODEL, STEP_BUDGET, "8192"]) != launched


def no_container(world: World) -> None:
    # As under a unit that does not label its container, or when the engine's start failed.
    world.launched = None


def no_answer(world: World) -> None:
    world.asked = False


def unknown_argument(world: World) -> None:
    world.launched = (MODEL, "--chat-template", "/srv/template.jinja")


@pytest.mark.parametrize("missing", [no_container, no_answer, unknown_argument])
def test_launch_evidence_the_host_does_not_give_is_unavailable(
    missing: Callable[[World], None], tmp_path: Path
) -> None:
    world = releasing(World(tmp_path, on_disk=True), MODEL, STEP_BUDGET, "4096")
    assert world.launch().run() is Outcome.CONTINUED
    missing(world)
    assert settings_fingerprint(world) == launch_settings.UNAVAILABLE


def test_an_engine_whose_start_failed_has_no_fingerprint(tmp_path: Path) -> None:
    world = releasing(World(tmp_path, on_disk=True), MODEL, STEP_BUDGET, "4096")
    failed_start(world)
    assert world.launch().run() is Outcome.ABANDONED
    assert settings_fingerprint(world) == launch_settings.UNAVAILABLE


def test_a_launch_is_on_record_until_auto_scaling_has_taken_its_completion(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=True)

    assert world.launch().run() is Outcome.CONTINUED

    # On record when the engine's unit was started and still when the hook was completed: an
    # agent restarted in between finds a launch whose hook waits, not an instance that serves.
    # Its steps are on record as passed only once they have, and before the completion is sent.
    assert world.recorded == [("start", True, False), ("complete", True, True)]
    assert not world.on_record
    assert not world.passed


def test_an_abandoned_launch_stays_on_record(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=True)
    failed_warm_up(world)

    assert world.launch().run() is Outcome.ABANDONED

    # Its engine was started, and the instance never served: no record says its steps passed.
    assert world.on_record
    assert not world.passed


def test_a_launch_that_cannot_be_put_on_record_is_abandoned_before_the_engine_starts(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=True)
    world.record_fails = True

    assert world.launch().run() is Outcome.ABANDONED

    assert world.calls == ["complete"]
    assert world.completions == [completion("ABANDON")]


def test_a_launch_that_cannot_put_its_steps_on_record_as_passed_is_abandoned(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=True)
    world.pass_fails = True

    assert world.launch().run() is Outcome.ABANDONED

    assert world.calls == ["check", "start", "health", "warm-up", "complete"]
    assert world.completions == [completion("ABANDON")]


@pytest.mark.parametrize(
    ("refuses", "outcome"),
    [
        pytest.param(None, Outcome.CONTINUED, id="the-hook-still-waits"),
        pytest.param(NO_ACTION, Outcome.REFUSED, id="no-action-waits"),
        pytest.param("Throttling", Outcome.NOT_COMPLETED, id="the-call-fails"),
    ],
)
def test_a_launch_whose_steps_passed_is_resumed_by_its_completion_alone(
    refuses: str | None, outcome: Outcome, tmp_path: Path
) -> None:
    # The agent stopped after the launch's last step: only the answer to its completion is missing.
    world = World(tmp_path, on_disk=True)
    world.on_record = world.passed = True
    world.refuses = refuses

    assert world.launch().resume() is outcome

    # No step is repeated but the check of the release. Only a completion that Auto Scaling took
    # lets the instance in, and leaves no launch on record: one it refused does not, because the
    # same answer is given for a hook that is over and for one that waits under another name.
    assert world.calls == ["check", "complete"]
    assert world.completions == ([completion("CONTINUE")] if refuses is None else [])
    assert world.on_record is world.passed is (outcome is not Outcome.CONTINUED)


def test_a_resumed_launch_whose_release_differs_by_now_is_abandoned(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=True)
    world.on_record = world.passed = True
    another_release(world)

    assert world.launch().resume() is Outcome.ABANDONED

    assert world.calls == ["check", "complete"]
    assert world.completions == [completion("ABANDON")]
    assert world.on_record


@pytest.mark.parametrize("target", ["Terminated", "Warmed:Stopped", None])
def test_nothing_is_done_outside_a_launch(target: str | None, tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=True)
    world.target = target

    # Without a target state the agent does not know that the instance is launching.
    assert world.launch().run() is (Outcome.NOT_COMPLETED if target is None else Outcome.NOT_A_LAUNCH)
    assert world.calls == []


def test_without_the_parameter_no_hook_is_named_and_nothing_is_completed(tmp_path: Path) -> None:
    world = World(tmp_path, on_disk=False)
    world.parameter = None

    assert world.launch().run() is Outcome.NOT_COMPLETED
    assert world.calls == []


def test_the_warm_up_is_a_request_the_engine_contract_answers() -> None:
    # The mock engine, in process.
    app = create_app(Settings(secret=SECRET))

    def to_the_mock(request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        async def call() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
                return await http.request(
                    request.get_method(),
                    request.full_url,
                    headers=dict(request.header_items()),
                    content=request.data if isinstance(request.data, bytes) else None,
                )

        answer = asyncio.run(call())
        return answer.status_code, answer.content

    engine = Engine(to_the_mock)
    assert engine.healthy()
    assert engine.warm_up("coursework", SECRET)
    assert not engine.warm_up("coursework", "another secret")


def test_a_request_gives_the_status_and_the_body_or_nothing_when_nothing_answers() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"ready" if self.path == "/health" else b"not ready"
            self.send_response(200 if self.path == "/health" else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    here = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert fetch(urllib.request.Request(f"{here}/health"), 5) == (200, b"ready")
        # A refusal is an answer: its status is what the agent reads.
        answer = fetch(urllib.request.Request(f"{here}/other"), 5)
        assert answer is not None
        assert answer[0] == 503
    finally:
        server.shutdown()
        server.server_close()

    # The port is closed now: nothing listens, as before the engine's start.
    assert fetch(urllib.request.Request(f"{here}/health"), 5) is None
