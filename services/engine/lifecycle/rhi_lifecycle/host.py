"""The host, as the lifecycle agent acts on it: its docker, its systemd and its console, through
nsenter.

The agent runs in a container, and the engine is a systemd unit of the host (system design,
section 4.3). So every action on the host runs the host's own `docker`, `systemctl` or `tee` in the
host's namespaces, with `nsenter` on the host's first process: the agent's container is privileged
and shares the host's process namespace for that. Every other module of the agent acts on the host
through this one, so a test replaces one module.

The engine image is in the account's ECR, and the host's docker holds no credentials for it. So a
pull first asks ECR for an authorization token, with the instance role, and logs the host's docker
in to the image's registry. The token's password reaches docker on its standard input, never in a
command's arguments, where any process of the host could read it.

The host's console is the instance's serial console, which EC2 keeps and gives as the instance's
console output. A line is written there on the host's own console device, and reaches the command
on its standard input: reading it back needs no permission of the instance role.

The host also keeps the record of a launch under way: an empty file, `LAUNCH_RECORD`, that is
there from the start of a launch until Auto Scaling has taken the completion that lets the instance
into service. The agent's program reads it when it starts, to tell a launch that was interrupted
from an instance that serves (`rhi_lifecycle.__main__`). It is under `/run`, which is empty at
every start of the instance, beside the settings the boot program writes there.

A command that fails raises `HostError`, which names the action and the exit status and repeats
nothing the command or AWS printed. So does a command that is still running when the time its
caller gave it has passed: it is stopped, because the run of a canary has a deadline
(`rhi_lifecycle.canary`). Two answers are read, compared and never repeated: the state systemd
gives the engine's unit, a word of systemd's own vocabulary, and the names of the files beside the
record.
"""

from __future__ import annotations

import base64
import binascii
import re
import subprocess
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ENGINE_UNIT = "rhi-engine"

# The host's console device: the kernel writes it to the serial console of an instance.
CONSOLE = "/dev/console"

# The record of a launch under way: an empty file of this name in this folder of the host, where
# the boot program writes the release's settings.
RUN_FOLDER = "/run/rhi"
LAUNCH_RECORD = "launching"

# The host's first process, and the namespaces of it a command enters.
HOST_NAMESPACES = ("--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid")

# The registry of an image reference: what comes before the first slash.
_REGISTRY = re.compile(r"([A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?)/\S+", re.ASCII)


class HostError(Exception):
    """An action on the host failed. `action` is `login`, `pull`, `inspect`, `start`, `restart`,
    `stop`, `state`, `record` or `console`; `returncode` is the command's exit status, or None when no
    command ran to its end."""

    def __init__(self, action: str, returncode: int | None = None) -> None:
        how = "could not be run" if returncode is None else f"exited with status {returncode}"
        super().__init__(f"{action}: the command {how}")
        self.action = action
        self.returncode = returncode


class Host:
    """The host's docker, systemd and console. `ecr` is an ECR client of the AWS SDK; without one, the
    instance role and the region of the environment make it at the first pull. `nsenter` is the
    program that enters the host's namespaces."""

    def __init__(self, ecr: Any = None, *, nsenter: str = "nsenter") -> None:
        self._ecr = ecr
        self._nsenter = nsenter

    def holds(self, image: str, timeout: float | None = None) -> bool:
        """Whether the host's docker holds `image`: a launch pulls it only when the disk lacks it.
        A docker that has not answered within `timeout` seconds, when one is given, has not said
        no: `HostError`."""
        end = None if timeout is None else time.monotonic() + timeout
        try:
            self._run("inspect", ("docker", "image", "inspect", image), end=end, quiet=True)
        except HostError as error:
            if error.returncode is None:
                raise
            return False
        return True

    def pull(self, image: str, timeout: float | None = None) -> None:
        """Log the host's docker in to the registry of `image`, then pull `image`, the two within
        `timeout` seconds when one is given."""
        end = None if timeout is None else time.monotonic() + timeout
        found = _REGISTRY.fullmatch(image)
        if found is None:
            raise HostError("login")
        registry = found.group(1)
        user, password = self._credentials()
        self._run("login", ("docker", "login", "--username", user, "--password-stdin", registry), password, end=end)
        self._run("pull", ("docker", "pull", image), end=end)

    def start_engine(self, timeout: float | None = None) -> None:
        end = None if timeout is None else time.monotonic() + timeout
        self._run("start", ("systemctl", "start", ENGINE_UNIT), end=end)

    def restart_engine(self) -> None:
        self._run("restart", ("systemctl", "restart", ENGINE_UNIT))

    def engine_started(self) -> bool:
        """Whether the engine's unit has been started on the host and not stopped since: systemd
        gives it any state but `inactive`. A unit that is starting, or that systemd is about to
        start again after a failure, counts: only the agent starts the unit
        (`services/engine/units/rhi-engine.service`), and only the agent stops it. A systemd that
        gave no state has not said no: `HostError`."""
        # The state is systemd's one word on the standard output. The exit status tells only a
        # unit that is active from every other.
        returncode, state = self._read("state", ("systemctl", "is-active", ENGINE_UNIT))
        if not state:
            raise HostError("state", returncode)
        return state != "inactive"

    def begin_launch(self) -> None:
        """Put on record that a launch is under way on the host."""
        self._run("record", ("touch", f"{RUN_FOLDER}/{LAUNCH_RECORD}"))

    def end_launch(self) -> None:
        """Remove the record of the launch: Auto Scaling has let the instance into service."""
        self._run("record", ("rm", "-f", f"{RUN_FOLDER}/{LAUNCH_RECORD}"))

    def launch_begun(self) -> bool:
        """Whether a launch is on record on the host: one was begun, and has not ended with the
        instance in service. A host that did not list its folder has not said no: `HostError`."""
        # The names in the folder, one on each line. The exit status of a command that looks for
        # one file would not tell a file that is absent from a host that could not be entered.
        returncode, names = self._read("record", ("ls", RUN_FOLDER))
        if returncode != 0:
            raise HostError("record", returncode)
        return LAUNCH_RECORD in names.splitlines()

    def stop_engine(self) -> None:
        self._run("stop", ("systemctl", "stop", ENGINE_UNIT))

    def write_console(self, line: str, timeout: float | None = None) -> None:
        """Write `line`, as one line, on the host's console, within `timeout` seconds when one is
        given."""
        end = None if timeout is None else time.monotonic() + timeout
        # `tee` repeats on its standard output what it writes: a quiet command, so that the line is
        # not in the agent's own log as well.
        self._run("console", ("tee", CONSOLE), line + "\n", end=end, quiet=True)

    def _credentials(self) -> tuple[str, str]:
        """The user and the password of an authorization token from ECR."""
        try:
            if self._ecr is None:
                self._ecr = boto3.client("ecr")
            token = self._ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
            # The token is `<user>:<password>` in base64.
            user, colon, password = base64.b64decode(token, validate=True).decode("utf-8").partition(":")
        except (BotoCoreError, ClientError, LookupError, TypeError, binascii.Error, UnicodeDecodeError):
            raise HostError("login") from None
        if not (user and colon and password):
            raise HostError("login")
        return user, password

    def _read(self, action: str, command: tuple[str, ...]) -> tuple[int, str]:
        """Run `command` in the host's namespaces, and give its exit status and what it wrote on
        its standard output."""
        arguments = (self._nsenter, *HOST_NAMESPACES, "--", *command)
        try:
            done = subprocess.run(
                arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False
            )
        except OSError:
            raise HostError(action) from None
        return done.returncode, done.stdout.decode("ascii", errors="replace").strip()

    def _run(
        self,
        action: str,
        command: tuple[str, ...],
        given: str | None = None,
        *,
        end: float | None = None,
        quiet: bool = False,
    ) -> None:
        """Run `command` in the host's namespaces, with `given` on its standard input. `end` is
        the moment, on the monotonic clock, at which a command still running is stopped. A `quiet`
        command prints nothing: only its exit status is the answer."""
        arguments = (self._nsenter, *HOST_NAMESPACES, "--", *command)
        timeout = None if end is None else max(end - time.monotonic(), 0)
        output = subprocess.DEVNULL if quiet else None
        try:
            if given is None:
                done = subprocess.run(
                    arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=output, timeout=timeout, check=False
                )
            else:
                done = subprocess.run(
                    arguments, input=given.encode("utf-8"), stdout=output, stderr=output, timeout=timeout, check=False
                )
        except (OSError, subprocess.TimeoutExpired):
            raise HostError(action) from None
        if done.returncode != 0:
            raise HostError(action, done.returncode)
