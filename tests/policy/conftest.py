"""What the invariants read: the app's templates, the module that created each resource, and the
acknowledgements (system design, section 8.4, control 2).

This folder is a protected path, and it synthesizes the app itself, so that no unprotected file
decides which templates the invariants see or which module a resource belongs to. The app of
`app.py` is built from the example configuration with the context of `cdk.json`, and nothing is
bundled: no test builds a function bundle or needs the network, and a skipped bundle changes no
IAM statement.

A test asks for what it reads by a fixture's name; pytest finds a fixture by its name, so a test
file imports nothing from here:

- `templates`: every template of the cloud assembly, those of nested stage assemblies included,
  by its path under the assembly;
- `created_by`: for every resource of those templates, by `(template path, logical ID)`, the name
  of the acknowledgement file of the module that created it (`acknowledged/README.md`);
- `module_names`: the name every module of `src/rhi/` gives its acknowledgement file;
- `acknowledged` and `acknowledged_folder`: the files of `acknowledged/`, read, and the folder;
- `cdk`, `read_app` and `read_acknowledged`: the CDK library and the two readers, for a test that
  builds its own app or its own folder to show that a check can fail.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    import aws_cdk
    from constructs import IConstruct

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "rhi"
ACKNOWLEDGED = Path(__file__).resolve().parent / "acknowledged"

Templates = dict[str, dict[str, Any]]
CreatedBy = dict[tuple[str, str], str]


def module_name(module: Path, source: Path) -> str:
    """The name a module gives its acknowledgement file: its path under the source tree, with `/`
    replaced by `_` and without `.py`."""
    return "_".join(module.relative_to(source).with_suffix("").parts)


def defining_module(construct: IConstruct, source: Path) -> str | None:
    """The module that defines the construct's class, when it is one of the source tree."""
    try:
        defined_in = Path(inspect.getfile(type(construct))).resolve()
    except (TypeError, OSError):  # A class with no file: not one of the source tree.
        return None
    return module_name(defined_in, source) if defined_in.is_relative_to(source) else None


def read(app: aws_cdk.App, source: Path) -> tuple[Templates, CreatedBy]:
    """Synthesizes the app, and gives its templates and the module that created each resource.

    A resource belongs to the module that defines the class of its nearest enclosing construct
    whose class is defined under `source`. The construct tree says which construct encloses a
    resource, and a construct made by this Python process keeps its Python class. A resource of
    a template with no such construct fails the read, and so every test that reads the app.
    """
    import aws_cdk as cdk

    source = source.resolve()
    outdir = Path(app.synth().directory)
    templates = {
        path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(outdir.rglob("*.template.json"))
    }
    created_by: CreatedBy = {}
    for construct in app.node.find_all():
        if not isinstance(construct, cdk.CfnResource):
            continue
        stack = cdk.Stack.of(construct)
        assembly = cdk.Stage.of(stack)
        assert assembly is not None, f"the stack of {construct.node.path} is in no assembly"
        template = (Path(assembly.outdir) / stack.template_file).relative_to(outdir).as_posix()
        enclosing = reversed(construct.node.scopes[:-1])
        modules = [module for scope in enclosing if (module := defining_module(scope, source)) is not None]
        if modules:
            created_by[template, stack.resolve(construct.logical_id)] = modules[0]
    without_a_module = sorted(
        f"{path}: {logical_id}"
        for path, template in templates.items()
        for logical_id in template.get("Resources", {})
        if (path, logical_id) not in created_by
    )
    assert not without_a_module, f"no construct of the source tree encloses these resources: {without_a_module}"
    return templates, created_by


def read_folder(folder: Path) -> dict[str, dict[str, Any]]:
    """Every acknowledgement file of a folder, by the module name it carries."""
    return {path.stem: tomllib.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.toml"))}


@pytest.fixture(scope="session")
def cdk() -> ModuleType:
    """The CDK library, `aws_cdk`. A test file does not import it at its top: this import starts
    jsii, which refuses a package cache below a directory that another user owns, and that is
    what the implement loop's sandbox gives it. Without the cache it unpacks the CDK into the
    system's temporary directory."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
        import aws_cdk

    return aws_cdk


@pytest.fixture(scope="session")
def synthesized(cdk: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> tuple[Templates, CreatedBy]:
    """The one synthesis of the app in a test session, read by `read`."""
    spec = importlib.util.spec_from_file_location("app", REPO / "app.py")
    assert spec is not None and spec.loader is not None
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {
        # No stack is bundled.
        "aws:cdk:bundling-stacks": [],
    }
    outdir = tmp_path_factory.mktemp("cdk.out")
    app = app_module.build(config.load({"RHI_CONFIG": "example"}), outdir=str(outdir), context=context)
    templates, created_by = read(app, SOURCE)
    assert templates, "the app synthesized no template"
    return templates, created_by


@pytest.fixture(scope="session")
def templates(synthesized: tuple[Templates, CreatedBy]) -> Templates:
    """Every template of the app: the pipeline's own stack once it exists, and every stack of the
    stage. The session shares them: a test does not change one."""
    return synthesized[0]


@pytest.fixture(scope="session")
def created_by(synthesized: tuple[Templates, CreatedBy]) -> CreatedBy:
    """The module that created each resource of `templates`, by `(template path, logical ID)`."""
    return synthesized[1]


@pytest.fixture(scope="session")
def module_names() -> set[str]:
    """The name each module of the app's source tree gives its acknowledgement file."""
    return {module_name(path, SOURCE) for path in SOURCE.rglob("*.py")}


@pytest.fixture(scope="session")
def acknowledged_folder() -> Path:
    """The folder of the acknowledgement files."""
    return ACKNOWLEDGED


@pytest.fixture(scope="session")
def acknowledged() -> dict[str, dict[str, Any]]:
    """The tables of every acknowledgement file, by module name. `test_acknowledged.py` holds
    the files to their format."""
    return read_folder(ACKNOWLEDGED)


@pytest.fixture(scope="session")
def read_app(cdk: ModuleType) -> Callable[[aws_cdk.App, Path], tuple[Templates, CreatedBy]]:
    """`read`, for an app and a source tree built in a test."""
    return read


@pytest.fixture(scope="session")
def read_acknowledged() -> Callable[[Path], dict[str, dict[str, Any]]]:
    """`read_folder`, for a folder built in a test."""
    return read_folder
