"""The host's commands (`rhi_lifecycle.host`; system design, section 4.3), with a program that
stands in for nsenter and an object that stands in for the ECR client. The stand-in program
records each call's arguments and standard input and runs nothing: no test here needs docker,
systemd, a privileged process or AWS."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

from rhi_lifecycle.host import EngineContainer, Host, HostError

if TYPE_CHECKING:
    from pathlib import Path

REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
IMAGE = f"{REGISTRY}/rhi/engine@sha256:{'a' * 64}"
PASSWORD = "the-password-of-the-token"
TOKEN = base64.b64encode(f"AWS:{PASSWORD}".encode()).decode()

# Docker's record of an engine's container, as the engine's unit starts one: the first process is
# a shell, with its script, then the image's command, then the release's flags.
SHELL = ["-c", "the script of the first process", "rhi-engine", "engine", "serve"]
FLAGS = [f"s3://rhi-models-123456789012-us-east-1/coursework/{'c' * 40}/", "--max-num-batched-tokens", "4096"]
LABEL = "rhi-engine-flags"
CONTAINER = "d" * 64

# Every command runs behind these: nsenter on the host's first process, into its namespaces.
IN_THE_HOST = ["--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--"]

# One file per call, numbered: the arguments, one per line, then the standard input. The call
# exits with status 3 when one of its arguments is the word in the file `fail`, and does not end
# by itself when one is the word in the file `hang`. It prints what the file `say` holds.
STAND_IN = """\
#!/bin/sh
here=$(dirname "$0")
call=$(ls "$here/arguments" | wc -l)
printf '%s\\n' "$@" > "$here/arguments/$call"
cat > "$here/input/$call"
if [ -f "$here/say" ]; then cat "$here/say"; fi
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

    def say(self, line: str) -> None:
        (self.folder / "say").write_text(line + "\n")

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


@pytest.mark.parametrize(
    ("state", "started"),
    [("active", True), ("activating", True), ("deactivating", True), ("failed", True), ("inactive", False)],
)
def test_the_engines_unit_is_started_in_every_state_of_systemd_but_inactive(
    state: str, started: bool, nsenter: Nsenter
) -> None:
    nsenter.say(state)
    if state != "active":
        nsenter.fail_on("is-active")  # systemd's exit status for a unit that is not active

    assert host(nsenter).engine_started() is started
    assert nsenter.calls() == [[*IN_THE_HOST, "systemctl", "is-active", "rhi-engine"]]


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

    # No answer is not the answer "no": the host could not be asked whether it holds the image, or
    # whether the engine's unit was started.
    with pytest.raises(HostError):
        Host(Ecr(), nsenter=str(tmp_path / "missing")).holds(IMAGE)
    with pytest.raises(HostError):
        Host(Ecr(), nsenter=str(tmp_path / "missing")).engine_started()
    with pytest.raises(HostError):
        Host(Ecr(), nsenter=str(tmp_path / "missing")).launch_begun()
    with pytest.raises(HostError):
        Host(Ecr(), nsenter=str(tmp_path / "missing")).launch_passed()
    with pytest.raises(HostError):
        Host(Ecr(), nsenter=str(tmp_path / "missing")).engine_container()


def test_a_systemd_that_gives_the_engines_unit_no_state_has_not_said_that_it_is_stopped(nsenter: Nsenter) -> None:
    with pytest.raises(HostError) as raised:
        host(nsenter).engine_started()

    assert raised.value.action == "state"


def test_a_launch_is_put_on_record_in_the_hosts_run_folder_and_removed_from_it(nsenter: Nsenter) -> None:
    host(nsenter).begin_launch()
    host(nsenter).pass_launch()
    host(nsenter).end_launch()

    # The launch's own record is removed first: a removal cut short leaves no launch on record.
    assert nsenter.calls() == [
        [*IN_THE_HOST, "touch", "/run/rhi/launching"],
        [*IN_THE_HOST, "touch", "/run/rhi/launch-passed"],
        [*IN_THE_HOST, "rm", "-f", "/run/rhi/launching", "/run/rhi/launch-passed"],
    ]


@pytest.mark.parametrize(
    ("names", "begun", "passed"),
    [
        ("engine-flags\nlaunch-passed\nlaunching\nrelease.env", True, True),
        ("engine-flags\nlaunching\nrelease.env", True, False),
        ("release.env", False, False),
    ],
)
def test_a_launch_is_on_record_when_the_hosts_run_folder_lists_its_file(
    names: str, begun: bool, passed: bool, nsenter: Nsenter
) -> None:
    nsenter.say(names)

    assert host(nsenter).launch_begun() is begun
    assert host(nsenter).launch_passed() is passed
    assert nsenter.calls() == [[*IN_THE_HOST, "ls", "/run/rhi"]] * 2


def test_a_host_that_does_not_list_its_run_folder_has_not_said_that_no_launch_is_on_record(nsenter: Nsenter) -> None:
    nsenter.fail_on("ls")

    with pytest.raises(HostError) as raised:
        host(nsenter).launch_begun()

    assert (raised.value.action, raised.value.returncode) == ("record", 3)


def test_the_running_engines_flags_are_the_last_arguments_docker_holds_of_its_container(nsenter: Nsenter) -> None:
    nsenter.say(json.dumps([CONTAINER, True, {LABEL: "3", "another": "label"}, [*SHELL, *FLAGS]]))

    # The container, by the ID docker gave it, and its flags.
    assert host(nsenter).engine_container() == EngineContainer(CONTAINER, tuple(FLAGS))
    # One question to docker: the container's ID, whether it runs, its labels and its arguments.
    asked = "[{{json .Id}},{{json .State.Running}},{{json .Config.Labels}},{{json .Args}}]"
    assert nsenter.calls() == [[*IN_THE_HOST, "docker", "container", "inspect", "--format", asked, "rhi-engine"]]

    # A release with no flag, the mock pool's: the container was given none, and that is known.
    nsenter.say(json.dumps([CONTAINER, True, {LABEL: "0"}, SHELL]))
    assert host(nsenter).engine_container() == EngineContainer(CONTAINER, ())


@pytest.mark.parametrize(
    "answer",
    [
        # A container of a unit that does not label it, as the units of an older release.
        [CONTAINER, True, {}, [*SHELL, *FLAGS]],
        [CONTAINER, True, None, [*SHELL, *FLAGS]],
        # A container that does not run.
        [CONTAINER, False, {LABEL: "3"}, [*SHELL, *FLAGS]],
        # A label that is not the number of some of the container's arguments.
        [CONTAINER, True, {LABEL: "9"}, [*SHELL, *FLAGS]],
        [CONTAINER, True, {LABEL: "three"}, [*SHELL, *FLAGS]],
        [CONTAINER, True, {LABEL: 3}, [*SHELL, *FLAGS]],
        [CONTAINER, True, {LABEL: "3"}, None],
        # A record that does not say which container it is of.
        [None, True, {LABEL: "3"}, [*SHELL, *FLAGS]],
        "no record",
    ],
)
def test_no_container_is_given_when_dockers_record_does_not_give_its_flags(answer: object, nsenter: Nsenter) -> None:
    nsenter.say(json.dumps(answer))
    assert host(nsenter).engine_container() is None


def test_no_container_is_given_when_docker_has_none_of_the_engine(nsenter: Nsenter) -> None:
    # docker's own answer for a container it does not have is an exit status that is not zero.
    nsenter.fail_on("inspect")
    assert host(nsenter).engine_container() is None


def test_a_docker_that_does_not_say_in_time_which_container_runs_is_not_waited_for(nsenter: Nsenter) -> None:
    nsenter.hang_on("inspect")

    with pytest.raises(HostError) as raised:
        host(nsenter).engine_container(timeout=0.3)

    assert (raised.value.action, raised.value.returncode) == ("inspect", None)
