"""Independent execution and mutation checks for the instance boot deliverable."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOT = REPO / "services/engine/boot"
TESTS = REPO / "tests/unit/engine/test_boot.py"

STANDIN = '''#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

state = Path(os.environ["PROBE_STATE"])
config = json.loads((state / "config.json").read_text())
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with (state / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([tool, args]) + "\\n")
if tool == "curl":
    headers = [args[i + 1].lower() for i, value in enumerate(args) if value in ("--header", "-H")]
    if args[-1].endswith("/api/token"):
        assert "PUT" in args
        assert any(h.startswith("x-aws-ec2-metadata-token-ttl-seconds:") for h in headers)
        print("probe-session")
    else:
        assert "x-aws-ec2-metadata-token: probe-session" in headers
        if args[-1].endswith("/tags/instance/rhi-release-parameter"):
            if config.get("failure") == "tag":
                sys.exit(22)
            print(config["parameter"])
        elif args[-1].endswith("/placement/region"):
            print(config["region"])
        else:
            sys.exit(22)
elif tool == "aws":
    assert args[args.index("--region") + 1] == config["region"]
    if args[:2] == ["ssm", "get-parameter"]:
        assert args[args.index("--name") + 1] == config["parameter"]
        if config.get("failure") == "parameter":
            sys.exit(1)
        print(json.dumps({"Parameter": {"Value": json.dumps(config["value"])}}))
    elif args[:2] == ["s3", "cp"]:
        files = config["value"]["files"]
        assert args[-2] == "s3://" + files["bucket"] + "/" + files["key"]
        if config.get("failure") == "fetch":
            sys.exit(1)
        shutil.copyfile(state / "files.tar", args[-1])
    else:
        sys.exit(1)
elif tool != "systemctl":
    sys.exit(1)
'''


class Machine:
    def __init__(self, folder: Path) -> None:
        self.root, self.state, self.bin = folder / "root", folder / "state", folder / "bin"
        for path in (self.root, self.state, self.bin):
            path.mkdir()
        # Restrict external tools to the issue's list, plus the service manager it requires.
        for name in ("bash", "python3", "tar", "cat", "chmod", "mkdir", "rm", "mktemp", "mv", "install", "sha256sum"):
            executable = shutil.which(name)
            assert executable is not None, name
            (self.bin / name).symlink_to(executable)
        for name in ("curl", "aws", "systemctl"):
            path = self.bin / name
            path.write_text(STANDIN)
            path.chmod(0o755)
        self.config: dict[str, Any] = {
            "region": "eu-west-1", "parameter": "/example/pool/release",
            "value": {
                "engine_image": "example.com/engine@sha256:" + hashlib.sha256(b"image").hexdigest(),
                "model_revision": None, "engine_flags": [],
                "settings": {
                    "pool": "example", "group": "example-group", "launch_hook": "example-launch",
                    "termination_hook": "example-terminate", "log_group": "/example/log",
                    "workspace_url": "https://example.com/remote_write", "heartbeat_table": "example-heartbeats",
                    "secret_id": "example/engine",
                },
            },
        }

    def publish(self, label: str, *, measured: int | None = None) -> dict[str, str]:
        members = {
            "units/rhi-collector.service": "[Service]\nExecStart=/bin/true\n# " + label,
            "units/rhi-lifecycle.service": "[Service]\nExecStart=/bin/true\n# " + label,
            "units/nested/not-installed.service": "[Service]\n",
            "collector/config.yaml": "# " + label,
            "agent/main.py": "# " + label,
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as bundle:
            for name, content in members.items():
                member = tarfile.TarInfo(name)
                member.size = len(content.encode())
                bundle.addfile(member, io.BytesIO(content.encode()))
        data = buffer.getvalue()
        (self.state / "files.tar").write_bytes(data)
        self.config["value"]["files"] = {
            "bucket": "example-artifacts", "key": label + ".tar", "sha256": hashlib.sha256(data).hexdigest(),
        }
        self.config["value"]["settings"]["group"] = "example-" + label
        self.config["value"]["measured_start_seconds"] = measured
        return members

    def run(self, name: str = "rhi-boot.sh") -> subprocess.CompletedProcess[str]:
        (self.state / "config.json").write_text(json.dumps(self.config))
        environment = {
            "PATH": str(self.bin), "PROBE_STATE": str(self.state), "RHI_BOOT_ROOT": str(self.root),
            "RHI_BOOT_METADATA": "http://127.0.0.1:1",
        }
        return subprocess.run(
            [str(self.bin / "bash"), str(BOOT / name)], env=environment,
            text=True, capture_output=True, timeout=30, check=False,
        )

    def calls(self, tool: str) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [args for name, args in calls if name == tool]


def test_user_data_installs_only_the_two_sources(tmp_path: Path) -> None:
    machine = Machine(tmp_path)
    result = machine.run("user-data.sh")
    assert result.returncode == 0, result.stderr
    installed = {p.relative_to(machine.root).as_posix() for p in machine.root.rglob("*") if p.is_file()}
    expected = {"etc/systemd/system/rhi-boot.service", "usr/local/sbin/rhi-boot.sh"}
    assert installed == expected
    for path in installed:
        assert (machine.root / path).read_bytes() == (BOOT / Path(path).name).read_bytes()
    assert os.access(machine.root / "usr/local/sbin/rhi-boot.sh", os.X_OK)
    calls = machine.calls("systemctl")
    assert any("enable" in call and "rhi-boot.service" in call for call in calls)
    assert any("start" in call and "rhi-boot.service" in call for call in calls)
    assert not machine.calls("aws")


def test_each_boot_fetches_the_current_release_and_settings(tmp_path: Path) -> None:
    machine = Machine(tmp_path)
    unit = (BOOT / "rhi-boot.service").read_text()
    assert "WantedBy=multi-user.target" in unit
    assert "ExecStart=/usr/local/sbin/rhi-boot.sh" in unit
    assert "After=network-online.target" in unit
    assert "ConditionFirstBoot" not in unit
    for label, measured in (("first", 51), ("second", None)):
        if (machine.root / "run").exists():
            shutil.rmtree(machine.root / "run")
        members = machine.publish(label, measured=measured)
        result = machine.run()
        assert result.returncode == 0, result.stderr
        for name, text in members.items():
            assert (machine.root / "opt/rhi/release" / name).read_text() == text
        installed = machine.root / "run/systemd/system"
        assert {p.name for p in installed.iterdir()} == {"rhi-collector.service", "rhi-lifecycle.service"}
        settings = dict(line.split("=", 1) for line in (machine.root / "run/rhi/release.env").read_text().splitlines())
        expected = {"RHI_" + key.upper(): value for key, value in machine.config["value"]["settings"].items()}
        expected.update({
            "RHI_REGION": machine.config["region"], "RHI_RELEASE_PARAMETER": machine.config["parameter"],
            "RHI_ENGINE_IMAGE": machine.config["value"]["engine_image"],
        })
        if measured is not None:
            expected["RHI_MEASURED_START"] = str(measured)
        assert settings == expected
    starts = [call for call in machine.calls("systemctl") if "start" in call]
    assert len(starts) == 2
    for call in starts:
        assert {part.removesuffix(".service") for part in call if not part.startswith("--")} == {
            "start", "rhi-collector", "rhi-lifecycle",
        }


@pytest.mark.parametrize("failure", ["tag", "parameter", "fetch", "digest"])
def test_failures_do_not_install_or_start_release_units(tmp_path: Path, failure: str) -> None:
    machine = Machine(tmp_path)
    machine.publish("failure")
    machine.config["failure"] = failure
    if failure == "digest":
        path = machine.state / "files.tar"
        data = bytearray(path.read_bytes())
        data[1024] ^= 1
        path.write_bytes(data)
    result = machine.run()
    assert result.returncode != 0
    assert not any("start" in call for call in machine.calls("systemctl"))
    assert not (machine.root / "run/systemd/system").exists()
    assert not (machine.root / "opt/rhi/release").exists()


def test_named_guards_reject_actual_text_mutations(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(TESTS))
    guard = namespace["test_user_data_holds_no_settings"]
    guard()
    original = (BOOT / "user-data.sh").read_text()
    changed = tmp_path / "user-data.sh"
    guard.__globals__["USER_DATA"] = changed
    for line in (
        "IMAGE=example.com/engine@sha256:" + hashlib.sha256(b"mutation").hexdigest(),
        "VERSION=8.7.6", "MODEL=Qwen/Example-9B", "ENGINE_PASSWORD=example-password",
    ):
        changed.write_text(original + "\n" + line + "\n")
        with pytest.raises(AssertionError):
            guard()
    # Each spelling must make the named architecture test fail in either script.
    guard = namespace["test_no_architecture_is_named"]
    guard.__globals__["USER_DATA"] = BOOT / "user-data.sh"
    guard()
    for variable, filename in (("USER_DATA", "user-data.sh"), ("PROGRAM", "rhi-boot.sh")):
        source = BOOT / filename
        for architecture in ("arm64", "aarch64", "amd64", "x86_64"):
            changed.write_text(source.read_text() + "\n# " + architecture + "\n")
            guard.__globals__[variable] = changed
            with pytest.raises(AssertionError):
                guard()
        guard.__globals__[variable] = source


def test_scripts_parse_and_embedded_python_is_standard_library() -> None:
    for name in ("rhi-boot.sh", "user-data.sh"):
        result = subprocess.run(["bash", "-n", str(BOOT / name)], check=False)
        assert result.returncode == 0
        source = (BOOT / name).read_text()
        assert not re.search("arm64|aarch64|amd64|x86_64", source, re.IGNORECASE)
    source = (BOOT / "rhi-boot.sh").read_text()
    python = re.findall(r"<<'PY'[^\n]*\n(.*?)\nPY\n", source, re.DOTALL)
    assert python
    for program in python:
        tree = ast.parse(program, feature_version=(3, 10))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] in sys.stdlib_module_names


def test_committed_temporary_root_harness_passes() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(TESTS)], cwd=REPO,
        text=True, capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
