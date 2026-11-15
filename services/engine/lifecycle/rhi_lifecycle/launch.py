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

An instance that returns from service to the warm pool has a warm-pool target state too. It is
draining, not entering, and is not handled here: `Launch.run` does nothing for an instance that
has served since its start. It knows one by a file it writes before it completes a hook into
service, in a folder the host empties at every start (`SERVED_FILE`).

**Abandon.** A release that differs, a step that fails and the transition's deadline all end the
same way. The steps on the host take what is left of the deadline as their time limit, so a pull
that hangs is abandoned at the deadline and not when it returns.

**What cannot be completed.** The hook is named in the settings of the release parameter, and the
instance by its metadata. Without one of them there is nothing to call: `run` returns
`NOT_COMPLETED`, and the hook's own timeout abandons the instance.

Every action on the host goes through `rhi_lifecycle.host`. Nothing here repeats what AWS, docker
or the engine answered.
"""

from __future__ import annotations

import enum
import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from rhi_lifecycle import release
from rhi_lifecycle.host import Host, HostError

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

# The instance metadata service, and the engine on its own instance (section 4.1).
METADATA_URL = "http://169.254.169.254/latest"
ENGINE_URL = "http://127.0.0.1:8000"

# A request, and the seconds its answer may take (None: no limit), give the answer's status and
# body, or None when nothing answered.
Fetch = Callable[[urllib.request.Request, float | None], tuple[int, bytes] | None]

# Neither the metadata service nor the engine is behind a proxy, whatever the environment says.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


class Metadata:
    """The instance metadata, read with a session token (IMDSv2 is required; section 4.3)."""

    def __init__(self, fetch: Fetch = fetch) -> None:
        self._fetch = fetch

    def target_state(self) -> str:
        """The lifecycle state Auto Scaling is moving the instance to."""
        return self._item("autoscaling/target-lifecycle-state")

    def instance_id(self) -> str:
        return self._item("instance-id")

    def _item(self, item: str) -> str:
        token = self._text(
            urllib.request.Request(
                f"{METADATA_URL}/api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}
            ),
            "the session token",
        )
        return self._text(
            urllib.request.Request(f"{METADATA_URL}/meta-data/{item}", headers={"X-aws-ec2-metadata-token": token}),
            item,
        )

    def _text(self, request: urllib.request.Request, item: str) -> str:
        answer = self._fetch(request, ANSWER_SECONDS)
        if answer is not None and answer[0] == 200:
            try:
                text = answer[1].decode("ascii").strip()
            except UnicodeDecodeError:
                text = ""
            if text:
                return text
        raise MetadataError(item)


class Engine:
    """The engine of this instance, as the launch asks it: its health route and one inference."""

    def __init__(self, fetch: Fetch = fetch) -> None:
        self._fetch = fetch

    def healthy(self) -> bool:
        """Whether `GET /health` answers 200: the model can serve (section 4.1)."""
        answer = self._fetch(urllib.request.Request(f"{ENGINE_URL}/health"), ANSWER_SECONDS)
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
    """How `Launch.run` ended."""

    # The hook was completed with `CONTINUE`.
    CONTINUED = "continued"
    # The hook was completed with `ABANDON`.
    ABANDONED = "abandoned"
    # The instance is in no launch transition, or is draining back to the warm pool: nothing was done.
    NOT_A_LAUNCH = "not-a-launch"
    # No completion reached Auto Scaling: the transition, the instance or the hook is unknown, or
    # the call failed. The hook's own timeout abandons the instance.
    NOT_COMPLETED = "not-completed"


class _Abandon(Exception):
    """A step did not pass, or the deadline did: the hook is completed with `ABANDON`."""


@dataclass(frozen=True)
class Launch:
    """One instance's launch. `parameter` is the name of its pool's release parameter;
    `autoscaling`, `ssm` and `secrets` are the AWS SDK's clients for Auto Scaling, Systems Manager
    and Secrets Manager. `held` is the record of the release on disk, `served` the file that says
    the instance has served since its start. `clock` and `sleep` are arguments so that a test
    waits for nothing."""

    parameter: str
    metadata: Metadata
    autoscaling: Any
    ssm: Any
    secrets: Any
    host: Host
    engine: Engine
    held: Path = release.HELD_RELEASE_FILE
    served: Path = SERVED_FILE
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep

    def run(self) -> Outcome:
        """Do the steps of the transition the instance is in, and complete its launch hook."""
        started = self.clock()
        try:
            target = self.metadata.target_state()
            instance = self.metadata.instance_id()
        except MetadataError:
            return Outcome.NOT_COMPLETED
        into_service = target == IN_SERVICE
        if not into_service and (target not in WARM_STATES or self.served.exists()):
            return Outcome.NOT_A_LAUNCH
        current = release.read_current(self.ssm, self.parameter)
        if current is None:
            return Outcome.NOT_COMPLETED
        try:
            if into_service:
                self._enter_service(current, started)
            else:
                self._enter_the_warm_pool(current, started + ENTRY_DEADLINE_SECONDS)
        except _Abandon:
            result = ABANDON
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
        return Outcome.CONTINUED if result == CONTINUE else Outcome.ABANDONED

    def _enter_the_warm_pool(self, current: release.Current, deadline: float) -> None:
        self._pull(current, deadline)
        self._check(current)
        self._left(deadline)

    def _enter_service(self, current: release.Current, started: float) -> None:
        try:
            warm = self.host.holds(current.release.engine_image)
        except HostError:
            raise _Abandon from None
        measured = current.measured_start_seconds
        deadline = None
        if warm and measured is not None:
            deadline = started + PROMOTION_DEADLINE_STARTS * measured
        if not warm:
            self._pull(current, deadline)
        self._check(current)
        secret = self._secret(current.settings.secret_id)
        try:
            self.host.start_engine(timeout=self._left(deadline))
        except HostError:
            raise _Abandon from None
        while not self.engine.healthy():
            left = self._left(deadline)
            self.sleep(HEALTH_INTERVAL_SECONDS if left is None else min(HEALTH_INTERVAL_SECONDS, left))
        # The pool's name is the model name the engine serves (section 4.2).
        if not self.engine.warm_up(current.settings.pool, secret, timeout=self._left(deadline)):
            raise _Abandon
        self._left(deadline)
        try:
            self.served.parent.mkdir(parents=True, exist_ok=True)
            self.served.touch()
        except OSError:
            # An instance that could not say it has served would be taken for a new one on its way
            # back to the warm pool.
            raise _Abandon from None

    def _left(self, deadline: float | None) -> float | None:
        """The seconds left before `deadline`, or None when there is no deadline. At the deadline
        the transition is abandoned."""
        if deadline is None:
            return None
        left = deadline - self.clock()
        if left <= 0:
            raise _Abandon
        return left

    def _pull(self, current: release.Current, deadline: float | None) -> None:
        try:
            self.host.pull(current.release.engine_image, timeout=self._left(deadline))
        except HostError:
            raise _Abandon from None

    def _check(self, current: release.Current) -> None:
        """Abandon unless the disk holds the pool's current release, which is still the one whose
        image this launch pulled or found."""
        verdict = release.check(self.ssm, self.parameter, self.held)
        if not verdict.equal or verdict.held != current.release:
            raise _Abandon

    def _secret(self, secret_id: str) -> str:
        """The pool's bearer secret, which the engine's inference routes require (section 4.1)."""
        try:
            secret = self.secrets.get_secret_value(SecretId=secret_id)["SecretString"]
        except (BotoCoreError, ClientError, KeyError, TypeError):
            raise _Abandon from None
        if not isinstance(secret, str) or not secret:
            raise _Abandon
        return secret
