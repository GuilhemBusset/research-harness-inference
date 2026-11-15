"""The CDK app: `cdk.json` starts this file, and the tests build the same app.

The app is environment-agnostic (system design, section 10): it names no account and no region,
and it looks nothing up, so it synthesizes on a machine that has never seen an AWS account.
"""

from __future__ import annotations

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

import aws_cdk as cdk

from rhi import config as configuration
from rhi.stage import EndpointStage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rhi.config import Config


def build(config: Config, *, outdir: str | None = None, context: Mapping[str, Any] | None = None) -> cdk.App:
    """The app for one configuration. The CDK CLI gives the output directory and the context of
    `cdk.json` through the environment; a test gives its own."""
    # No version-reporting resource: in a stack without a region, its condition lists region names.
    app = cdk.App(outdir=outdir, context=context, analytics_reporting=False)
    EndpointStage(app, "Rhi", config=config)
    return app


def main() -> None:
    build(configuration.load()).synth()


if __name__ == "__main__":
    main()
