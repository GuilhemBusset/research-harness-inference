"""The host, as the lifecycle agent acts on it: its docker and its systemd, through nsenter.

The agent runs in a container, and the engine is a systemd unit of the host (system design,
section 4.3). So every action on the host runs the host's own `docker` or `systemctl` in the
host's namespaces, with `nsenter` on the host's first process: the agent's container is privileged
and shares the host's process namespace for that. Every other module of the agent acts on the host
through this one, so a test replaces one module.

The engine image is in the account's ECR, and the host's docker holds no credentials for it. So a
pull first asks ECR for an authorization token, with the instance role, and logs the host's docker
in to the image's registry. The token's password reaches docker on its standard input, never in a
command's arguments, where any process of the host could read it.

A command that fails raises `HostError`, which names the action and the exit status and repeats
nothing the command or AWS printed. So does a command that is still running when the time its
caller gave it has passed: it is stopped, because a transition has a deadline (section 4.3).
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

# The host's first process, and the namespaces of it a command enters.
HOST_NAMESPACES = ("--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid")

# The registry of an image reference: what comes before the first slash.
_REGISTRY = re.compile(r"([A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?)/\S+", re.ASCII)


class HostError(Exception):
    """An action on the host failed. `action` is `login`, `pull`, `inspect`, `start`, `restart` or
    `stop`; `returncode` is the command's exit status, or None when no command ran to its end."""

    def __init__(self, action: str, returncode: int | None = None) -> None:
        how = "could not be run" if returncode is None else f"exited with status {returncode}"
        super().__init__(f"{action}: the command {how}")
        self.action = action
        self.returncode = returncode


class Host:
    """The host's docker and systemd. `ecr` is an ECR client of the AWS SDK; without one, the
    instance role and the region of the environment make it at the first pull. `nsenter` is the
    program that enters the host's namespaces."""

    def __init__(self, ecr: Any = None, *, nsenter: str = "nsenter") -> None:
        self._ecr = ecr
        self._nsenter = nsenter

    def holds(self, image: str) -> bool:
        """Whether the host's docker holds `image`: an instance that went through the warm pool
        pulled it there, and one launched straight into service has not."""
        try:
            self._run("inspect", ("docker", "image", "inspect", image), quiet=True)
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

    def stop_engine(self) -> None:
        self._run("stop", ("systemctl", "stop", ENGINE_UNIT))

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
