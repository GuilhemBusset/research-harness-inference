"""The app synthesizes on a machine that has never seen an AWS account (system design, sections 8.3 and 10).

It names no account and no region, looks nothing up, and writes only where it is told to. `cdk.json`
holds CDK feature flags and nothing that could name an account, a region or a resource. `app.py`
finds every package of the repository by itself, since nothing installs the project.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi.config import ConfigError

if TYPE_CHECKING:
    from types import ModuleType

    import aws_cdk as cdk

REPO = Path(__file__).resolve().parents[2]
CDK_JSON = REPO / "cdk.json"

# Twelve digits that stand alone. Inside a longer run of letters and digits, such as the hash of an
# asset, they are not an account ID.
ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
# A region name of any partition, such as the two in `test_the_detectors_find_what_they_look_for`.
REGION_NAME = re.compile(
    r"(?<![0-9A-Za-z])(?:us-gov|us-iso[a-z]?|eusc-[a-z]+|af|ap|ca|cn|eu|il|me|mx|sa|us)"
    r"-(?:north|south|east|west|central|northeast|northwest|southeast|southwest)-\d(?![0-9A-Za-z])"
)


def manifests(outdir: Path) -> dict[str, dict[str, Any]]:
    """The manifest of the cloud assembly and of each nested assembly, by its path under `outdir`."""
    return {
        path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(outdir.rglob("manifest.json"))
    }


def known_flags(outdir: Path) -> set[str]:
    """The feature flags the pinned aws-cdk-lib knows: it lists them in the manifest of every app."""
    reports = [
        artifact["properties"]["flags"]
        for artifact in manifests(outdir)["manifest.json"]["artifacts"].values()
        if artifact["type"] == "cdk:feature-flag-report"
    ]
    assert len(reports) == 1
    return set(reports[0])


def cdk_json_problems(cdk_json: dict[str, Any], flags: set[str]) -> list[str]:
    """What `cdk.json` holds besides the app's command and feature flags; empty when nothing."""
    problems = [f"the key {key!r} is not `app` or `context`" for key in cdk_json if key not in ("app", "context")]
    if cdk_json.get("app") != "uv run --locked python app.py":
        problems.append("the app's command is not `uv run --locked python app.py`")
    for key, value in cdk_json.get("context", {}).items():
        if key not in flags:
            problems.append(f"the context key {key!r} is not a feature flag of the pinned aws-cdk-lib")
        elif not isinstance(value, bool):
            # Only a flag that is on or off is certain to name nothing.
            problems.append(f"the feature flag {key!r} is not true or false")
    return problems


def test_the_stage_holds_its_stacks_in_a_nested_assembly(
    templates: dict[str, dict[str, Any]], cloud_assembly: Path
) -> None:
    in_a_stage = [path for path in templates if "/" in path]
    assert in_a_stage
    for path in in_a_stage:
        # The stage's assembly is nested in the app's, where the pipeline will deploy it from.
        assert Path(path).parent.as_posix() + "/manifest.json" in manifests(cloud_assembly)


def test_the_detectors_find_what_they_look_for() -> None:
    seeded = json.dumps(
        {"Outputs": {"Bucket": {"Value": "assets-123456789012-eu-west-1"}, "Home": {"Value": "us-gov-west-1"}}}
    )
    assert ACCOUNT_ID.findall(seeded) == ["123456789012"]
    assert REGION_NAME.findall(seeded) == ["eu-west-1", "us-gov-west-1"]
    # The hash of an asset is neither.
    assert not ACCOUNT_ID.search("0a123456789012b4") and not REGION_NAME.search("0a123456789012b4")


def test_no_template_holds_an_account_or_a_region(templates: dict[str, dict[str, Any]]) -> None:
    assert templates
    for path, template in templates.items():
        text = json.dumps(template)
        assert ACCOUNT_ID.findall(text) == [], path
        assert REGION_NAME.findall(text) == [], path


def test_no_stack_has_an_environment_and_nothing_is_looked_up(
    templates: dict[str, dict[str, Any]], cloud_assembly: Path
) -> None:
    stacks = 0
    for path, manifest in manifests(cloud_assembly).items():
        # A context lookup the app could not answer is recorded here, for the CLI to make.
        assert "missing" not in manifest, path
        for artifact in manifest["artifacts"].values():
            if artifact["type"] == "aws:cloudformation:stack":
                stacks += 1
                assert artifact["environment"] == "aws://unknown-account/unknown-region", path
    assert stacks == len(templates)


def test_synthesis_writes_only_to_its_temporary_directory(
    written_to_the_tree: list[str], cloud_assembly: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    assert written_to_the_tree == []
    assert not (REPO / "cdk.context.json").exists()
    assert tmp_path_factory.getbasetemp() in cloud_assembly.parents
    assert (cloud_assembly / "manifest.json").is_file()


def test_no_stack_is_bundled(app: cdk.App) -> None:
    assert app.node.try_get_context("aws:cdk:bundling-stacks") == []


def test_the_app_fails_closed_without_a_configuration_source(
    app_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RHI_CONFIG", raising=False)
    with pytest.raises(ConfigError):
        app_module.main()


def test_cdk_json_holds_flags_only(cloud_assembly: Path) -> None:
    flags = known_flags(cloud_assembly)
    cdk_json = json.loads(CDK_JSON.read_text(encoding="utf-8"))
    assert cdk_json["context"], "cdk.json sets no feature flag"
    assert cdk_json_problems(cdk_json, flags) == []

    a_flag = next(iter(cdk_json["context"]))
    for key, value in (
        ("account", "123456789012"),
        ("aws:cdk:bundling-stacks", []),
        ("@aws-cdk/core:noSuchFlag", True),
        (a_flag, "example.com"),
    ):
        grown = cdk_json | {"context": cdk_json["context"] | {key: value}}
        problems = cdk_json_problems(grown, flags)
        assert len(problems) == 1 and key in problems[0], problems
    # A setting beside the context is not a flag either.
    assert cdk_json_problems(cdk_json | {"versionReporting": False}, flags)
    assert cdk_json_problems(cdk_json | {"app": "python app.py"}, flags)


def test_the_app_sees_every_package_root(tmp_path: Path) -> None:
    assert REPO not in tmp_path.resolve().parents
    pytest_path = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"][
        "pythonpath"
    ]
    # `app.py` under another name than `__main__`: its path setup and its imports run, `main()`
    # does not. Unlike `python app.py`, this does not put the repository's root on the path.
    child = (
        "import json, runpy, sys\n"
        "runpy.run_path(sys.argv[1], run_name='app')\n"
        "import rhi_common\n"
        "print(json.dumps({'path': sys.path, 'rhi_common': rhi_common.__file__}))\n"
    )
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    # As in conftest.py: jsii's package cache cannot be used in the implement loop's sandbox.
    environment["JSII_RUNTIME_PACKAGE_CACHE"] = "disabled"

    result = subprocess.run(
        [sys.executable, "-c", child, str(REPO / "app.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.splitlines()[-1])
    assert pytest_path
    for folder in pytest_path:
        assert str(REPO / folder) in seen["path"], folder
    assert seen["rhi_common"] == str(REPO / "services/common/rhi_common/__init__.py")
