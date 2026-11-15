"""The CDK app: `cdk.json` starts this file, and the tests build the same app.

The app is environment-agnostic (system design, section 10): it names no account and no region,
and it looks nothing up, so it synthesizes on a machine that has never seen an AWS account.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parent


def package_roots() -> list[Path]:
    """The folders `pyproject.toml` puts on pytest's import path: one list for the tests and the app."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [REPO_ROOT / folder for folder in pyproject["tool"]["pytest"]["ini_options"]["pythonpath"]]


# The project is not installed, and Python gives a script its own folder and nothing else. So the
# package roots go on the import path here, in pytest's order, before anything of the repository
# is imported.
sys.path[:0] = [str(root) for root in package_roots() if str(root) not in sys.path]

# No reliance on an existing cache (AGENTS.md, "Toolchain"): jsii's package cache lives under the
# user's home folder, and jsii refuses to start when that folder's owners do not suit it, as in a
# sandbox. The runtime reads this when `aws_cdk` is first imported.
os.environ.setdefault("JSII_RUNTIME_PACKAGE_CACHE", "disabled")

import aws_cdk as cdk
import cdk_nag

from rhi import config as configuration
from rhi import nag
from rhi.stage import EndpointStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rhi.config import Config


def add_nag(app: cdk.App, *, acknowledged: Path = nag.ACKNOWLEDGED, source: Path = nag.SOURCE) -> None:
    """Runs cdk-nag's AWS Solutions pack inside the synthesis of the app, as a policy-validation
    plugin: a finding fails synthesis (system design, section 8.2, step 2), unless the file of the
    module that created the resource acknowledges it (`rhi.nag`).

    cdk-nag writes every acknowledgement into the template, in the metadata of the resource it
    covers. `tests/policy/test_nag_acknowledgements.py` reads them there, so that nothing is
    acknowledged that the protected files do not list. `acknowledged` and `source` are the folder
    of those files and the source tree of the modules that name them; a test gives its own.
    """
    # First, so that on each resource the acknowledgements are made before they are written.
    nag.acknowledge(app, folder=acknowledged, source=source)
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app, write_suppressions_to_cloud_formation=True))
    # The pack writes through an aspect of the app, which does not visit what is inside a stage:
    # each stage gets the same aspect.
    for assembly in nag.assemblies(app):
        if assembly.parent_stage is not None:
            cdk.Aspects.of(assembly).add(cdk_nag.WriteNagSuppressionsToCloudFormationAspect())


def build(config: Config, *, outdir: str | None = None, context: Mapping[str, Any] | None = None) -> cdk.App:
    """The app for one configuration. The CDK CLI gives the output directory and the context of
    `cdk.json` through the environment; a test gives its own."""
    # No version-reporting resource: in a stack without a region, its condition lists region names.
    app = cdk.App(outdir=outdir, context=context, analytics_reporting=False)
    EndpointStage(app, "Rhi", config=config)
    # Last: it reaches the stages the app holds by now.
    add_nag(app)
    return app


def main() -> None:
    build(configuration.load()).synth()


if __name__ == "__main__":
    main()
