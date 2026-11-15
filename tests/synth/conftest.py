"""The app, synthesized once for the tests that read its templates.

`synthesized` builds the app of `app.py` from the example configuration and synthesizes it into a
temporary directory, as `cdk synth` would with the context of `cdk.json`, with two differences:
nothing is bundled, so no test needs the package index, and nothing comes from the environment
the tests run in. A test asks for what it reads by a fixture's name: `templates`, `cloud_assembly`
or `app`. pytest finds a fixture by its name, so a test file imports nothing from here.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

    import aws_cdk as cdk

REPO = Path(__file__).resolve().parents[2]

# Folders the tools write to while the tests run, whatever the tests do.
NOT_THE_TREE = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".ty_cache"}


@dataclass(frozen=True)
class Synthesized:
    """The app after synthesis, and what synthesis wrote."""

    app: cdk.App
    outdir: Path
    # By path under `outdir`. A stage's templates are in the folder of its assembly.
    templates: dict[str, dict[str, Any]]
    written_to_the_tree: list[str]


def tree() -> dict[str, tuple[int, int]]:
    """Every file of the repository, with its size and the time it was last written."""
    files = {}
    for folder, subfolders, names in os.walk(REPO):
        subfolders[:] = [name for name in subfolders if name not in NOT_THE_TREE]
        for name in names:
            path = Path(folder, name)
            status = path.lstat()
            files[path.relative_to(REPO).as_posix()] = (status.st_size, status.st_mtime_ns)
    return files


@pytest.fixture(scope="session")
def app_module() -> ModuleType:
    """`app.py`, run as a module: its path setup and its imports, without `main()`."""
    spec = importlib.util.spec_from_file_location("app", REPO / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        # Importing the CDK starts jsii, which refuses a package cache below a directory that
        # another user owns: that is what the implement loop's sandbox gives it. Without the cache
        # it unpacks the CDK into the system's temporary directory.
        patch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def synthesized(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Synthesized:
    """The one synthesis of a test session. The fixtures below give its parts."""
    outdir = tmp_path_factory.mktemp("cdk.out")
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {
        # The CDK CLI asks every app for the resource that reports the CDK's version. The tests
        # ask too, so that they see what the app answers.
        "aws:cdk:version-reporting": True,
        # No stack is bundled: a function's bundle is built from the package index.
        "aws:cdk:bundling-stacks": [],
    }
    before = tree()
    app = app_module.build(config.load({"RHI_CONFIG": "example"}), outdir=str(outdir), context=context)
    app.synth()
    after = tree()
    return Synthesized(
        app=app,
        outdir=outdir,
        templates={
            path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(outdir.rglob("*.template.json"))
        },
        written_to_the_tree=sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        ),
    )


@pytest.fixture(scope="session")
def app(synthesized: Synthesized) -> cdk.App:
    """The app, after synthesis: its construct tree and its context."""
    return synthesized.app


@pytest.fixture(scope="session")
def cloud_assembly(synthesized: Synthesized) -> Path:
    """The directory of the cloud assembly: its manifest, and a folder for each stage's assembly."""
    return synthesized.outdir


@pytest.fixture(scope="session")
def templates(synthesized: Synthesized) -> dict[str, dict[str, Any]]:
    """Every template of the cloud assembly, those of nested stage assemblies included, by its
    path under `cloud_assembly`. The session shares them: a test does not change one."""
    return synthesized.templates


@pytest.fixture(scope="session")
def written_to_the_tree(synthesized: Synthesized) -> list[str]:
    """The files of the repository that synthesis created, changed or removed."""
    return synthesized.written_to_the_tree
