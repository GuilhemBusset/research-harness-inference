"""The pipeline's synth step (system design, section 8.2, step 2).

The synth step is the one place that knows the real configuration. In this order, it:

1. installs a pinned uv and the CDK CLI of `package-lock.json`;
2. runs `uv sync --locked`;
3. fetches the configuration from SSM into the file RHI_CONFIG_FILE names
   (`scripts/pipeline/fetch_config.py`);
4. runs the leak check: no value of a private key of that configuration is in the source tree
   (`scripts/pipeline/leak_check.py`; section 10);
5. synthesizes with lookups off, from that file (`RHI_CONFIG=file`), bundling the functions;
6. runs the tests, which read the example configuration whatever the environment.

**The fetched file is outside the source tree.** The source the pipeline hands CodeBuild has no
`.git`, so the leak check cannot tell an untracked file from a tracked one, and would find a file
written in the checkout. RHI_CONFIG_FILE names a file in the build's temporary directory instead.

**A git work tree for the tests.** For the same reason the tests would find no git work tree, and
some read the tree through git (the hygiene scan of the repository, for one). Before them the step
makes the checkout one, with no commit: git then lists every file of the source as untracked, and
leaves out what `.gitignore` names, which is what the build added.

**The import path.** The project is not installed, so a script sees only its own folder. Every
command gets the folders `pyproject.toml` puts on pytest's import path in PYTHONPATH, read when
the app synthesizes, as `app.py` puts them on its own path. They are relative to the checkout,
the folder every command of the step runs in.

**The role** may read the parameters under the configuration prefix and nothing else of SSM, and
no secret: `tests/policy/test_pipeline.py` holds it to that.

The step is a step, not a construct: CDK Pipelines creates its project and role inside the
pipeline construct of `rhi.pipeline.stack`, whose acknowledgement file covers them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import pipelines

from rhi import config

# The uv the step installs: the one CI pins (`.github/workflows/ci.yml`).
UV_VERSION = "0.11.28"
# The file the configuration is fetched into: in the build's temporary directory, outside the
# checkout, which CodeBuild places under its own source folder.
CONFIG_FILE = "/tmp/rhi-config.toml"

INSTALL_COMMANDS = [f"python3 -m pip install uv=={UV_VERSION}", "npm ci --ignore-scripts"]
COMMANDS = [
    "uv sync --locked",
    "uv run --locked python scripts/pipeline/fetch_config.py",
    "uv run --locked python scripts/pipeline/leak_check.py",
    "RHI_CONFIG=file node_modules/.bin/cdk synth --lookups=false",
    "git init --quiet",
    "uv run --locked pytest",
]


def import_path(pyproject: Path = config.REPO_ROOT / "pyproject.toml") -> list[str]:
    """The folders `pyproject.toml` puts on pytest's import path, relative to the repository root."""
    settings = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return list(settings["tool"]["pytest"]["ini_options"]["pythonpath"])


def config_reader(scope: cdk.Stack) -> iam.PolicyStatement:
    """The one read the step makes of SSM: every parameter under the configuration prefix, which is
    what the `ssm` source asks for, by path."""
    prefix = config.SSM_PREFIX.strip("/")
    parameters = f"arn:{scope.partition}:ssm:{scope.region}:{scope.account}:parameter/{prefix}"
    return iam.PolicyStatement(actions=["ssm:GetParametersByPath"], resources=[parameters, f"{parameters}/*"])


def synth_step(scope: cdk.Stack, source: pipelines.IFileSetProducer) -> pipelines.CodeBuildStep:
    """The synth step of the pipeline of `scope`, on `source`."""
    return pipelines.CodeBuildStep(
        "Synth",
        input=source,
        install_commands=INSTALL_COMMANDS,
        commands=COMMANDS,
        env={"RHI_CONFIG_FILE": CONFIG_FILE, "PYTHONPATH": ":".join(import_path())},
        role_policy_statements=[config_reader(scope)],
    )
