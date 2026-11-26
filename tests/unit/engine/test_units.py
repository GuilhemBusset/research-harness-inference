"""The units of an instance (`services/engine/units/`; system design, section 4.3).

The unit files are read as systemd reads them, and what each one's `docker run` is given is
checked word by word. No systemd and no container runs here. The engine's two shell commands are
run, with bash, against stand-ins for the host's `aws` and `docker`: the first records what a
`docker run` is given, which is all docker can write to a container's record. The secret is made
at run time.
"""

from __future__ import annotations

import csv
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

from rhi_lifecycle import host as agent_host
from rhi_lifecycle import metrics, release
from rhi_mock_engine import app

ENGINE_FOLDER = Path(__file__).resolve().parents[3] / "services/engine"
NAMES = ("rhi-engine", "rhi-collector", "rhi-lifecycle")
TEXTS = {name: (ENGINE_FOLDER / "units" / f"{name}.service").read_text(encoding="utf-8") for name in NAMES}
# The units of a GPU instance only, which the boot program installs on no other.
GPU_DROP_IN = ENGINE_FOLDER / "units/gpu/rhi-engine.service.d/gpu.conf"
# The DCGM exporter's unit, and the drop-in that makes the collector's unit want it.
DCGM_UNIT = ENGINE_FOLDER / "units/gpu/rhi-dcgm.service"
DCGM_TEXT = DCGM_UNIT.read_text(encoding="utf-8")
COLLECTOR_DROP_IN = ENGINE_FOLDER / "units/gpu/rhi-collector.service.d/dcgm.conf"
# The exporter's fields, where the release's archive puts them on an instance, and its version.
DCGM_FIELDS = "/opt/rhi/release/dcgm/counters.csv"
DCGM_FOLDER = ENGINE_FOLDER / "dcgm"
# The quantities of the GPU row (system design, section 7.2), each with the field of exporter
# 4.9.0 that gives it and that field's type. Tokens per joule are a rate of the energy counter.
GPU_QUANTITIES = {
    "utilization of memory bandwidth": ("DCGM_FI_DEV_MEMORY_UTIL_RATIO", "gauge"),
    "utilization of compute": ("DCGM_FI_DEV_GPU_UTIL_RATIO", "gauge"),
    "memory": ("DCGM_FI_DEV_FB_USED_BYTES", "gauge"),
    "temperature": ("DCGM_FI_DEV_GPU_TEMP_CELSIUS", "gauge"),
    "power": ("DCGM_FI_DEV_BOARD_POWER_WATTS", "gauge"),
    "energy": ("DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL", "counter"),
    "Xid errors": ("DCGM_FI_DEV_XID_ERROR", "gauge"),
}

SETTINGS = "/run/rhi/release.env"
ARCHITECTURES = ("arm64", "aarch64", "amd64", "x86_64")
# An image that cannot move under a unit: `<repository>[:<tag>]@sha256:<digest>`.
PINNED = re.compile(r"[^\s@:]+(?::[^\s@:]+)?@sha256:[0-9a-f]{64}", re.ASCII)
# The release's engine image, which the release parameter names by its digest (rhi_lifecycle.release).
RELEASE_IMAGE = "$RHI_ENGINE_IMAGE"

# The options of `docker run` that take a value, and those that take none. Any other fails a test.
VALUED = {
    "--name", "--network", "--pid", "--user", "--env", "--env-file", "--volume", "--workdir", "--entrypoint",
    "--runtime", "--gpus", "--label",
}  # fmt: skip
FLAGS = {"--rm", "--interactive", "--privileged"}
# Where the engine's start command places the options of a drop-in, and where it reads its flags.
DROP_IN_OPTIONS = "${options[@]}"
ENGINE_FLAGS = "/run/rhi/engine-flags"

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
    if start[0] != "/bin/bash":
        return run_of(start)
    return run_of([word for word in shlex.split(start[2]) if word != DROP_IN_OPTIONS])


def moving_images(text: str) -> list[str]:
    """The images of a unit that could change under it: neither pinned nor the release's."""
    image = container(text).image
    return [] if image == RELEASE_IMAGE or PINNED.fullmatch(image) else [image]


def exporter_version(text: str) -> tuple[int, ...]:
    """A version of the DCGM exporter, as numbers. One below 4.9.0, which still exports the old
    names of the fields, fails."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", text), text
    found = tuple(int(part) for part in text.split("."))
    assert found >= (4, 9, 0), f"DCGM exporter {text} is below 4.9.0"
    return found


def exporter_fields(text: str) -> dict[str, str]:
    """The fields of an exporter's list, each with its Prometheus type. The exporter's format: a
    line that starts with # is a comment, and every other one is a field, a type and a help text."""
    rows = list(csv.reader(line for line in text.splitlines() if line and not line.startswith("#")))
    assert all(len(row) == 3 and all(value.strip() for value in row) for row in rows), rows
    fields = {row[0].strip(): row[1].strip() for row in rows}
    assert len(fields) == len(rows), "a field is listed twice"
    assert set(fields.values()) <= {"gauge", "counter", "label"}
    return fields


def architectures_named(text: str) -> list[str]:
    return [name for name in ARCHITECTURES if name in text.lower()]


def drop_in_variables(text: str) -> dict[str, str]:
    """The variables a drop-in sets with Environment=, as systemd reads them: quotes group the
    words of an assignment, and a dollar sign is itself."""
    assignments = [word for line in read(text)["Service"]["Environment"] for word in shlex.split(line)]
    return dict(assignment.split("=", 1) for assignment in assignments)


class Host:
    """A folder of stand-ins for the host's `aws` and `docker`, and what they are told to answer."""

    def __init__(self, folder: Path) -> None:
        self.state, self.bin = folder / "state", folder / "bin"
        for made in (self.state, self.bin):
            made.mkdir()
        for name, text in (("aws", AWS), ("docker", DOCKER)):
            (self.bin / name).write_text(text.format(python=sys.executable), encoding="utf-8")
            (self.bin / name).chmod(0o755)
        # The engine's flags, as the boot program writes those of a release that has none.
        self.flags = self.state / "engine-flags"
        self.flags.write_text("", encoding="utf-8")
        # The start command of the engine's unit, which names the file of its flags last.
        *start, flags = words(read(TEXTS["rhi-engine"])["Service"]["ExecStart"][0])
        assert flags == ENGINE_FLAGS
        self.start = [*start, str(self.flags)]

    def run(self, command: list[str], drop_in: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Run a command of a unit, with the variables of release.env that the engine's unit reads
        and those a drop-in sets."""
        environment = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "RHI_TEST_STATE": str(self.state),
            "RHI_REGION": REGION,
            "RHI_SECRET_ID": SECRET_ID,
            "RHI_ENGINE_IMAGE": IMAGE,
            **(drop_in or {}),
        }
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60, check=False)

    def started(self, drop_in: dict[str, str] | None = None) -> Container:
        """The container the engine's unit starts, on a host that holds the secret and the image."""
        (self.state / "secret").write_text(secrets.token_urlsafe(36), encoding="utf-8")
        (self.state / "image-command").write_text("engine\nserve\n", encoding="utf-8")
        ran = self.run(self.start, drop_in)
        assert ran.returncode == 0, ran.stderr
        given = json.loads((self.state / "run.json").read_text(encoding="utf-8"))
        return run_of(["docker", *given["arguments"]])


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
    # The boot program names the engine's unit nowhere. The one `rhi-engine` it reads is the
    # instance's tag that names the pool's engine, from the instance metadata.
    boot = (ENGINE_FOLDER / "boot/rhi-boot.sh").read_text(encoding="utf-8")
    assert boot.count("/meta-data/tags/instance/rhi-engine") == 1
    assert "rhi-engine" not in boot.replace("/meta-data/tags/instance/rhi-engine", "")


def test_the_engine_listens_on_port_8000_and_every_other_listener_is_on_localhost() -> None:
    # On the host's network, with no port published, a container's listeners are the instance's own.
    for name in NAMES:
        assert container(TEXTS[name]).options["--network"] == ["host"], name
        assert "0.0.0.0" not in TEXTS[name], name
    # The engine's is port 8000, and the agent's is on the loopback address. So is the DCGM
    # exporter's, the one listener it is told to open: the three the collector scrapes.
    assert app.PORT == 8000
    assert (metrics.HOST, metrics.PORT) == ("127.0.0.1", 9110)
    exporter = container(DCGM_TEXT)
    assert exporter.options["--network"] == ["host"]
    assert "0.0.0.0" not in DCGM_TEXT
    assert exporter.command[:2] == ["--address", "127.0.0.1:9400"]
    assert exporter.command.count("--address") == 1
    collector = yaml.safe_load((ENGINE_FOLDER / "collector/config.yaml").read_text(encoding="utf-8"))
    jobs = collector["receivers"]["prometheus"]["config"]["scrape_configs"]
    scraped = [target for job in jobs for static in job["static_configs"] for target in static["targets"]]
    assert scraped == [f"127.0.0.1:{app.PORT}", f"{metrics.HOST}:{metrics.PORT}", exporter.command[1]]
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
    assert agent.options["--env-file"] == [SETTINGS]
    # Privileged, in the host's process namespace: the agent enters the host's namespaces.
    assert agent.options["--pid"] == ["host"]
    assert "--privileged" in agent.options
    # The file its program names is the host's.
    assert release.HELD_RELEASE_FILE.parent.as_posix() in mounts


def test_images_are_pinned() -> None:
    for name in NAMES:
        assert moving_images(TEXTS[name]) == [], name
    assert moving_images(DCGM_TEXT) == []
    assert container(TEXTS["rhi-engine"]).image == RELEASE_IMAGE
    agent = container(TEXTS["rhi-lifecycle"]).image
    # The official image, at a tag of Python 3.13.
    assert re.fullmatch(r"python:3\.13(?:[.\-][^@:]*)?@sha256:[0-9a-f]{64}", agent), agent

    # An image named by a tag alone is found, in each unit.
    for text in (TEXTS["rhi-collector"], TEXTS["rhi-lifecycle"], DCGM_TEXT):
        tagged = re.sub(r"@sha256:[0-9a-f]{64}", "", text)
        assert moving_images(tagged) == [container(text).image.partition("@")[0]]
    assert moving_images(TEXTS["rhi-engine"].replace('"$$RHI_ENGINE_IMAGE" -c', "rhi/engine:latest -c")) == [
        "rhi/engine:latest"
    ]


def test_no_architecture_is_named() -> None:
    # Every file of the folder, with those of a GPU instance.
    files = [path for path in (ENGINE_FOLDER / "units").rglob("*") if path.is_file()]
    gpu = {GPU_DROP_IN, DCGM_UNIT, COLLECTOR_DROP_IN}
    assert {*gpu, *(ENGINE_FOLDER / "units" / f"{name}.service" for name in NAMES)} <= set(files)
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert architectures_named(text) == [], path.name
        # Each of the four names is found, in any case, wherever a unit would hold it.
        for architecture in ARCHITECTURES:
            assert architectures_named(f"{text}# --platform linux/{architecture.upper()}\n") == [architecture]


def test_the_engine_finds_the_secret_in_its_environment_and_docker_is_never_given_it(host: Host) -> None:
    secret = secrets.token_urlsafe(36)
    printenv = shutil.which("printenv")
    assert printenv is not None
    (host.state / "secret").write_text(secret, encoding="utf-8")
    # The image's command: its entry point, then its command, a word on each line.
    (host.state / "image-command").write_text(f"{printenv}\nRHI_POOL_SECRET\n", encoding="utf-8")

    started = host.run(host.start)

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
    start = host.start
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


def test_the_engine_is_given_the_releases_flags_after_the_images_command(host: Host) -> None:
    flags = ["--served-model-name", "a name of four words", "--max-model-len=65536"]
    host.flags.write_text("".join(flag + "\n" for flag in flags), encoding="utf-8")
    # In order, each one argument, after the image's own command.
    assert host.started().command[2:] == ["rhi-engine", "engine", "serve", *flags]
    # A release with no flag: the image's command alone.
    host.flags.write_text("", encoding="utf-8")
    assert host.started().command[2:] == ["rhi-engine", "engine", "serve"]
    # The boot program wrote no flags: no container is run.
    host.flags.unlink()
    (host.state / "run.json").unlink()
    assert host.run(host.start).returncode != 0
    assert not (host.state / "run.json").exists()


def test_the_agent_reads_back_the_flags_of_the_container_the_unit_started(host: Host, tmp_path: Path) -> None:
    # A pool's flags hold a value of the deployment, the model bucket's name, and may hold a name
    # that starts with a hyphen.
    flags = [
        f"s3://rhi-models-123456789012-{REGION}/coursework/{'c' * 40}/",
        "--served-model-name=-coursework",
        "--max-num-batched-tokens",
        "4096",
    ]
    host.flags.write_text("".join(flag + "\n" for flag in flags), encoding="utf-8")
    # A drop-in that names the label does not set it: the unit's own comes last.
    started = host.started({"RHI_ENGINE_OPTIONS": f"--label {agent_host.FLAGS_LABEL}=1"})
    assert started.options["--label"][-1] == f"{agent_host.FLAGS_LABEL}={len(flags)}"

    # Docker's record of that container: its ID, that it runs, each label with the last value it
    # was given, and its arguments, which are what follows the image. The agent's host module asks
    # docker for the four.
    labels = dict(label.split("=", 1) for label in started.options["--label"])
    docker = tmp_path / "nsenter"
    docker.write_text('#!/bin/sh\ncat "$0.answer"\n', encoding="utf-8")
    docker.chmod(0o755)
    record = ["d" * 64, True, labels, started.command]
    (tmp_path / "nsenter.answer").write_text(json.dumps(record), encoding="utf-8")
    running = agent_host.Host(object(), nsenter=str(docker)).engine_container()
    assert running is not None
    assert running.arguments == tuple(flags)


def test_the_engine_starts_as_before_without_a_drop_in_and_with_the_options_of_one(host: Host) -> None:
    alone = host.started()
    # The options the unit names, and the image's command. The label's value is made at the start:
    # the number of the release's flags, here none.
    assert alone.options == {**container(TEXTS["rhi-engine"]).options, "--label": [f"{agent_host.FLAGS_LABEL}=0"]}
    assert (alone.image, alone.command[2:]) == (IMAGE, ["rhi-engine", "engine", "serve"])

    # A drop-in sets one variable. The start shell reads its words: quotes group, and a variable
    # of release.env is given its value.
    more = host.started({"RHI_ENGINE_OPTIONS": '--workdir /srv --env "EXAMPLE=the region is $RHI_REGION"'})
    added = {"--workdir": ["/srv"], "--env": [f"EXAMPLE=the region is {REGION}"]}
    assert more.options == {name: alone.options.get(name, []) + added.get(name, []) for name in alone.options | added}
    assert (more.image, more.command) == (alone.image, alone.command)


def test_the_gpu_drop_in_adds_the_nvidia_runtime_every_gpu_and_the_region(host: Host) -> None:
    text = GPU_DROP_IN.read_text(encoding="utf-8")
    # It sets the one variable of the unit's options, and restates no key of the unit.
    assert {section: set(keys) for section, keys in read(text).items()} == {"Service": {"Environment"}}
    variables = drop_in_variables(text)
    assert set(variables) == {"RHI_ENGINE_OPTIONS"}

    alone, gpu = host.started(), host.started(variables)

    # AWS_REGION has the value of RHI_REGION, not its name. Every other option is the unit's, once.
    added = {"--runtime": ["nvidia"], "--gpus": ["all"], "--env": [f"AWS_REGION={REGION}"]}
    assert gpu.options == {name: alone.options.get(name, []) + added.get(name, []) for name in alone.options | added}
    assert (gpu.image, gpu.command) == (alone.image, alone.command)


def test_the_exporter_runs_on_every_gpu_with_the_releases_fields_and_restarts_on_failure() -> None:
    sections = read(DCGM_TEXT)
    assert sections["Service"]["Restart"] == ["on-failure"]
    exporter = container(DCGM_TEXT)
    # The NVIDIA runtime with every GPU, as the engine's container, and nothing more: no
    # capability, no privilege, no published port.
    assert (exporter.options["--runtime"], exporter.options["--gpus"]) == (["nvidia"], ["all"])
    assert set(exporter.options) == {"--rm", "--name", "--network", "--runtime", "--gpus", "--volume"}
    # The fields are the release's, read-only, in place of the image's own list.
    assert exporter.mounts() == {DCGM_FIELDS: (DCGM_FIELDS, "ro")}
    assert exporter.command[2:] == ["--collectors", DCGM_FIELDS]


def test_starting_the_collector_starts_the_exporter_on_a_gpu_instance() -> None:
    # Nothing else starts the exporter: its unit has no [Install] section, and the boot program
    # starts the collector's and the agent's units by name.
    assert "Install" not in read(DCGM_TEXT)
    assert "rhi-dcgm" not in (ENGINE_FOLDER / "boot/rhi-boot.sh").read_text(encoding="utf-8")
    # The drop-in adds one key to the collector's unit, and restates nothing of it.
    drop_in = read(COLLECTOR_DROP_IN.read_text(encoding="utf-8"))
    assert drop_in == {"Unit": {"Wants": [DCGM_UNIT.name]}}


def test_the_exporters_image_is_the_recorded_version_which_is_4_9_0_or_later() -> None:
    recorded = (DCGM_FOLDER / "VERSION").read_text(encoding="utf-8")
    assert recorded.endswith("\n") and recorded.count("\n") == 1, "VERSION holds one line"
    image = container(DCGM_TEXT).image
    named = re.fullmatch(r"nvcr\.io/nvidia/k8s/dcgm-exporter:([0-9.]+)@sha256:[0-9a-f]{64}", image)
    assert named, f"{image} is not NVIDIA's image by digest, at a tag of the exporter's version"
    assert exporter_version(named[1]) == exporter_version(recorded.strip())

    # A version below 4.9.0 is refused. 4.10.0 is above 4.9.0 as numbers, and below it as text.
    for below in ("4.8.4", "3.12.0"):
        with pytest.raises(AssertionError, match="below"):
            exporter_version(below)
    assert exporter_version("4.10.0") > exporter_version("4.9.0")


def test_the_exporters_fields_name_one_for_each_quantity_of_the_gpu_panels() -> None:
    fields = exporter_fields((DCGM_FOLDER / "counters.csv").read_text(encoding="utf-8"))
    for quantity, (field, kind) in GPU_QUANTITIES.items():
        assert fields.get(field) == kind, quantity
    # A line that is not a field, a type and a help text is found.
    with pytest.raises(AssertionError):
        exporter_fields("DCGM_FI_DEV_BOARD_POWER_WATTS, gauge\n")


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
