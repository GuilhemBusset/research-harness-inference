"""The launch of an instance, completed or abandoned (system design, section 4.3).

Without an answer a lifecycle hook waits, and a GPU bills while it does. So when an instance
starts, the agent does the steps of a launch and completes the launch hook: with `CONTINUE` when
every step passed, with `ABANDON` when one failed, and Auto Scaling then replaces the instance.

**The launch.** An instance of a group has one way into service, and the agent knows it by the
target lifecycle state in the instance metadata, `InService`. Its steps, in order:

1. pull the engine image, if the host's disk lacks it;
2. check the release: the disk holds the pool's current one;
3. start the engine;
4. wait for the engine's health route;
5. send one warm-up request, a real one;
6. complete the launch hook.

Any other target state is no launch: `run` does nothing and returns `NOT_A_LAUNCH`.

**Abandon.** A release that differs, and a step that fails (a pull, the engine's start, a warm-up
request the engine refuses or drops), end the same way: the hook is completed with `ABANDON`.

**No deadline of the agent's.** The launch hook's own timeout bounds a launch: 15 minutes, or twice
the pool's measured start when that is longer (`rhi.pools.hooks`). The agent keeps no time of its
own beside it, and gives its steps no time limit. So an engine whose health route never answers
200, or a warm-up request that stays unanswered, is waited for: the agent completes nothing, the
hook's timeout passes, and Auto Scaling abandons the instance as it does when the agent is gone.

**What cannot be completed.** The hook is named in the settings of the release parameter, and the
instance by its metadata. Without one of them there is nothing to call: `run` returns
`NOT_COMPLETED`, and the hook's own timeout abandons the instance.

**A launch under way is on record.** The agent's unit starts the agent again when it has failed,
and the agent that starts then finds the engine's unit started: by a launch that was interrupted,
or by one that let the instance into service. So a launch puts itself on record on the host before
its first step (`Host.begin_launch`), and removes the record once Auto Scaling has taken the
completion with `CONTINUE` (`Host.end_launch`), not before: an instance whose launch hook still
waits is never taken for one that serves. A launch that is abandoned stays on record, and so does
one whose completion did not reach Auto Scaling. A launch that cannot be put on record is
abandoned before the engine starts. A record that cannot be removed is logged, and the instance
serves: Auto Scaling has let it in. So a record that is there says a launch may not have ended in
service, never that it did not: an agent that starts and finds one drains the instance when its
group terminates it, as it drains one that served (`rhi_lifecycle.__main__`).

**Its last step is on record too.** A record that stayed cannot tell a launch that was interrupted
from one that Auto Scaling let into service. So once the engine has answered the warm-up request,
and before the completion is sent, the launch puts on record that its steps have all passed
(`Host.pass_launch`); one that cannot is abandoned. `CONTINUE` is therefore sent only for a launch
with that record, and the record is removed with the launch's own.

**An interrupted launch is run again.** The agent that finds a launch on record, and no record
that its steps passed, runs the launch again (`rhi_lifecycle.__main__`): its hook still waits. Every
step can be repeated: the image is on disk, the release is checked again, a unit that is started
stays so, and the engine answers one more warm-up request. The hook's timeout is counted from the
instance's start, not from the second run.

**A launch whose steps passed is resumed, not run again** (`Launch.resume`). Only the answer to
its completion is missing, so the agent checks the release once more and sends the completion
again: with `CONTINUE`, or with `ABANDON` when the release differs by now, as at any launch. It
does nothing else: no other step is repeated, and no row of a launch's stage is written over the
row of an instance that serves. The instance is let into service only when Auto Scaling takes
`CONTINUE`: the records are removed then, and the launch is counted among the launches completed,
with the seconds since the kernel's start.

**A completion Auto Scaling refuses lets no instance in.** Auto Scaling answers a resumed launch
that no lifecycle action waits for the instance under the names the settings give (`NO_ACTION`)
in three cases, and the answer does not say which: the completion sent before was taken, and the
instance serves; the hook's timeout has passed, and the group terminates the instance; or the
settings misname the hook or the group, and the hook still waits. The instance role may complete a
lifecycle action and may not ask Auto Scaling for an instance's state (`rhi.pools.role`), so
nothing on the instance tells the three apart. `resume` therefore returns `REFUSED`: the instance
is not taken as in service, the launch stays on record, and the completion is not sent again,
because the answer would be the same. What that costs: an instance that a completion let in, and
whose agent stopped before it removed the records or could not remove them, writes no serving
heartbeat once its agent has restarted. It is drained when its group terminates it, like any
instance that may have served. Any other failure of the call says nothing either: the launch stays
on record, and the completion is sent again later.

**The stages.** A launch marks where each stage of the start begins and ends
(`rhi_lifecycle.stages`, which says what each stage covers), so the agent's metrics give the
seconds each took. At the start of each stage the launch also rewrites the instance's row in the
heartbeats table, with the stage it has reached and the release it is starting. Nothing runs on the
instance yet, so the row's counts are zero. An abandoned launch does not reach the hook stage: its
row keeps the stage it was abandoned in. The boot is over when the agent starts, so no row names
it. A row that cannot be written changes nothing else: the launch goes on. The time a write takes
is its stage's.

**What the engine was started with.** The release parameter says what a pool should run, and it
can change while an engine runs. What an engine does run with is on the host alone: the arguments
of its container, as docker holds them (`Host.engine_container`). `started_engine` gives the
container that runs, by its ID, with the public fingerprint of those arguments
(`rhi_common.launch_settings`), which is `UNAVAILABLE` when they are not ones the contract knows.
`settings_fingerprint` is that fingerprint alone, and `UNAVAILABLE` too when the host gives no
container. Neither reads a release: the parameter and the record on disk cannot change what they
say of a running engine. Neither says that the engine serves: a container runs while its engine
still loads (`rhi_lifecycle.serving` says when a fingerprint is reported).

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
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from rhi_common import launch_settings
from rhi_common.records import InstanceHeartbeat
from rhi_lifecycle import release, stages
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.stages import Stage

# The wait between two questions to the engine's health route.
HEALTH_INTERVAL_SECONDS = 2.0
# How long one answer of the instance metadata, or of the health route, may take.
ANSWER_SECONDS = 5.0

# The results of a lifecycle action.
CONTINUE = "CONTINUE"
ABANDON = "ABANDON"

# The target lifecycle state of a launch. The others (`Terminated`, ...) are not launches.
IN_SERVICE = "InService"

# The error code of Auto Scaling's answer to the completion of a lifecycle action that does not
# wait under the names given: the hook was completed before, its timeout has passed, or the names
# are not the hook's and the group's.
NO_ACTION = "ValidationError"

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

    def tag(self, key: str) -> str:
        """The value of the instance's tag `key`: the launch template lets the metadata give the
        instance's tags. One the instance does not carry raises `NoSuchItemError`."""
        return self._item(f"tags/instance/{key}")

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

    # The launch hook was completed with `CONTINUE`: Auto Scaling took the completion.
    CONTINUED = "continued"
    # The launch hook was completed with `ABANDON`.
    ABANDONED = "abandoned"
    # The engine was idle, and the termination hook was completed: the instance may leave service.
    DRAINED = "drained"
    # The instance is not being launched: nothing was done.
    NOT_A_LAUNCH = "not-a-launch"
    # The instance is not being terminated: nothing was done.
    NOT_A_TERMINATION = "not-a-termination"
    # No completion reached Auto Scaling: the target state, the instance or the hook is unknown, or
    # the call failed. The hook's own timeout ends the wait.
    NOT_COMPLETED = "not-completed"
    # Auto Scaling refused the completion of a launch resumed: no lifecycle action waits for the
    # instance under the names the settings give. Whether the instance serves is not known.
    REFUSED = "refused"


class Abandon(Exception):
    """A step did not pass, or its caller's deadline did: a launch completes its hook with
    `ABANDON`."""


def identity(served: release.Release) -> tuple[str, str]:
    """The image digest and the model revision a heartbeat's row carries for the release `served`."""
    # The digest is what follows the repository in the image's reference.
    digest = served.engine_image.rpartition("@")[2]
    return digest, NO_MODEL if served.model_revision is None else served.model_revision


@dataclass(frozen=True)
class Started:
    """An engine's container that runs: the ID docker gave it, which is that start's alone, and
    the fingerprint of the settings it was started with, `launch_settings.UNAVAILABLE` when its
    flags are not ones the contract knows."""

    container: str
    fingerprint: str


def started_engine(host: Host, timeout: float | None = None) -> Started | None:
    """The engine's container that runs on `host` now, or None: the host cannot be asked or has
    not answered within `timeout` seconds, no engine's container runs there, or its unit recorded
    no flags."""
    try:
        container = host.engine_container(timeout)
    except HostError:
        return None
    return None if container is None else Started(container.id, launch_settings.fingerprint(container.arguments))


def settings_fingerprint(host: Host, timeout: float | None = None) -> str:
    """The fingerprint of the settings the engine that runs on `host` was started with, or
    `launch_settings.UNAVAILABLE`: the host gives no container of the engine, or flags that are
    not ones the contract knows."""
    started = started_engine(host, timeout)
    return launch_settings.UNAVAILABLE if started is None else started.fingerprint


@dataclass(frozen=True)
class Steps:
    """The steps of a start, as a launch and the run of a canary do them (`rhi_lifecycle.canary`):
    the two share them, so they cannot differ. `parameter`, `ssm`, `secrets`, `host`, `engine`,
    `held`, `clock` and `sleep` are `Launch`'s. A step takes what is left before `deadline` as its
    time limit, and raises `Abandon` when it does not pass or the deadline has. The deadline is the
    canary's: a launch gives None, no limit, and its hook's timeout is the bound."""

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
    and Secrets Manager, and `dynamodb` is boto3's DynamoDB resource. `held` is the record of the
    release on disk. `clock` and `sleep` are arguments so that a test waits for nothing. `wall`
    gives the seconds since the epoch, for the heartbeat's row, and `uptime` the seconds since the
    kernel started, which are the boot stage."""

    parameter: str
    metadata: Metadata
    autoscaling: Any
    ssm: Any
    secrets: Any
    dynamodb: Any
    host: Host
    engine: Engine
    held: Path = release.HELD_RELEASE_FILE
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep
    wall: Callable[[], float] = time.time
    uptime: Callable[[], float | None] = stages.uptime

    def run(self) -> Outcome:
        """Do the steps of a launch, and complete the launch hook."""
        started = self.clock()
        # The boot ends where the launch starts: both are read now, before anything takes time.
        booted = self.uptime()
        launching = self._launching()
        if isinstance(launching, Outcome):
            return launching
        current, instance = launching
        start = self._start(current, instance)
        if booted is not None:
            start.timings.start(Stage.BOOT, at=started - booted)
        start.reach(Stage.IMAGE, at=started)
        try:
            self._steps(current, start)
        except Abandon:
            result = ABANDON
            # The stage the launch was in ends here, and the instance's row keeps naming it.
            start.timings.end()
        else:
            result = CONTINUE
        try:
            self._complete(current.settings, instance, result)
        except (BotoCoreError, ClientError):
            return Outcome.NOT_COMPLETED
        finally:
            # The hook stage of a launch that passed. An abandoned launch is in no stage by now.
            start.timings.end()
        if result != CONTINUE:
            return Outcome.ABANDONED
        # Auto Scaling took the completion: the launch is one completed, and no longer under way.
        start.timings.completed()
        self._off_record()
        return Outcome.CONTINUED

    def resume(self) -> Outcome:
        """Send again the completion of a launch whose steps have all passed, as the host has on
        record: `CONTINUE` when the disk still holds the pool's current release, `ABANDON` when it
        does not. Nothing else of a launch is done. `CONTINUED` only when Auto Scaling takes
        `CONTINUE`; `REFUSED` when it answers that no lifecycle action waits."""
        launching = self._launching()
        if isinstance(launching, Outcome):
            return launching
        current, instance = launching
        try:
            self._steps_of().check(current)
        except Abandon:
            result = ABANDON
        else:
            result = CONTINUE
        try:
            self._complete(current.settings, instance, result)
        except ClientError as refusal:
            if refusal.response.get("Error", {}).get("Code") == NO_ACTION:
                return Outcome.REFUSED
            return Outcome.NOT_COMPLETED
        except BotoCoreError:
            return Outcome.NOT_COMPLETED
        if result != CONTINUE:
            return Outcome.ABANDONED
        # The stages of this launch were timed by the agent that ran it. The launch itself ends
        # here, this long after the kernel's start: a host that does not say how long counts none.
        launched = self.uptime()
        if launched is not None:
            stages.completed(launched)
        self._off_record()
        return Outcome.CONTINUED

    def _launching(self) -> tuple[release.Current, str] | Outcome:
        """The pool's current release, whose settings name the launch hook, and the instance; or
        how the call ends when the instance is not being launched or one of them is unknown."""
        try:
            target = self.metadata.target_state()
            instance = self.metadata.instance_id()
        except MetadataError:
            return Outcome.NOT_COMPLETED
        if target != IN_SERVICE:
            return Outcome.NOT_A_LAUNCH
        current = release.read_current(self.ssm, self.parameter)
        if current is None:
            return Outcome.NOT_COMPLETED
        return current, instance

    def _complete(self, settings: release.Settings, instance: str, result: str) -> None:
        self.autoscaling.complete_lifecycle_action(
            AutoScalingGroupName=settings.group,
            LifecycleHookName=settings.launch_hook,
            InstanceId=instance,
            LifecycleActionResult=result,
        )

    def _off_record(self) -> None:
        """Remove the launch's records from the host: Auto Scaling has let the instance in."""
        try:
            self.host.end_launch()
        except HostError as error:
            _LOG.error("the record of the launch was not removed (%s)", error)

    def _start(self, current: release.Current, instance: str) -> _Start:
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
        return _Start(stages.Timings(self.clock), table, row, self.wall)

    def _steps_of(self) -> Steps:
        return Steps(self.parameter, self.ssm, self.secrets, self.host, self.engine, self.held, self.clock, self.sleep)

    def _steps(self, current: release.Current, start: _Start) -> None:
        """The steps before the hook's completion, none with a time limit."""
        steps = self._steps_of()
        try:
            # On record before the engine's unit can be started: an agent that starts and finds the
            # unit started knows by the record that the launch hook still waits.
            self.host.begin_launch()
            on_disk = self.host.holds(current.release.engine_image)
        except HostError:
            raise Abandon from None
        if not on_disk:
            steps.pull(current, None)
        steps.serve(current, None, start.reach)
        try:
            # On record before the completion is sent: an agent that starts and finds this record
            # sends the completion again, and repeats no step.
            self.host.pass_launch()
        except HostError:
            raise Abandon from None
        start.reach(Stage.HOOK)
