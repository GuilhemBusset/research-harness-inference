"""The host's commands (`rhi_lifecycle.host`; system design, section 4.3), with a program that
stands in for nsenter and an object that stands in for the ECR client. The stand-in program
records each call's arguments and standard input and runs nothing: no test here needs docker,
systemd, a privileged process or AWS."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

from rhi_lifecycle.host import Host, HostError

if TYPE_CHECKING:
    from pathlib import Path

REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
IMAGE = f"{REGISTRY}/rhi/engine@sha256:{'a' * 64}"
PASSWORD = "the-password-of-the-token"
TOKEN = base64.b64encode(f"AWS:{PASSWORD}".encode()).decode()

# Every command runs behind these: nsenter on the host's first process, into its namespaces.
IN_THE_HOST = ["--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--"]

# One file per call, numbered: the arguments, one per line, then the standard input. The call
# exits with status 3 when one of its arguments is the word in the file `fail`, and does not end
# by itself when one is the word in the file `hang`.
STAND_IN = """\
#!/bin/sh
here=$(dirname "$0")
call=$(ls "$here/arguments" | wc -l)
printf '%s\\n' "$@" > "$here/arguments/$call"
cat > "$here/input/$call"
for argument in "$@"; do
    if [ -f "$here/fail" ] && [ "$argument" = "$(cat "$here/fail")" ]; then exit 3; fi
    if [ -f "$here/hang" ] && [ "$argument" = "$(cat "$here/hang")" ]; then exec sleep 60; fi
done
"""


class Ecr:
    """An ECR client, as far as the module uses one: an authorization token, or a refusal."""

    def __init__(self, token: str | None = TOKEN) -> None:
        self.token = token

    def get_authorization_token(self) -> dict[str, Any]:
        if self.token is None:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetAuthorizationToken")
        return {"authorizationData": [{"authorizationToken": self.token, "proxyEndpoint": f"https://{REGISTRY}"}]}


class Nsenter:
    """The stand-in program, and what its calls recorded."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.program = folder / "nsenter"
        self.program.write_text(STAND_IN)
        self.program.chmod(0o755)
        (folder / "arguments").mkdir()
        (folder / "input").mkdir()

    def fail_on(self, word: str) -> None:
        (self.folder / "fail").write_text(word)

    def hang_on(self, word: str) -> None:
        (self.folder / "hang").write_text(word)

    def calls(self) -> list[list[str]]:
        files = sorted((self.folder / "arguments").iterdir(), key=lambda file: int(file.name))
        return [file.read_text().splitlines() for file in files]

    def input(self, call: int) -> str:
        return (self.folder / "input" / str(call)).read_text()


@pytest.fixture
def nsenter(tmp_path: Path) -> Nsenter:
    return Nsenter(tmp_path)


def host(nsenter: Nsenter, ecr: Ecr | None = None) -> Host:
    return Host(ecr or Ecr(), nsenter=str(nsenter.program))


ENGINE_ACTIONS = [("start_engine", "start"), ("restart_engine", "restart"), ("stop_engine", "stop")]


@pytest.mark.parametrize(("method", "verb"), ENGINE_ACTIONS)
def test_an_engine_action_runs_the_hosts_systemctl_on_the_engines_unit(
    method: str, verb: str, nsenter: Nsenter
) -> None:
    getattr(host(nsenter), method)()

    assert nsenter.calls() == [[*IN_THE_HOST, "systemctl", verb, "rhi-engine"]]


def test_a_pull_logs_in_to_the_images_registry_and_then_pulls(nsenter: Nsenter) -> None:
    host(nsenter).pull(IMAGE)

    assert nsenter.calls() == [
        [*IN_THE_HOST, "docker", "login", "--username", "AWS", "--password-stdin", REGISTRY],
        [*IN_THE_HOST, "docker", "pull", IMAGE],
    ]
    # The token reaches docker on the login's standard input, and no command's arguments hold it.
    assert nsenter.input(0) == PASSWORD
    assert nsenter.input(1) == ""
    assert not [word for call in nsenter.calls() for word in call if PASSWORD in word or TOKEN in word]


@pytest.mark.parametrize(("method", "verb"), ENGINE_ACTIONS)
def test_an_engine_action_that_fails_is_reported(method: str, verb: str, nsenter: Nsenter) -> None:
    nsenter.fail_on(verb)

    with pytest.raises(HostError) as raised:
        getattr(host(nsenter), method)()

    assert (raised.value.action, raised.value.returncode) == (verb, 3)


def test_a_pull_that_fails_is_reported(nsenter: Nsenter) -> None:
    nsenter.fail_on("pull")

    with pytest.raises(HostError) as raised:
        host(nsenter).pull(IMAGE)

    assert (raised.value.action, raised.value.returncode) == ("pull", 3)


def test_a_login_that_fails_is_reported_and_nothing_is_pulled(nsenter: Nsenter) -> None:
    nsenter.fail_on("login")

    with pytest.raises(HostError) as raised:
        host(nsenter).pull(IMAGE)

    assert (raised.value.action, raised.value.returncode) == ("login", 3)
    assert len(nsenter.calls()) == 1
    assert PASSWORD not in str(raised.value)


@pytest.mark.parametrize("token", [None, "not base64!", base64.b64encode(b"no-colon").decode()])
def test_without_a_token_from_ecr_the_pull_fails_and_no_command_runs(token: str | None, nsenter: Nsenter) -> None:
    with pytest.raises(HostError) as raised:
        host(nsenter, Ecr(token)).pull(IMAGE)

    assert raised.value.action == "login"
    assert nsenter.calls() == []


def test_the_host_says_whether_its_docker_holds_an_image(nsenter: Nsenter) -> None:
    assert host(nsenter).holds(IMAGE)
    assert nsenter.calls() == [[*IN_THE_HOST, "docker", "image", "inspect", IMAGE]]

    # docker's own answer for an image it does not hold is an exit status that is not zero.
    nsenter.fail_on("inspect")
    assert not host(nsenter).holds(IMAGE)


@pytest.mark.parametrize(("word", "action"), [("login", "login"), ("pull", "pull")])
def test_a_pull_still_running_at_its_time_limit_is_stopped_and_reported(
    word: str, action: str, nsenter: Nsenter
) -> None:
    nsenter.hang_on(word)

    with pytest.raises(HostError) as raised:
        host(nsenter).pull(IMAGE, timeout=0.3)

    assert (raised.value.action, raised.value.returncode) == (action, None)


def test_a_start_still_running_at_its_time_limit_is_stopped_and_reported(nsenter: Nsenter) -> None:
    nsenter.hang_on("start")

    with pytest.raises(HostError) as raised:
        host(nsenter).start_engine(timeout=0.3)

    assert (raised.value.action, raised.value.returncode) == ("start", None)


def test_a_line_for_the_console_is_written_on_the_hosts_console_device(nsenter: Nsenter) -> None:
    host(nsenter).write_console("rhi-canary failed stage=image")

    # The line reaches the command on its standard input, as one line.
    assert nsenter.calls() == [[*IN_THE_HOST, "tee", "/dev/console"]]
    assert nsenter.input(0) == "rhi-canary failed stage=image\n"

    nsenter.fail_on("tee")
    with pytest.raises(HostError) as raised:
        host(nsenter).write_console("rhi-canary failed stage=image")
    assert (raised.value.action, raised.value.returncode) == ("console", 3)


def test_a_command_that_cannot_be_run_is_reported(tmp_path: Path) -> None:
    with pytest.raises(HostError) as raised:
        Host(Ecr(), nsenter=str(tmp_path / "missing")).start_engine()

    assert (raised.value.action, raised.value.returncode) == ("start", None)

    # No answer is not the answer "no": the host could not be asked whether it holds the image.
    with pytest.raises(HostError):
        Host(Ecr(), nsenter=str(tmp_path / "missing")).holds(IMAGE)
