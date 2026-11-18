"""The units of an instance (`services/engine/units/`; system design, section 4.3).

The unit files are read as systemd reads them, and what each one's `docker run` is given is
checked word by word. No systemd and no container runs here. The engine's two shell commands are
run, with bash, against stand-ins for the host's `aws` and `docker`: the first records what a
`docker run` is given, which is all docker can write to a container's record. The secret is made
at run time.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

from rhi_lifecycle import launch, metrics, release
from rhi_mock_engine import app

ENGINE_FOLDER = Path(__file__).resolve().parents[3] / "services/engine"
NAMES = ("rhi-engine", "rhi-collector", "rhi-lifecycle")
TEXTS = {name: (ENGINE_FOLDER / "units" / f"{name}.service").read_text(encoding="utf-8") for name in NAMES}

SETTINGS = "/run/rhi/release.env"
ARCHITECTURES = ("arm64", "aarch64", "amd64", "x86_64")
# An image that cannot move under a unit: `<repository>[:<tag>]@sha256:<digest>`.
PINNED = re.compile(r"[^\s@:]+(?::[^\s@:]+)?@sha256:[0-9a-f]{64}", re.ASCII)
# The release's engine image, which the release parameter names by its digest (rhi_lifecycle.release).
RELEASE_IMAGE = "$RHI_ENGINE_IMAGE"

# The options of `docker run` that take a value, and those that take none. Any other fails a test.
VALUED = {"--name", "--network", "--pid", "--user", "--env", "--env-file", "--volume", "--workdir", "--entrypoint"}
FLAGS = {"--rm", "--interactive", "--privileged"}

REGION, SECRET_ID = "eu-west-3", "/rhi/pools/mock/bearer"
IMAGE = "registry.example.com/rhi/engine@sha256:" + "0" * 64

AWS = """#!{python}
# The AWS CLI: it records its arguments and prints the secret, when there is one to read.
import json, os, sys
state = os.environ["RHI_TEST_STATE"]
with open(os.path.join(state, "aws.json"), "w", encoding="utf-8") as stream:
    json.dump(sys.argv[1:], stream)
if not os.path.exists(os.path.join(state, "secret")):
    sys.exit(254)
with open(os.path.join(state, "secret"), encoding="utf-8") as stream:
    print(stream.read())
"""
DOCKER = """#!{python}
# The host's docker: it says where its data is and what command an image it holds names, and it
# records what a `run` is given: its arguments, its environment and its standard input.
import json, os, sys
state, call = os.environ["RHI_TEST_STATE"], sys.argv[1:]
answer = os.path.join(state, "docker-data" if call[0] == "info" else "image-command")
if call[0] == "run":
    with open(os.path.join(state, "run.json"), "w", encoding="utf-8") as stream:
        json.dump({{"arguments": call, "environment": dict(os.environ), "input": sys.stdin.read()}}, stream)
elif (call[0] == "info" or call[:2] == ["image", "inspect"]) and os.path.exists(answer):
    with open(answer, encoding="utf-8") as stream:
        # Docker ends what a format prints with a line break of its own.
        print(stream.read())
else:
    sys.exit(1)
"""


def read(text: str) -> dict[str, dict[str, list[str]]]:
    """A unit file's sections, each key with every value it is given. A line that ends with a
    backslash goes on with the next one."""
    sections: dict[str, dict[str, list[str]]] = {}
    for line in map(str.strip, text.replace("\\\n", " ").splitlines()):
        if line.startswith("["):
            section = sections.setdefault(line.strip("[]"), {})
        elif line and line[0] not in "#;":
            key, _, value = line.partition("=")
            section.setdefault(key.strip(), []).append(value.strip())
    return sections


def words(command: str) -> list[str]:
    """The words systemd makes of a command line. The units use no variable of systemd's, no
    specifier and no backslash, so quotes group, `$$` is a dollar sign and `%%` a percent sign."""
    bare = command.replace("$$", "").replace("%%", "")
    assert not set("$%\\") & set(bare), command
    return [word.replace("$$", "$").replace("%%", "%") for word in shlex.split(command.removeprefix("-"))]


class Container(NamedTuple):
    """What a `docker run` is given: its options, each with its values, its image and its command."""

    options: dict[str, list[str]]
    image: str
    command: list[str]

    def mounts(self) -> dict[str, tuple[str, str]]:
        """The host's paths the container mounts, each with its path inside and `ro` or `rw`."""
        parts = [volume.split(":") for volume in self.options.get("--volume", [])]
        return {source: (target, mode[0] if mode else "rw") for source, target, *mode in parts}


def run_of(command: list[str]) -> Container:
    at = next(index for index, word in enumerate(command) if command[index : index + 2] == [word, "run"]) + 2
    options: dict[str, list[str]] = {}
    while command[at].startswith("-"):
        assert command[at] in VALUED | FLAGS, command[at]
        options.setdefault(command[at], []).extend(command[at + 1 : at + 2] if command[at] in VALUED else [])
        at += 2 if command[at] in VALUED else 1
    return Container(options, command[at], command[at + 1 :])


def container(text: str) -> Container:
    """The container a unit starts. The engine's `docker run` ends a shell script."""
    start = words(read(text)["Service"]["ExecStart"][0])
    return run_of(shlex.split(start[2]) if start[0] == "/bin/bash" else start)


def moving_images(text: str) -> list[str]:
    """The images of a unit that could change under it: neither pinned nor the release's."""
    image = container(text).image
    return [] if image == RELEASE_IMAGE or PINNED.fullmatch(image) else [image]


def architectures_named(text: str) -> list[str]:
    return [name for name in ARCHITECTURES if name in text.lower()]


class Host:
    """A folder of stand-ins for the host's `aws` and `docker`, and what they are told to answer."""

    def __init__(self, folder: Path) -> None:
        self.state, self.bin = folder / "state", folder / "bin"
        for made in (self.state, self.bin):
            made.mkdir()
        for name, text in (("aws", AWS), ("docker", DOCKER)):
            (self.bin / name).write_text(text.format(python=sys.executable), encoding="utf-8")
            (self.bin / name).chmod(0o755)

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a command of a unit, with the variables of release.env that the engine's unit reads."""
        environment = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "RHI_TEST_STATE": str(self.state),
            "RHI_REGION": REGION,
            "RHI_SECRET_ID": SECRET_ID,
            "RHI_ENGINE_IMAGE": IMAGE,
        }
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60, check=False)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_every_unit_reads_its_settings_and_restarts_on_failure() -> None:
    for name in NAMES:
        service = read(TEXTS[name])["Service"]
        # No `-` before the path: a unit whose settings are missing does not start.
        assert service["EnvironmentFile"] == [SETTINGS], name
        assert service["Restart"] == ["on-failure"], name


def test_the_engine_is_not_started_at_boot() -> None:
    assert "Install" not in read(TEXTS["rhi-engine"])
    for name in ("rhi-collector", "rhi-lifecycle"):
        assert "rhi-engine" not in json.dumps(read(TEXTS[name])["Unit"]), name
    assert "rhi-engine" not in (ENGINE_FOLDER / "boot/rhi-boot.sh").read_text(encoding="utf-8")


def test_the_engine_listens_on_port_8000_and_every_other_listener_is_on_localhost() -> None:
    # On the host's network, with no port published, a container's listeners are the instance's own.
    for name in NAMES:
        assert container(TEXTS[name]).options["--network"] == ["host"], name
        assert "0.0.0.0" not in TEXTS[name], name
    # The engine's is port 8000, and the agent's is on the loopback address: the two the collector scrapes.
    assert app.PORT == 8000
    assert (metrics.HOST, metrics.PORT) == ("127.0.0.1", 9110)
    collector = yaml.safe_load((ENGINE_FOLDER / "collector/config.yaml").read_text(encoding="utf-8"))
    jobs = collector["receivers"]["prometheus"]["config"]["scrape_configs"]
    scraped = [target for job in jobs for static in job["static_configs"] for target in static["targets"]]
    assert scraped == [f"127.0.0.1:{app.PORT}", f"{metrics.HOST}:{metrics.PORT}"]
    # The collector's configuration names no address to listen on, and leaves the collector's own
    # metrics where the pinned release serves them by default: on the loopback address.
    assert "endpoint" not in json.dumps([collector["receivers"], collector["extensions"]])
    assert "telemetry" not in collector["service"]


def test_the_collector_runs_the_releases_configuration_and_mounts_the_journal_read_only() -> None:
    collector = container(TEXTS["rhi-collector"])
    mounts = collector.mounts()
    for path in ("/var/log/journal", "/run/log/journal", "/etc/machine-id"):
        assert mounts[path] == (path, "ro")
    configuration = "/opt/rhi/release/collector/config.yaml"
    assert mounts[configuration] == (configuration, "ro")
    assert collector.command == ["--config", configuration]
    # Named, not given a value: docker takes each from the unit's environment, which is release.env.
    assert collector.options["--env"] == ["RHI_POOL", "RHI_WORKSPACE_URL", "RHI_REGION", "RHI_LOG_GROUP"]


def test_the_agent_runs_its_program_from_the_release() -> None:
    agent = container(TEXTS["rhi-lifecycle"])
    assert agent.command == ["python", "-m", "rhi_lifecycle"]
    assert agent.options["--workdir"] == ["/opt/rhi/release/agent"]
    mounts = agent.mounts()
    assert mounts["/opt/rhi/release/agent"] == ("/opt/rhi/release/agent", "ro")
    assert mounts["/run/rhi"] == ("/run/rhi", "rw")
    assert agent.options["--env-file"] == [SETTINGS]
    # Privileged, in the host's process namespace: the agent enters the host's namespaces.
    assert agent.options["--pid"] == ["host"]
    assert "--privileged" in agent.options
    # The files its program names are the host's.
    assert launch.SERVED_FILE.parent.as_posix() in mounts
    assert release.HELD_RELEASE_FILE.parent.as_posix() in mounts


def test_images_are_pinned() -> None:
    for name in NAMES:
        assert moving_images(TEXTS[name]) == [], name
    assert container(TEXTS["rhi-engine"]).image == RELEASE_IMAGE
    agent = container(TEXTS["rhi-lifecycle"]).image
    # The official image, at a tag of Python 3.13.
    assert re.fullmatch(r"python:3\.13(?:[.\-][^@:]*)?@sha256:[0-9a-f]{64}", agent), agent

    # An image named by a tag alone is found, in each unit.
    for name in ("rhi-collector", "rhi-lifecycle"):
        tagged = re.sub(r"@sha256:[0-9a-f]{64}", "", TEXTS[name])
        assert moving_images(tagged) == [container(TEXTS[name]).image.partition("@")[0]], name
    assert moving_images(TEXTS["rhi-engine"].replace('"$$RHI_ENGINE_IMAGE" -c', "rhi/engine:latest -c")) == [
        "rhi/engine:latest"
    ]


def test_no_architecture_is_named() -> None:
    for name in NAMES:
        assert architectures_named(TEXTS[name]) == [], name
        # Each of the four names is found, in any case, wherever a unit would hold it.
        for architecture in ARCHITECTURES:
            assert architectures_named(f"{TEXTS[name]}# --platform linux/{architecture.upper()}\n") == [architecture]


def test_the_engine_finds_the_secret_in_its_environment_and_docker_is_never_given_it(host: Host) -> None:
    secret = secrets.token_urlsafe(36)
    printenv = shutil.which("printenv")
    assert printenv is not None
    (host.state / "secret").write_text(secret, encoding="utf-8")
    # The image's command: its entry point, then its command, a word on each line.
    (host.state / "image-command").write_text(f"{printenv}\nRHI_POOL_SECRET\n", encoding="utf-8")

    started = host.run(words(read(TEXTS["rhi-engine"])["Service"]["ExecStart"][0]))

    assert started.returncode == 0, started.stderr
    asked = json.loads((host.state / "aws.json").read_text(encoding="utf-8"))
    assert asked[:6] == ["secretsmanager", "get-secret-value", "--region", REGION, "--secret-id", SECRET_ID]
    # Docker writes the arguments and the variables of a container to its record, under its data
    # directory. Neither holds the secret, and no variable of that name is given.
    given = json.loads((host.state / "run.json").read_text(encoding="utf-8"))
    assert secret not in json.dumps([given["arguments"], given["environment"]])
    assert secret not in started.stdout + started.stderr
    engine = run_of(["docker", *given["arguments"]])
    assert engine.image == IMAGE
    assert engine.command[2:] == ["rhi-engine", printenv, "RHI_POOL_SECRET"]
    assert "--env-file" not in engine.options
    assert not [variable for variable in engine.options["--env"] if "SECRET" in variable]
    # The container's first process, with the standard input docker was given and no environment:
    # the image's own command runs, and finds the secret in RHI_POOL_SECRET.
    assert "--interactive" in engine.options
    inside = subprocess.run(
        [*engine.options["--entrypoint"], *engine.command],
        input=given["input"],
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (inside.returncode, inside.stdout) == (0, secret + "\n")


def test_the_engine_does_not_start_without_the_secret_or_the_image(host: Host) -> None:
    start = words(read(TEXTS["rhi-engine"])["Service"]["ExecStart"][0])
    true = shutil.which("true")
    assert true is not None
    (host.state / "image-command").write_text(f"{true}\n", encoding="utf-8")
    # Secrets Manager gives no secret, then an empty one: no container is run.
    for value in (None, ""):
        if value is not None:
            (host.state / "secret").write_text(value, encoding="utf-8")
        assert host.run(start).returncode != 0
        assert not (host.state / "run.json").exists()
    # A container that is handed nothing does not run the image's command.
    first_process = ["/bin/sh", "-c", start[4], "rhi-engine", true]
    assert subprocess.run(first_process, input="", env={}, capture_output=True, timeout=60, check=False).returncode != 0

    # The host does not hold the image, so its command is not known.
    (host.state / "secret").write_text(secrets.token_urlsafe(36), encoding="utf-8")
    (host.state / "image-command").unlink()
    assert host.run(start).returncode != 0
    assert not (host.state / "run.json").exists()


def test_dockers_data_and_the_compile_cache_are_on_the_ebs_volume(host: Host, tmp_path: Path) -> None:
    service = read(TEXTS["rhi-engine"])["Service"]
    *check, data, cache, volume = words(next(command for command in service["ExecStartPre"] if "bash" in command))
    # The instance's one EBS volume is its root volume, and both directories are named on it. The
    # cache directory is the one the engine's container mounts.
    assert (data, volume) == ("/var/lib/docker", "/")
    assert cache.startswith("/var/lib/")
    assert container(TEXTS["rhi-engine"]).mounts()[cache][1] == "rw"

    # The check the unit makes before each start, here on a folder in the place of the volume.
    here = tmp_path / "volume"
    (here / "docker").mkdir(parents=True)
    (host.state / "docker-data").write_text(str(here / "docker"), encoding="utf-8")
    assert host.run([*check, str(here / "docker"), str(here / "cache"), str(here)]).returncode == 0
    assert (here / "cache").is_dir()
    # Docker keeps its data in another directory than the one the unit names.
    assert host.run([*check, str(here / "elsewhere"), str(here / "cache"), str(here)]).returncode != 0
    # Docker's data is on another filesystem, as an instance store is.
    (host.state / "docker-data").write_text("/proc", encoding="utf-8")
    assert host.run([*check, "/proc", str(here / "cache"), str(here)]).returncode != 0
