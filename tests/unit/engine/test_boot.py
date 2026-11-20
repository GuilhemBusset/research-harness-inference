"""The boot unit (`services/engine/boot/`; system design, section 4.3).

The boot program and the user data run here as an instance runs them, with bash, in a temporary
root. Three stand-ins take the place of what an instance has: a loopback server that answers as
the instance metadata does and refuses a read that carries no session token, an `aws` that knows
one region, one parameter and one object, and a `systemctl` that records its calls. `PATH` holds
those two and the tools the issue allows, so a call to any other program fails the run. Every
digest and every secret-shaped value below is made at run time.

The engine's flags are followed to the engine's container: the start command of the engine's unit
runs, with bash, against stand-ins for the host's `aws` and `docker`, with the settings and the
flags the boot program wrote.
"""

from __future__ import annotations

import ast
import base64
import configparser
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi_lifecycle import release

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BOOT = Path(__file__).resolve().parents[3] / "services/engine/boot"
USER_DATA, PROGRAM, UNIT = BOOT / "user-data.sh", BOOT / "rhi-boot.sh", BOOT / "rhi-boot.service"
ENGINE_UNIT = BOOT.parent / "units/rhi-engine.service"

REGION = "eu-west-3"
PARAMETER = "/rhi/pools/coursework/release"
TAG, PLACEMENT = "tags/instance/rhi-release-parameter", "placement/region"
OBJECT = "s3://artifacts/releases/coursework.tar"
# The archive of a release: units/, collector/config.yaml and agent/ at its root.
MEMBERS = {
    "units/rhi-collector.service": "[Service]\nExecStart=/usr/bin/true the collector\n",
    "units/rhi-lifecycle.service": "[Service]\nExecStart=/usr/bin/true the lifecycle agent\n",
    "units/later/rhi-other.service": "[Service]\nExecStart=/usr/bin/true a later issue\n",
    "units/gpu/rhi-engine.service.d/gpu.conf": "[Service]\nEnvironment=RHI_ENGINE_OPTIONS=--gpus=all\n",
    "collector/config.yaml": "receivers: {}\n",
    "agent/rhi_lifecycle/__main__.py": "print('the agent')\n",
}
TOP_UNITS = {"rhi-collector.service", "rhi-lifecycle.service"}
GPU_DROP_IN = "rhi-engine.service.d/gpu.conf"
# The vendor IDs of two PCI devices: NVIDIA's, and Amazon's, which a network adapter has.
NVIDIA, AMAZON = "0x10de", "0x1d0f"

# The programs of GNU coreutils. With bash, tar, curl and python3 they are all the boot program may call.
COREUTILS = """[ b2sum base32 base64 basename basenc cat chcon chgrp chmod chown chroot cksum comm cp csplit cut date dd
df dir dircolors dirname du echo env expand expr factor false fmt fold groups head hostid id install join link ln
logname ls md5sum mkdir mkfifo mknod mktemp mv nice nl nohup nproc numfmt od paste pathchk pinky pr printenv printf
ptx pwd readlink realpath rm rmdir runcon seq sha1sum sha224sum sha256sum sha384sum sha512sum shred shuf sleep sort
split stat stdbuf stty sum sync tac tail tee test timeout touch tr true truncate tsort tty uname unexpand uniq unlink
users vdir wc who whoami yes""".split()  # noqa: SIM905 - a hundred names read better as words than as a list

AWS = '''#!/usr/bin/env python3
"""The AWS CLI, for the two calls of the boot program: one region, its parameters and its objects."""
import argparse
import json
import os
import shutil
import sys

parser = argparse.ArgumentParser()
parser.add_argument("call", nargs=2)
parser.add_argument("--region", required=True)
parser.add_argument("--name")
parser.add_argument("--output", choices=["json"])
parser.add_argument("--only-show-errors", action="store_true")
parser.add_argument("paths", nargs="*")
arguments = parser.parse_intermixed_args()
with open(os.path.join(os.environ["RHI_TEST_STATE"], "cloud.json"), encoding="utf-8") as stream:
    cloud = json.load(stream)
if arguments.region != cloud["region"]:
    sys.exit(254)
if arguments.call == ["ssm", "get-parameter"] and arguments.output and arguments.name in cloud["parameters"]:
    value = cloud["parameters"][arguments.name]
    print(json.dumps({"Parameter": {"Name": arguments.name, "Type": "String", "Value": value}}))
elif arguments.call == ["s3", "cp"] and len(arguments.paths) == 2 and arguments.paths[0] in cloud["objects"]:
    shutil.copyfile(cloud["objects"][arguments.paths[0]], arguments.paths[1])
else:
    sys.exit(254)
'''
SYSTEMCTL = """#!/bin/bash
# systemctl: it records each call and, at a start, what is installed by then.
printf '%s\\n' "$*" >>"$RHI_TEST_STATE/systemctl.log"
case " $* " in
  *" start "*) ls "$RHI_BOOT_ROOT/run/systemd/system" "$RHI_BOOT_ROOT/run/rhi" >"$RHI_TEST_STATE/at-start" 2>&1 ;;
esac
exit 0
"""
HOST = """#!{python}
# The host's `aws` and `docker`, for the engine's start: a secret, the command of the image, and
# a record of what a `docker run` is given.
import json, os, sys
call = sys.argv[1:]
if os.path.basename(sys.argv[0]) == "aws":
    print(os.environ["RHI_TEST_SECRET"])
elif call[:2] == ["image", "inspect"]:
    print("engine\\nserve")
elif call[0] == "run":
    with open(os.path.join(os.environ["RHI_TEST_STATE"], "run.json"), "w", encoding="utf-8") as stream:
        json.dump(call, stream)
else:
    sys.exit(1)
"""


def archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, text in MEMBERS.items():
            member = tarfile.TarInfo(name)
            member.size = len(text.encode())
            tar.addfile(member, io.BytesIO(text.encode()))
    return buffer.getvalue()


def parameter_value(files: bytes) -> dict[str, Any]:
    """The parameter's value for a release whose archive is `files`, with the keys the agent reads."""
    return {
        "engine_image": f"123456789012.dkr.ecr.{REGION}.amazonaws.com/rhi/engine@sha256:{hashlib.sha256(b'i').hexdigest()}",
        "model_revision": hashlib.sha1(b"a revision", usedforsecurity=False).hexdigest(),
        "engine_flags": ["--max-model-len", "65536"],
        "files": {"bucket": "artifacts", "key": "releases/coursework.tar", "sha256": hashlib.sha256(files).hexdigest()},
        "settings": {
            "pool": "coursework",
            "group": "rhi-coursework",
            "launch_hook": "launch",
            "termination_hook": "termination",
            "log_group": "/rhi/coursework",
            "workspace_url": "https://aps-workspaces.example.com/api/v1/remote_write",
            "heartbeat_table": "rhi-heartbeats",
            "secret_id": "rhi/coursework/engine",
        },
    }


class Metadata(ThreadingHTTPServer):
    """The instance metadata, as IMDSv2 serves it: a session token first, then reads that carry it."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), MetadataRequest)
        self.token = secrets.token_urlsafe(24)
        self.values = {TAG: PARAMETER, PLACEMENT: REGION}
        self.reads: list[str] = []


class MetadataRequest(BaseHTTPRequestHandler):
    def answer(self, status: int, body: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_PUT(self) -> None:
        assert isinstance(self.server, Metadata)
        asked = self.path == "/latest/api/token" and self.headers.get("X-aws-ec2-metadata-token-ttl-seconds")
        self.answer(200, self.server.token) if asked else self.answer(400)

    def do_GET(self) -> None:
        assert isinstance(self.server, Metadata)
        if self.headers.get("X-aws-ec2-metadata-token") != self.server.token:
            self.answer(401)
            return
        name = self.path.removeprefix("/latest/meta-data/")
        self.server.reads.append(name)
        self.answer(200, self.server.values[name]) if name in self.server.values else self.answer(404)

    def log_message(self, format: str, *args: object) -> None:
        """Nothing: the server's log is not the test's output."""


class Instance:
    """A temporary root, the stand-ins, and a release in the pool's parameter and bucket."""

    def __init__(self, folder: Path, metadata: Metadata) -> None:
        self.root, self.state, self.bin = folder / "root", folder / "state", folder / "bin"
        for made in (self.root, self.state, self.bin):
            made.mkdir()
        self.metadata = metadata
        self.url = f"http://127.0.0.1:{metadata.server_address[1]}"
        for tool in ("bash", "tar", "curl", "python3", *COREUTILS):
            found = shutil.which(tool)
            if found is not None:
                (self.bin / tool).symlink_to(found)
        for name, text in (("aws", AWS), ("systemctl", SYSTEMCTL)):
            (self.bin / name).write_text(text, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.files = archive()
        self.value = parameter_value(self.files)
        self.publish(json.dumps(self.value), self.files)

    def publish(self, value: str | None, files: bytes | None) -> None:
        """Put `value` in the pool's parameter and `files` in the bucket; None leaves one out."""
        (self.state / "release.tar").write_bytes(files or b"")
        cloud = {
            "region": REGION,
            "parameters": {} if value is None else {PARAMETER: value},
            "objects": {} if files is None else {OBJECT: str(self.state / "release.tar")},
        }
        (self.state / "cloud.json").write_text(json.dumps(cloud), encoding="utf-8")

    def run(self, script: Path) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": str(self.bin),
            "RHI_BOOT_ROOT": str(self.root),
            "RHI_BOOT_METADATA": self.url,
            "RHI_TEST_STATE": str(self.state),
        }
        command = [str(self.bin / "bash"), str(script)]
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120, check=False)

    def calls(self) -> list[list[str]]:
        """What systemctl was called with: the command, then the units, without their `.service`."""
        log = self.state / "systemctl.log"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [[word.removesuffix(".service") for word in line.split() if not word.startswith("--")] for line in lines]

    def units(self, command: str) -> set[str]:
        return {unit for call in self.calls() if call[0] == command for unit in call[1:]}

    def stop(self) -> None:
        """What a stop does to the instance: /run is emptied. The calls so far are forgotten too."""
        shutil.rmtree(self.root / "run")
        (self.state / "systemctl.log").unlink()

    def files_under(self, folder: str) -> set[str]:
        top = self.root / folder
        return {path.relative_to(top).as_posix() for path in top.rglob("*") if path.is_file()}

    def pci_device(self, address: str, vendor: str) -> None:
        """One more device on the PCI bus, as the kernel lists it: a folder with its vendor file."""
        device = self.root / "sys/bus/pci/devices" / address
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor + "\n", encoding="utf-8")

    def engine_arguments(self) -> list[str]:
        """What the engine's unit gives the container after the command of its image, which is
        `engine serve`. The unit's start command runs as systemd runs it, with the settings the
        boot program wrote and the file the unit names for its flags, both under the root."""
        unit = ENGINE_UNIT.read_text(encoding="utf-8").replace("\\\n", " ")
        start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        # For systemd, `$$` is a dollar sign and `%%` a percent sign.
        *command, flags = shlex.split(start.removeprefix("ExecStart=").replace("$$", "$").replace("%%", "%"))
        host = self.state / "host"
        host.mkdir(exist_ok=True)
        for name in ("aws", "docker"):
            (host / name).write_text(HOST.format(python=sys.executable), encoding="utf-8")
            (host / name).chmod(0o755)
        settings = (self.root / "run/rhi/release.env").read_text(encoding="utf-8").splitlines()
        environment = {
            **dict(line.split("=", 1) for line in settings),
            "PATH": f"{host}{os.pathsep}{self.bin}",
            "RHI_TEST_STATE": str(self.state),
            "RHI_TEST_SECRET": secrets.token_urlsafe(36),
        }
        subprocess.run([*command, str(self.root / flags.lstrip("/"))], env=environment, timeout=60, check=True)
        given = json.loads((self.state / "run.json").read_text(encoding="utf-8"))
        at = given.index("serve", given.index("engine", given.index("--entrypoint")))
        assert given[at - 1 : at + 1] == ["engine", "serve"]
        return given[at + 1 :]


@pytest.fixture
def instance(tmp_path: Path) -> Iterator[Instance]:
    metadata = Metadata()
    threading.Thread(target=metadata.serve_forever, daemon=True).start()
    yield Instance(tmp_path, metadata)
    metadata.shutdown()
    metadata.server_close()


@pytest.mark.parametrize("measured", [None, 412])
def test_a_release_with_the_parameters_digest_is_unpacked_installed_and_started(
    instance: Instance, measured: int | None
) -> None:
    if measured is not None:
        instance.value["measured_start_seconds"] = measured
    text = json.dumps(instance.value)
    instance.publish(text, instance.files)
    # A file of an earlier release, left on the volume by an earlier start.
    earlier = instance.root / "opt/rhi/release/units/rhi-earlier.service"
    earlier.parent.mkdir(parents=True)
    earlier.write_text("[Service]\n", encoding="utf-8")

    result = instance.run(PROGRAM)

    assert result.returncode == 0, result.stderr
    assert instance.units("start") == {"rhi-collector", "rhi-lifecycle"}
    # The settings are the ones the lifecycle agent reads from the same value.
    current = release.parse_current(text)
    settings = {f"RHI_{name.upper()}": value for name, value in vars(current.settings).items()}
    expected = settings | {
        "RHI_REGION": REGION,
        "RHI_RELEASE_PARAMETER": PARAMETER,
        "RHI_ENGINE_IMAGE": current.release.engine_image,
    }
    if measured is not None:
        expected["RHI_MEASURED_START"] = str(measured)
    lines = (instance.root / "run/rhi/release.env").read_text(encoding="utf-8").splitlines()
    assert dict(line.split("=", 1) for line in lines) == expected
    assert len(lines) == len(expected)
    # The archive is all there is under /opt/rhi, and only the top of units/ is installed.
    assert instance.files_under("opt/rhi") == {f"release/{name}" for name in MEMBERS}
    for name, content in MEMBERS.items():
        assert (instance.root / "opt/rhi/release" / name).read_text(encoding="utf-8") == content
    assert instance.files_under("run/systemd/system") == TOP_UNITS
    for name in TOP_UNITS:
        assert (instance.root / "run/systemd/system" / name).read_text(encoding="utf-8") == MEMBERS[f"units/{name}"]
    # systemd had read the installed units, and the settings were written, before the start.
    commands = [call[0] for call in instance.calls()]
    assert commands.index("daemon-reload") < commands.index("start")
    at_start = set((instance.state / "at-start").read_text(encoding="utf-8").split())
    assert {*TOP_UNITS, "release.env", "engine-flags"} <= at_start


def test_the_mock_pool_gets_no_gpu_unit(instance: Instance) -> None:
    # A mock pool's instance: its PCI bus lists a network adapter, and no device of NVIDIA's.
    instance.pci_device("0000:00:05.0", AMAZON)
    assert instance.run(PROGRAM).returncode == 0
    assert instance.files_under("run/systemd/system") == TOP_UNITS
    assert instance.units("start") == {"rhi-collector", "rhi-lifecycle"}

    # The same release on a GPU instance, whose bus lists one more device: the drop-in is
    # installed in the folder of its unit, before systemd reads the units. No other subfolder is.
    instance.stop()
    instance.pci_device("0000:00:1e.0", NVIDIA)
    assert instance.run(PROGRAM).returncode == 0
    assert instance.files_under("run/systemd/system") == {*TOP_UNITS, GPU_DROP_IN}
    installed = (instance.root / "run/systemd/system" / GPU_DROP_IN).read_text(encoding="utf-8")
    assert installed == MEMBERS[f"units/gpu/{GPU_DROP_IN}"]
    assert GPU_DROP_IN.split("/")[0] in (instance.state / "at-start").read_text(encoding="utf-8").split()


def test_the_engine_flags_reach_the_unit(instance: Instance) -> None:
    three = ["--served-model-name", "a name of four words", "--max-model-len=65536"]
    for flags in (three, []):
        instance.value["engine_flags"] = flags
        instance.publish(json.dumps(instance.value), instance.files)

        assert instance.run(PROGRAM).returncode == 0
        # In order, each one argument; with none, the command of the image alone.
        assert instance.engine_arguments() == flags


def test_the_tag_and_the_region_are_read_with_a_session_token(instance: Instance) -> None:
    # The stand-in is IMDSv2 only: it answers no read that comes without the token.
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, whatever proxy is set
    with pytest.raises(urllib.error.HTTPError) as refused:
        direct.open(f"{instance.url}/latest/meta-data/{PLACEMENT}", timeout=10)
    assert refused.value.code == 401
    assert instance.metadata.reads == []

    assert instance.run(PROGRAM).returncode == 0
    assert set(instance.metadata.reads) == {TAG, PLACEMENT}
    # The parameter the tag names, and no other: with another name in the tag, nothing is read.
    instance.metadata.values[TAG] = "/rhi/pools/another/release"
    assert instance.run(PROGRAM).returncode != 0


def no_tag(instance: Instance) -> None:
    del instance.metadata.values[TAG]


def no_metadata(instance: Instance) -> None:
    with socket.socket() as closed:  # a port nothing listens on, once this socket is closed
        closed.bind(("127.0.0.1", 0))
        instance.url = f"http://127.0.0.1:{closed.getsockname()[1]}"


def no_parameter(instance: Instance) -> None:
    instance.publish(None, instance.files)


def not_json(instance: Instance) -> None:
    instance.publish("{", instance.files)


def no_secret_id(instance: Instance) -> None:
    del instance.value["settings"]["secret_id"]
    instance.publish(json.dumps(instance.value), instance.files)


def a_setting_of_two_lines(instance: Instance) -> None:
    instance.value["settings"]["log_group"] = "/rhi/coursework\nRHI_SECRET_ID=another"
    instance.publish(json.dumps(instance.value), instance.files)


def a_flag_of_two_lines(instance: Instance) -> None:
    instance.value["engine_flags"] = ["--max-model-len", "65536\n--another"]
    instance.publish(json.dumps(instance.value), instance.files)


def flags_that_are_no_list(instance: Instance) -> None:
    instance.value["engine_flags"] = "--max-model-len 65536"
    instance.publish(json.dumps(instance.value), instance.files)


def no_archive(instance: Instance) -> None:
    instance.publish(json.dumps(instance.value), None)


def one_byte_changed(instance: Instance) -> None:
    at = instance.files.index(b"the lifecycle agent")  # inside a unit file, so the archive still unpacks
    changed = instance.files[:at] + bytes([instance.files[at] ^ 1]) + instance.files[at + 1 :]
    with tarfile.open(fileobj=io.BytesIO(changed)) as tar:
        assert tar.getnames() == list(MEMBERS)
    instance.publish(json.dumps(instance.value), changed)


FAILURES = [
    no_tag, no_metadata, no_parameter, not_json, no_secret_id, a_setting_of_two_lines, a_flag_of_two_lines,
    flags_that_are_no_list, no_archive, one_byte_changed,
]  # fmt: skip


@pytest.mark.parametrize("fails", FAILURES, ids=lambda fails: fails.__name__)
def test_a_start_that_fails_starts_no_unit(instance: Instance, fails: Callable[[Instance], None]) -> None:
    # An earlier start succeeded, and the instance stopped: its release is still on the volume.
    assert instance.run(PROGRAM).returncode == 0
    instance.stop()
    fails(instance)

    result = instance.run(PROGRAM)

    assert result.returncode != 0
    assert result.stderr.startswith("rhi-boot: ") or "\nrhi-boot: " in result.stderr
    assert instance.calls() == []
    assert not (instance.root / "run/systemd/system").exists()
    assert instance.files_under("opt/rhi") == set()


def boot_unit() -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None)
    unit.optionxform = str  # ty: ignore[invalid-assignment] the documented way to keep the keys' case
    unit.read_string(UNIT.read_text(encoding="utf-8"))
    return unit


def test_user_data_installs_the_boot_unit_and_the_boot_program_and_nothing_else(instance: Instance) -> None:
    result = instance.run(USER_DATA)

    assert result.returncode == 0, result.stderr
    unit = boot_unit()
    installed = {"etc/systemd/system/rhi-boot.service": UNIT, unit["Service"]["ExecStart"].lstrip("/"): PROGRAM}
    assert instance.files_under("") == set(installed)
    for name, source in installed.items():
        assert (instance.root / name).read_bytes() == source.read_bytes(), f"{name} is not a copy of {source.name}"
    assert os.access(instance.root / unit["Service"]["ExecStart"].lstrip("/"), os.X_OK)
    # Enabled, the unit is part of every boot; started, it also runs on this first one.
    assert {call[0] for call in instance.calls()} == {"daemon-reload", "enable", "start"}
    assert instance.units("enable") == instance.units("start") == {"rhi-boot"}
    # EC2 takes no more than 16 KB of user data.
    assert USER_DATA.stat().st_size <= 16384


def test_the_unit_runs_the_program_at_every_boot() -> None:
    # A restart from the warm pool is a boot: the unit is part of each one, with no condition on it.
    unit = boot_unit()
    assert unit["Service"]["Type"] == "oneshot"
    assert unit["Install"]["WantedBy"] == "multi-user.target"
    assert "network-online.target" in unit["Unit"]["After"].split()
    assert not [key for key in unit["Unit"] if key.startswith(("Condition", "Assert"))]


def test_the_boot_program_imports_only_the_standard_library() -> None:
    programs = re.findall(r"<<'PY'.*?\n(.*?)\nPY\n", PROGRAM.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert programs
    for program in programs:
        # Ubuntu 22.04, the GPU image's system, has Python 3.10.
        tree = ast.parse(program, feature_version=(3, 10))
        imported = {
            alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        }
        imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert imported
        assert imported <= sys.stdlib_module_names


@pytest.mark.parametrize("script", [USER_DATA, PROGRAM], ids=lambda script: script.name)
def test_each_script_parses(script: Path) -> None:
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0


# What user data never holds, as far as a pattern can tell: a setting comes from the parameter.
FAMILIES = "qwen|llama|mistral|mixtral|gemma|deepseek|gpt|glm|kimi|claude|codestral|devstral|granite|nemotron"
OPAQUE = "[A-Za-z0-9+/_=]"
SETTINGS = {
    "a digest": re.compile(r"sha256:|[0-9a-fA-F]{32,}"),
    # A dotted number, or the tag of an image.
    "a version": re.compile(r"\d+\.\d+|(?<![\w/])[\w.-]+/[\w.-]+:\w"),
    # A known family, or `publisher/model` where the model's name has a number.
    "a model name": re.compile(
        rf"(?i:(?<![a-z])(?:{FAMILIES}))|(?<![\w./$:-])[A-Za-z][\w.-]*/[A-Za-z][\w.-]*\d[\w.-]*(?![\w/])"
    ),
    "a secret": re.compile(
        r"(?i:bearer\s+[\w.~+/-]{8,})|rhi_[A-Za-z0-9]{46}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"|(?i:(?:password|passwd|secret|token|api_?key)\w*[\"']?\s*[:=]\s*[\"']?[\w+/.-]{8,})"
        rf"|(?={OPAQUE}*[a-z])(?={OPAQUE}*[A-Z])(?={OPAQUE}*\d){OPAQUE}{{32,}}"
    ),
}
# The one dotted number the boot program holds: the address of the instance metadata, on every instance.
METADATA_ADDRESS = "169.254.169.254"


def settings_in(text: str) -> set[str]:
    text = text.replace(METADATA_ADDRESS, "")
    return {kind for kind, pattern in SETTINGS.items() if pattern.search(text)}


def test_user_data_holds_no_settings() -> None:
    # The whole text, with its copies of the unit and of the boot program.
    assert settings_in(USER_DATA.read_text(encoding="utf-8")) == set()


HEXADECIMAL = hashlib.sha256(b"an example").hexdigest()
RANDOM = base64.b64encode(hashlib.sha512(b"an example").digest()).decode()
LINES = {
    "an image digest": ("a digest", f"IMAGE=repository@sha256:{HEXADECIMAL}"),
    "a bare digest": ("a digest", f"echo {HEXADECIMAL} release.tar | sha256sum --check"),
    "a pinned version": ("a version", "pip install " + "==".join(["engine", "0.11.2"])),
    "an image tag": ("a version", "docker pull registry/engine:nightly"),
    "a model family": ("a model name", "--served-model-name qwen-coursework"),
    "a model identifier": ("a model name", "--model example-lab/Example-27B-Instruct"),
    "a bearer value": ("a secret", f"curl --header 'Authorization: Bearer {RANDOM[:24]}'"),
    "a student key": ("a secret", "KEY=rhi_" + re.sub(r"\W", "", RANDOM)[:46]),
    "a private key": ("a secret", "-----BEGIN " + "PRIVATE KEY-----"),
    "a password": ("a secret", "ENGINE_PASSWORD=" + "-".join(["correct", "horse"])),
    "an opaque value": ("a secret", f"KEY={RANDOM}"),
}


@pytest.mark.parametrize(("kind", "line"), LINES.values(), ids=LINES)
def test_user_data_that_holds_a_setting_is_reported(kind: str, line: str) -> None:
    assert kind in settings_in(USER_DATA.read_text(encoding="utf-8") + line + "\n")


ARCHITECTURES = ("arm64", "aarch64", "amd64", "x86_64")


def architectures_in(text: str) -> set[str]:
    return {name for name in ARCHITECTURES if name in text.lower()}


def test_no_architecture_is_named() -> None:
    for path in (USER_DATA, PROGRAM, UNIT):
        assert architectures_in(path.read_text(encoding="utf-8")) == set(), path.name


@pytest.mark.parametrize("path", [USER_DATA, PROGRAM], ids=lambda path: path.name)
@pytest.mark.parametrize("name", ARCHITECTURES)
def test_a_named_architecture_is_reported(path: Path, name: str) -> None:
    line = f"curl https://example.com/tools/agent-{name.upper()}.tar\n"
    assert architectures_in(path.read_text(encoding="utf-8") + line) == {name}
