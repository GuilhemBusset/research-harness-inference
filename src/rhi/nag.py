"""The accepted cdk-nag findings of the app, acknowledged as the protected files list them
(system design, section 8.4, control 2).

cdk-nag's AWS Solutions pack runs inside synthesis and fails it on a finding (`app.py`). A finding
that is accepted is listed with its reason in the `[nag]` table of the acknowledgement file of the
module that created the resource, `tests/policy/acknowledged/<module>.toml`
(`tests/policy/acknowledged/README.md`). This module is the one place of the app that acknowledges
a finding: on each resource, the entries of its module's file and nothing else.
`tests/policy/test_nag_acknowledgements.py` reads what cdk-nag wrote into the templates and
refuses an acknowledgement the files do not list.

**A finding that holds a pool's name.** cdk-nag names a finding after the resource it found, so a
statement that names a pool's resource brings a finding of that pool's name, and no file of the
repository names the pools of a deployed configuration. An entry whose key holds `POOL` in place
of the name lists that finding for each pool of the stage's configuration, and for no other name
(`listed`).
"""

from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import aws_cdk as cdk
import jsii

from rhi.stage import EndpointStage

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from constructs import IConstruct

# The source tree whose modules create the app's resources, and the folder of their files.
SOURCE = Path(__file__).resolve().parent
ACKNOWLEDGED = SOURCE.parents[1] / "tests" / "policy" / "acknowledged"
# In the key of an entry, the place of a pool's name (`tests/policy/acknowledged/README.md`).
POOL = "<pool>"


def created_by(resource: cdk.CfnResource, source: Path) -> str | None:
    """The name of the acknowledgement file of the module that created the resource: the module
    that defines the class of the resource's nearest enclosing construct whose class is defined
    under `source`, as its path there with `/` replaced by `_` and without `.py`. None when no
    construct of the source tree encloses the resource."""
    for scope in reversed(resource.node.scopes[:-1]):
        try:
            defined_in = Path(inspect.getfile(type(scope))).resolve()
        except (TypeError, OSError):  # A class with no file: not one of the source tree.
            continue
        if defined_in.is_relative_to(source):
            return "_".join(defined_in.relative_to(source).with_suffix("").parts)
    return None


def listed(entries: Mapping[str, str], pools: Iterable[str]) -> dict[str, str]:
    """The findings a nag table lists for the pools of a configuration, each with its reason. An
    entry whose key holds `POOL` lists its finding once for each pool, under the key with the
    pool's name in place of `POOL`, and nothing where there is no pool. Any other entry lists the
    finding its key names."""
    pools = tuple(pools)
    return {
        finding: reason
        for key, reason in entries.items()
        for finding in ([key.replace(POOL, pool) for pool in pools] if POOL in key else [key])
    }


def pools_of(construct: IConstruct) -> tuple[str, ...]:
    """The names of the pools of the configuration of the stage that holds the construct. None
    outside the endpoint's stage: the pipeline's own stack has no pool."""
    stage = cdk.Stage.of(construct)
    return tuple(pool.name for pool in stage.config.pools) if isinstance(stage, EndpointStage) else ()


def assemblies(app: cdk.App) -> list[cdk.Stage]:
    """The app and every stage in it. Each is synthesized as an assembly of its own, with its own
    aspects: an aspect of the app does not visit what is inside a stage."""
    return [construct for construct in app.node.find_all() if isinstance(construct, cdk.Stage)]


@jsii.implements(cdk.IAspect)
class _Acknowledge:
    """Acknowledges on each resource it visits what the file of the resource's module lists."""

    def __init__(self, folder: Path, source: Path) -> None:
        self._source = source.resolve()
        # The nag table of every file, by the module name the file carries.
        self._listed: dict[str, dict[str, str]] = {
            path.stem: tomllib.loads(path.read_text(encoding="utf-8")).get("nag", {})
            for path in sorted(folder.glob("*.toml"))
        }

    def visit(self, node: IConstruct) -> None:
        if not isinstance(node, cdk.CfnResource):
            return
        module = created_by(node, self._source)
        entries = self._listed.get(module, {}) if module is not None else {}
        findings = listed(entries, pools_of(node))
        if findings:
            cdk.Validations.of(node).acknowledge(
                *(cdk.Acknowledgment(id=finding, reason=reason) for finding, reason in findings.items())
            )


def acknowledge(app: cdk.App, *, folder: Path = ACKNOWLEDGED, source: Path = SOURCE) -> None:
    """Has synthesis acknowledge, on each resource of the app, every entry of the nag table of the
    file of the module that created it, with that entry's reason. An entry for a pool's name is
    acknowledged once for each pool of the resource's stage (`listed`).

    The acknowledgement is made on the resource itself, never on a construct around it, so the
    entries of one module's file never reach a resource another module created. It is made during
    synthesis, by an aspect, so a resource that a construct creates only then has its own too. Call
    this once the app holds all its stages.
    """
    aspect = _Acknowledge(folder, source)
    for assembly in assemblies(app):
        cdk.Aspects.of(assembly).add(aspect)
