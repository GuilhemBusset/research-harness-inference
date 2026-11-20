"""The two launch transitions of an instance, each completed or abandoned (system design, section 4.3).

Without an answer a lifecycle hook waits, and a GPU bills while it does. So when an instance
starts, the agent finds which transition it is in, does that transition's steps, and completes the
launch hook: with `CONTINUE` when every step passed, with `ABANDON` otherwise, and Auto Scaling
then replaces the instance.

**The transition.** One launch hook, and one wait state, serve every case (evidence E10), so the
agent chooses by the target lifecycle state in the instance metadata:

| Target state | Case | Steps, in order | The agent's own deadline |
| --- | --- | --- | --- |
| a warm-pool state | entry to the warm pool | pull the engine image, check the release | 15 minutes |
| `InService`, image on disk | promotion from the warm pool | check the release, start the engine, wait for its health route, send one warm-up request | twice the measured start, or none |
| `InService`, no image on disk | launch straight into service | pull the engine image, then the steps of a promotion | none |

An instance launched straight into service never entered the warm pool: the pool was empty, or
keeps no warm instance. Its start is not the one that was measured, so only the hook's own timeout
bounds it. The same holds for a promotion while the parameter gives no measured start.

An instance that returns from service to the warm pool has a warm-pool target state too, and meets
the same wait state. It is leaving service, not entering the pool: `Launch.run` hands an instance
that has served since its start to `rhi_lifecycle.termination`, which waits for the engine to drain
and completes the termination hook, and does none of an entry's steps. It knows one by a file it
writes before it completes a hook into service, in a folder the host empties at every start
(`SERVED_FILE`).

**Abandon.** A release that differs, a step that fails and the transition's deadline all end the
same way. Every step takes what is left of the deadline as its time limit, so a pull that hangs, or
a health route that is slow to answer, is abandoned at the deadline and not when it returns.

Whether the disk holds the image is the first question of a launch into service, and until the
host has answered it the launch may be a promotion. So where a start was measured, the question is
asked within the promotion's deadline, and a docker that does not answer in that time is abandoned.

**What cannot be completed.** The hook is named in the settings of the release parameter, and the
instance by its metadata. Without one of them there is nothing to call: `run` returns
`NOT_COMPLETED`, and the hook's own timeout abandons the instance.

**The stages.** A launch marks where each stage of the start begins and ends
(`rhi_lifecycle.stages`, which says what each stage covers), so the agent's metrics give the
seconds each took. At the start of each stage the launch also rewrites the instance's row in the
heartbeats table, with the stage it has reached and the release it is starting. Nothing runs on the
instance yet, so the row's counts are zero. An abandoned launch does not reach the hook stage: its
row keeps the stage it was abandoned in. The boot is over when the agent starts, so no row names
it. A row that cannot be written changes nothing else: the launch goes on. The time a write takes
is its stage's, and counts toward the transition's deadline as any step's does.

Every action on the host goes through `rhi_lifecycle.host`. Nothing here repeats what AWS, docker
or the engine answered.
"""

from __future__ import annotations

import enum
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from rhi_common.records import InstanceHeartbeat
from rhi_lifecycle import release, stages
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.stages import Stage, Transition

if TYPE_CHECKING:
    from rhi_lifecycle.termination import Termination

# The deadline of an entry to the warm pool (section 4.3; section 17 gives the same 15 minutes to
# the hook itself until a start is measured).
ENTRY_DEADLINE_SECONDS = 15 * 60
# A promotion's deadline is this many times the pool's measured start.
PROMOTION_DEADLINE_STARTS = 2
# The wait between two questions to the engine's health route.
HEALTH_INTERVAL_SECONDS = 2.0
# How long one answer of the instance metadata, or of the health route, may take.
ANSWER_SECONDS = 5.0

# The results of a lifecycle action.
CONTINUE = "CONTINUE"
ABANDON = "ABANDON"

# The target lifecycle states of a launch. The others (`Terminated`, `Warmed:Terminated`, ...) are
# not launches.
IN_SERVICE = "InService"
WARM_STATES = frozenset({"Warmed:Stopped", "Warmed:Running", "Warmed:Hibernated"})

# Written when the instance is about to enter service. `/run` is in memory, so the file is gone
# when the instance stops: the agent's unit gives the agent the host's folder.
SERVED_FILE = Path("/run/rhi/served")

# The model revision of a pool that serves no model: the heartbeat's row takes no empty value.
NO_MODEL = "none"

# The instance metadata service, and the engine on its own instance (section 4.1).
METADATA_URL = "http://169.254.169.254/latest"
ENGINE_URL = "http://127.0.0.1:8000"

# A request, and the seconds its answer may take (None: no limit), give the answer's status and
# body, or None when nothing answered.
Fetch = Callable[[urllib.request.Request, float | None], tuple[int, bytes] | None]

# Neither the metadata service nor the engine is behind a proxy, whatever the environment says.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_LOG = logging.getLogger(__name__)


def fetch(request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
    """Send `request` over HTTP."""
    try:
        with _OPENER.open(request, timeout=timeout) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as refusal:
        refusal.close()
        return refusal.code, b""
    except (OSError, http.client.HTTPException):
        return None


class MetadataError(Exception):
    """The instance metadata did not give an item. The message names the item, never a value."""


class NoSuchItemError(MetadataError):
    """The instance metadata answered that it has no such item: the answer is the service's own,
    to a request that carried its session token, and not a reading that failed."""


class Metadata:
    """The instance metadata, read with a session token (IMDSv2 is required; section 4.3)."""

    def __init__(self, fetch: Fetch = fetch) -> None:
        self._fetch = fetch

    def target_state(self) -> str:
        """The lifecycle state Auto Scaling is moving the instance to. An instance that belongs to
        no group has none, and the metadata says so: `NoSuchItemError`."""
        return self._item("autoscaling/target-lifecycle-state")

    def instance_id(self) -> str:
        return self._item("instance-id")

    def _item(self, item: str) -> str:
        token = self._text(
            urllib.request.Request(
                f"{METADATA_URL}/api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}
            ),
            "the session token",
            token=True,
        )
        return self._text(
            urllib.request.Request(f"{METADATA_URL}/meta-data/{item}", headers={"X-aws-ec2-metadata-token": token}),
            item,
        )

    def _text(self, request: urllib.request.Request, item: str, *, token: bool = False) -> str:
        answer = self._fetch(request, ANSWER_SECONDS)
        if answer is not None and answer[0] == 200:
            try:
                text = answer[1].decode("ascii").strip()
            except UnicodeDecodeError:
                text = ""
            if text:
                return text
        if answer is not None and answer[0] == 404 and not token:
            raise NoSuchItemError(item)
        raise MetadataError(item)


class Engine:
    """The engine of this instance, as the launch asks it: its health route and one inference."""

    def __init__(self, fetch: Fetch = fetch) -> None:
        self._fetch = fetch

    def healthy(self, timeout: float | None = None) -> bool:
        """Whether `GET /health` answers 200: the model can serve (section 4.1). The answer may
        take `ANSWER_SECONDS`, or `timeout` seconds when that is less."""
        limit = ANSWER_SECONDS if timeout is None else min(ANSWER_SECONDS, timeout)
        answer = self._fetch(urllib.request.Request(f"{ENGINE_URL}/health"), limit)
        return answer is not None and answer[0] == 200

    def warm_up(self, model: str, secret: str, timeout: float | None = None) -> bool:
        """Send one real request, a completion of one token by `model` with the pool's `secret`,
        and say whether the engine answered it. The health route cannot say that: an engine can
        hang with its health route still answering (evidence E23)."""
        body = {"model": model, "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 1}
        request = urllib.request.Request(
            f"{ENGINE_URL}/v1/chat/completions",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        )
        answer = self._fetch(request, timeout)
        if answer is None or answer[0] != 200:
            return False
        try:
            return isinstance(json.loads(answer[1]), dict)
        except ValueError:
            return False


class Outcome(enum.Enum):
    """How `Launch.run`, or `Termination.run`, ended."""

    # The launch hook was completed with `CONTINUE`.
    CONTINUED = "continued"
    # The launch hook was completed with `ABANDON`.
    ABANDONED = "abandoned"
    # The engine was idle, and the termination hook was completed: the instance may leave service.
    DRAINED = "drained"
    # The instance is in no launch transition: nothing was done.
    NOT_A_LAUNCH = "not-a-launch"
    # The instance is not being terminated: nothing was done.
    NOT_A_TERMINATION = "not-a-termination"
    # No completion reached Auto Scaling: the transition, the instance or the hook is unknown, or
    # the call failed. The hook's own timeout ends the wait.
    NOT_COMPLETED = "not-completed"


class Abandon(Exception):
    """A step did not pass, or the deadline did: a launch completes its hook with `ABANDON`."""


def identity(served: release.Release) -> tuple[str, str]:
    """The image digest and the model revision a heartbeat's row carries for the release `served`."""
    # The digest is what follows the repository in the image's reference.
    digest = served.engine_image.rpartition("@")[2]
    return digest, NO_MODEL if served.model_revision is None else served.model_revision


@dataclass(frozen=True)
class Steps:
    """The steps of a start, as a launch transition and the run of a canary do them
    (`rhi_lifecycle.canary`): the two share them, so they cannot differ. `parameter`, `ssm`,
    `secrets`, `host`, `engine`, `held`, `clock` and `sleep` are `Launch`'s. A step takes what is
    left before `deadline` as its time limit (None: no limit), and raises `Abandon` when it does
    not pass or the deadline has."""

    parameter: str
    ssm: Any
    secrets: Any
    host: Host
    engine: Engine
    held: Path
    clock: Callable[[], float]
    sleep: Callable[[float], object]

    def left(self, deadline: float | None) -> float | None:
        """The seconds left before `deadline`, or None when there is no deadline."""
        if deadline is None:
            return None
        left = deadline - self.clock()
        if left <= 0:
            raise Abandon
        return left

    def pull(self, current: release.Current, deadline: float | None) -> None:
        try:
            self.host.pull(current.release.engine_image, timeout=self.left(deadline))
        except HostError:
            raise Abandon from None

    def check(self, current: release.Current) -> None:
        """Abandon unless the disk holds the pool's current release, which is still the one whose
        image this start pulled or found."""
        verdict = release.check(self.ssm, self.parameter, self.held)
        if not verdict.equal or verdict.held != current.release:
            raise Abandon

    def serve(self, current: release.Current, deadline: float | None, reach: Callable[[Stage], object]) -> None:
        """With the image on disk: check the release, start the engine, wait for its health route
        and send the warm-up request. `reach` is told each stage as it starts."""
        reach(Stage.RELEASE_CHECK)
        self.check(current)
        reach(Stage.CONTAINER_START)
        secret = self._secret(current.settings.secret_id)
        try:
            self.host.start_engine(timeout=self.left(deadline))
        except HostError:
            raise Abandon from None
        reach(Stage.WEIGHTS)
        while not self.engine.healthy(self.left(deadline)):
            left = self.left(deadline)
            self.sleep(HEALTH_INTERVAL_SECONDS if left is None else min(HEALTH_INTERVAL_SECONDS, left))
        reach(Stage.WARM_UP)
        # The pool's name is the model name the engine serves (section 4.2).
        if not self.engine.warm_up(current.settings.pool, secret, timeout=self.left(deadline)):
            raise Abandon
        self.left(deadline)

    def _secret(self, secret_id: str) -> str:
        """The pool's bearer secret, which the engine's inference routes require (section 4.1)."""
        try:
            secret = self.secrets.get_secret_value(SecretId=secret_id)["SecretString"]
        except (BotoCoreError, ClientError, KeyError, TypeError):
            raise Abandon from None
        if not isinstance(secret, str) or not secret:
            raise Abandon
        return secret


@dataclass(frozen=True)
class _Start:
    """One launch under way: what its stages took, and the instance's row in the heartbeats
    `table`, which `row` is but for the stage and the time."""

    timings: stages.Timings
    table: Any
    row: InstanceHeartbeat
    wall: Callable[[], float]

    def reach(self, stage: Stage, at: float | None = None) -> None:
        """Mark the start of `stage`, now or at the moment `at` of the launch's clock, and name the
        stage in the instance's row."""
        self.timings.start(stage, at)
        row = replace(self.row, stage=stage.value, written_at=int(self.wall()))
        try:
            self.table.put_item(Item=row.to_item())
        except (BotoCoreError, ClientError) as error:
            _LOG.warning("the heartbeat was not written (%s)", type(error).__name__)


@dataclass(frozen=True)
class Launch:
    """One instance's launch. `parameter` is the name of its pool's release parameter;
    `autoscaling`, `ssm` and `secrets` are the AWS SDK's clients for Auto Scaling, Systems Manager
    and Secrets Manager, and `dynamodb` is boto3's DynamoDB resource. `termination` is what an
    instance that leaves service is handed to. `held` is the record of the release on disk,
    `served` the file that says the instance has served since its start. `clock` and `sleep` are
    arguments so that a test waits for nothing. `wall` gives the seconds since the epoch, for the
    heartbeat's row, and `uptime` the seconds since the kernel started, which are the boot stage."""

    parameter: str
    metadata: Metadata
    autoscaling: Any
    ssm: Any
    secrets: Any
    dynamodb: Any
    host: Host
    engine: Engine
    termination: Termination
    held: Path = release.HELD_RELEASE_FILE
    served: Path = SERVED_FILE
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep
    wall: Callable[[], float] = time.time
    uptime: Callable[[], float | None] = stages.uptime

    def run(self) -> Outcome:
        """Do the steps of the transition the instance is in, and complete its hook: the launch
        hook, or the termination hook for an instance that returns to the warm pool."""
        started = self.clock()
        # The boot ends where the launch starts: both are read now, before anything takes time.
        booted = self.uptime()
        try:
            target = self.metadata.target_state()
            instance = self.metadata.instance_id()
        except MetadataError:
            return Outcome.NOT_COMPLETED
        into_service = target == IN_SERVICE
        if not into_service and target not in WARM_STATES:
            return Outcome.NOT_A_LAUNCH
        if not into_service and self.served.exists():
            # The instance has served: it is leaving service for the warm pool, not entering it.
            return self.termination.drain(instance)
        current = release.read_current(self.ssm, self.parameter)
        if current is None:
            return Outcome.NOT_COMPLETED
        # A launch into service may be a promotion until the host says its disk has no image.
        start = self._start(current, instance, Transition.PROMOTION if into_service else Transition.ENTRY)
        if booted is not None:
            start.timings.start(Stage.BOOT, at=started - booted)
        start.reach(Stage.IMAGE, at=started)
        try:
            if into_service:
                self._enter_service(current, started, start)
            else:
                self._enter_the_warm_pool(current, started + ENTRY_DEADLINE_SECONDS, start)
        except Abandon:
            result = ABANDON
            # The stage the launch was in ends here, and the instance's row keeps naming it.
            start.timings.end()
        else:
            result = CONTINUE
        try:
            self.autoscaling.complete_lifecycle_action(
                AutoScalingGroupName=current.settings.group,
                LifecycleHookName=current.settings.launch_hook,
                InstanceId=instance,
                LifecycleActionResult=result,
            )
        except (BotoCoreError, ClientError):
            return Outcome.NOT_COMPLETED
        finally:
            # The hook stage of a launch that passed. An abandoned launch is in no stage by now.
            start.timings.end()
        return Outcome.CONTINUED if result == CONTINUE else Outcome.ABANDONED

    def _start(self, current: release.Current, instance: str, transition: Transition) -> _Start:
        digest, revision = identity(current.release)
        row = InstanceHeartbeat(
            instance=instance,
            pool=current.settings.pool,
            running=0,
            waiting=0,
            image_digest=digest,
            model_revision=revision,
            stage=Stage.IMAGE.value,
            written_at=0,
        )
        table = self.dynamodb.Table(current.settings.heartbeat_table)
        return _Start(stages.Timings(transition, self.clock), table, row, self.wall)

    def _steps(self) -> Steps:
        return Steps(self.parameter, self.ssm, self.secrets, self.host, self.engine, self.held, self.clock, self.sleep)

    def _enter_the_warm_pool(self, current: release.Current, deadline: float, start: _Start) -> None:
        steps = self._steps()
        steps.pull(current, deadline)
        start.reach(Stage.RELEASE_CHECK)
        steps.check(current)
        steps.left(deadline)
        start.reach(Stage.HOOK)

    def _enter_service(self, current: release.Current, started: float, start: _Start) -> None:
        steps = self._steps()
        measured = current.measured_start_seconds
        # A promotion's deadline. The launch may be one until the host says the disk has no image.
        deadline = None if measured is None else started + PROMOTION_DEADLINE_STARTS * measured
        try:
            warm = self.host.holds(current.release.engine_image, timeout=steps.left(deadline))
        except HostError:
            raise Abandon from None
        if not warm:
            # Straight into service: this start is not the measured one, and has no deadline here.
            deadline = None
            start.timings.transition = Transition.COLD_LAUNCH
            steps.pull(current, deadline)
        steps.serve(current, deadline, start.reach)
        start.reach(Stage.HOOK)
        try:
            self.served.parent.mkdir(parents=True, exist_ok=True)
            self.served.touch()
        except OSError:
            # An instance that could not say it has served would be taken for a new one on its way
            # back to the warm pool.
            raise Abandon from None
