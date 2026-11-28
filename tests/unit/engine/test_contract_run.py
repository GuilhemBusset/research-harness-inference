"""The contract check on an instance (`services/engine/contract/run.sh`; system design, section 4.1).

The script runs here as the document `rhi-engine-contract` runs it, with bash, in a temporary root
that holds the release's settings and the release's folder, and against stand-ins for the host's
`aws` and `docker`. `PATH` holds those two, bash, grep and a `python` for the container, so a call
to any other program fails the run. The `docker` stand-in records what it is given. It then
either prints what the test asks for, or runs the container's own command, as the container would:
in the folder mounted at its working directory, with the variables of its `--env` options, the
suite's libraries being this interpreter's. The secret is drawn when the tests run.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn

from rhi_mock_engine.app import Settings, create_app

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "services/engine/contract/run.sh"
LIFECYCLE_UNIT = REPO / "services/engine/units/rhi-lifecycle.service"
TESTS = ("test_inference", "test_health", "test_metrics", "test_bearer_secret")
CONTRACT = "/opt/rhi/release/contract"

AWS = """\
import json, os, sys
with open(os.environ["STAND_IN_RECORD"] + "/aws.json", "w", encoding="utf-8") as stream:
    json.dump(sys.argv[1:], stream)
sys.stdout.write(os.environ["STAND_IN_SECRET"] + "\\n")
"""
DOCKER = """\
import json, os, subprocess, sys
arguments = sys.argv[1:]
given = sys.stdin.read()
with open(os.environ["STAND_IN_RECORD"] + "/docker.json", "w", encoding="utf-8") as stream:
    json.dump({"arguments": arguments, "stdin": given}, stream)
engine = os.environ.get("STAND_IN_ENGINE")
if engine is None:
    sys.stdout.write(os.environ["STAND_IN_OUTPUT"])
    sys.exit(int(os.environ["STAND_IN_STATUS"]))
# The container's own command. The host's loopback port 8000 is the test's: the address it gives.
position, environment, volumes = 1, {}, {}
while arguments[position].startswith("--"):
    option = arguments[position]
    if option in ("--rm", "--interactive"):
        position += 1
        continue
    value = arguments[position + 1]
    position += 2
    if option == "--env":
        name, _, text = value.partition("=")
        environment[name] = text
    elif option == "--volume":
        source, target, _ = value.split(":")
        volumes[target] = source
    elif option == "--workdir":
        workdir = value
    elif option == "--entrypoint":
        entrypoint = value
command = arguments[position + 1 :]
environment |= {"PATH": os.environ["PATH"], "RHI_ENGINE_URL": engine}
run = subprocess.run([entrypoint, *command], cwd=volumes[workdir], env=environment, input=given, text=True)
sys.exit(run.returncode)
"""


@dataclass
class Instance:
    root: Path
    record: Path
    bin: Path
    secret: str

    def run(self, **stand_in: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": str(self.bin),
            "RHI_CONTRACT_ROOT": str(self.root),
            "STAND_IN_RECORD": str(self.record),
            "STAND_IN_SECRET": self.secret,
            **{f"STAND_IN_{name.upper()}": value for name, value in stand_in.items()},
        }
        return subprocess.run(
            ["bash", str(SCRIPT)], env=environment, capture_output=True, text=True, timeout=300, check=False
        )

    def recorded(self, name: str) -> object:
        return json.loads((self.record / f"{name}.json").read_text(encoding="utf-8"))


def instance_with(tmp_path: Path, engine: str) -> Instance:
    root, record, tools = tmp_path / "root", tmp_path / "record", tmp_path / "bin"
    for folder in (root / "run/rhi", root / "opt/rhi/release/units", record, tools):
        folder.mkdir(parents=True)
    (root / "run/rhi/release.env").write_text(
        f"RHI_POOL=mock\nRHI_REGION=eu-west-3\nRHI_SECRET_ID=/rhi/pools/mock/bearer\nRHI_ENGINE={engine}\n",
        encoding="utf-8",
    )
    shutil.copyfile(LIFECYCLE_UNIT, root / "opt/rhi/release/units/rhi-lifecycle.service")
    # The release's contract folder, as the archive lays it out, without the libraries.
    contract = root / "opt/rhi/release/contract"
    ignore = shutil.ignore_patterns("__pycache__")
    for package in ("services/common/rhi_common", "services/gateway/rhi_gateway", "services/engine/mock/rhi_mock_engine"):
        shutil.copytree(REPO / package, contract / Path(package).name, ignore=ignore)
    shutil.copytree(REPO / "tests/contract", contract / "tests/contract", ignore=ignore)
    for file in (REPO / "services/engine/contract").iterdir():
        if file.is_file():
            shutil.copyfile(file, contract / file.name)

    for name, program in (("aws", AWS), ("docker", DOCKER)):
        (tools / name).write_text(f"#!{sys.executable}\n{program}", encoding="utf-8")
        (tools / name).chmod(0o755)
    (tools / "python").write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n', encoding="utf-8")
    (tools / "python").chmod(0o755)
    for name in ("bash", "grep"):
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    return Instance(root, record, tools, secrets.token_urlsafe(32))


def lines(outcomes: dict[str, str], status: int, reasons: dict[str, str] | None = None) -> str:
    """The script's output: each test's outcome, followed by its reason where it has one, then the exit."""
    printed = ""
    for name in TESTS:
        printed += f"rhi-contract {name} {outcomes[name]}\n"
        if name in (reasons or {}):
            printed += f"rhi-contract-reason {name} {(reasons or {})[name]}\n"
    return printed + f"rhi-contract exit {status}\n"


@pytest.mark.parametrize(
    ("printed", "status", "outcomes"),
    [
        pytest.param(dict.fromkeys(TESTS, "passed"), 0, dict.fromkeys(TESTS, "passed"), id="every-test-passed"),
        pytest.param(
            dict.fromkeys(TESTS, "passed") | {"test_metrics": "failed"},
            1,
            dict.fromkeys(TESTS, "passed") | {"test_metrics": "failed"},
            id="a-test-failed",
        ),
        pytest.param({}, 4, dict.fromkeys(TESTS, "missing"), id="no-test-ran"),
    ],
)
def test_the_container_gets_the_secret_on_its_standard_input_and_the_output_is_five_lines(
    tmp_path: Path, printed: dict[str, str], status: int, outcomes: dict[str, str]
) -> None:
    instance = instance_with(tmp_path, "vllm")
    # Around the outcomes, what the script must not repeat: a failure's report, a line of a test
    # it does not run, a line with more words.
    output = f"E   secret={instance.secret}\n" + "".join(f"{n} {o}\n" for n, o in printed.items())
    output += "test_shutdown passed\ntest_health passed and more\n"
    run = instance.run(output=output, status=str(status))

    assert run.stdout == lines(outcomes, status)
    assert run.stderr == ""
    assert run.returncode == status

    assert instance.recorded("aws") == [
        *("secretsmanager", "get-secret-value", "--region", "eu-west-3", "--secret-id", "/rhi/pools/mock/bearer"),
        *("--query", "SecretString", "--output", "text"),
    ]
    docker = instance.recorded("docker")
    assert isinstance(docker, dict)
    arguments, stdin = docker["arguments"], docker["stdin"]
    assert stdin == instance.secret
    assert not [argument for argument in arguments if instance.secret in argument]
    assert "--env-file" not in arguments
    given = [arguments[i + 1] for i, argument in enumerate(arguments) if argument == "--env"]
    assert "RHI_ENGINE_URL=http://127.0.0.1:8000" in given
    assert "RHI_ENGINE_NAME=vllm" in given
    assert not [variable for variable in given if variable.startswith("RHI_POOL_SECRET")]

    image = re.search(r"python:\S+@sha256:[0-9a-f]{64}", LIFECYCLE_UNIT.read_text(encoding="utf-8"))
    assert image
    assert arguments[0] == "run"
    position = arguments.index(image[0])
    assert arguments[position - 2 : position] == ["--entrypoint", "/bin/sh"]
    options = arguments[:position]
    assert options[options.index("--network") + 1] == "host"
    assert options[options.index("--volume") + 1] == f"{instance.root}{CONTRACT}:{CONTRACT}:ro"
    assert options[options.index("--workdir") + 1] == CONTRACT
    assert arguments[-len(TESTS) :] == [f"tests/contract/test_engine_contract.py::{name}" for name in TESTS]


def test_a_failed_test_s_reason_follows_its_outcome_in_the_suite_s_words_only(tmp_path: Path) -> None:
    instance = instance_with(tmp_path, "vllm")
    outcomes = dict.fromkeys(TESTS, "failed") | {"test_health": "passed"}
    output = "".join(f"{name} {outcome}\n" for name, outcome in outcomes.items())
    output += "test_inference reason messages-whole no-text max_tokens\n"
    # A word outside the vocabulary, the secret, more words, a passed test's, a test not run here.
    output += "test_metrics reason metrics metric-missing-sentinel none\n"
    output += f"test_bearer_secret reason {instance.secret} status-200 none\n"
    output += "test_bearer_secret reason chat-with-secret status-401 none and more\n"
    output += "test_health reason health status-503 none\ntest_shutdown reason none timeout none\n"
    run = instance.run(output=output, status="1")

    reasons = {"test_inference": "messages-whole no-text max_tokens"}
    assert run.stdout == lines(outcomes, 1, reasons)
    assert run.returncode == 1


async def _no_sleep(seconds: float) -> None:
    del seconds


def test_the_suite_passes_against_an_engine_on_the_loopback_address(tmp_path: Path) -> None:
    """The container's own command, run against the mock engine, served with the instance's secret."""
    instance = instance_with(tmp_path, "mock")
    server = uvicorn.Server(
        uvicorn.Config(create_app(Settings(secret=instance.secret), sleep=_no_sleep), access_log=False, log_level="warning")
    )
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        serving = threading.Thread(target=server.run, kwargs={"sockets": [listening]})
        serving.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                assert serving.is_alive() and time.monotonic() < deadline, "the mock engine did not start"
                time.sleep(0.01)
            run = instance.run(engine=f"http://127.0.0.1:{listening.getsockname()[1]}")
        finally:
            server.should_exit = True
            serving.join(timeout=30)

    assert run.stdout == lines(dict.fromkeys(TESTS, "passed"), 0)
    assert run.returncode == 0


def test_pytest_s_report_stays_in_the_container_and_only_the_outcomes_leave(tmp_path: Path) -> None:
    """The container's own command, run: against an engine that refuses every connection, each
    test fails with a report that names the secret, and the script prints the outcomes and the
    reasons alone."""
    instance = instance_with(tmp_path, "mock")
    with socket.socket() as refusing:
        refusing.bind(("127.0.0.1", 0))  # bound and not listening: a connection is refused
        run = instance.run(engine=f"http://127.0.0.1:{refusing.getsockname()[1]}")

    # Each test's reason: the first request it sends found no engine to connect to.
    reasons = {
        "test_inference": "chat-whole no-connection none",
        "test_health": "health no-connection none",
        "test_metrics": "chat-streamed no-connection none",
        "test_bearer_secret": "chat-without-secret no-connection none",
    }
    assert run.stdout == lines(dict.fromkeys(TESTS, "failed"), 1, reasons)
    assert run.returncode == 1
    assert instance.secret not in run.stdout + run.stderr
    # Nothing written beside the suite: no report, no cache.
    contract = instance.root / "opt/rhi/release/contract"
    assert not [p for p in contract.rglob("*") if p.suffix == ".xml" or p.name in (".pytest_cache", "__pycache__")]


def test_settings_without_the_engine_run_no_container(tmp_path: Path) -> None:
    instance = instance_with(tmp_path, "vllm")
    settings = instance.root / "run/rhi/release.env"
    settings.write_text(settings.read_text(encoding="utf-8").replace("RHI_ENGINE=vllm\n", ""), encoding="utf-8")
    run = instance.run(output="", status="0")
    assert run.stdout == lines(dict.fromkeys(TESTS, "missing"), 1)
    assert run.returncode == 1
    assert not (instance.record / "docker.json").exists()
