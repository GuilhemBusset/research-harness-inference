"""Builds every dashboard of the package from a configuration, each into one JSON file.

A dashboard is one module of this package. The build lists none: it takes every module that
defines, itself, these two names.

- `dashboard(config)` returns the Grafana Foundation SDK's dashboard builder
  (`grafana_foundation_sdk.builders.dashboard.Dashboard`), with a fixed identifier (`uid`) and a
  title; or a tuple of them, the dashboard and a companion view of its numbers that needs a time
  range of its own (Grafana gives every panel of a dashboard the dashboard's fixed dates). `config` is the configuration (`rhi.config.Config`): a pool is one of its entries and one
  row of a dashboard (system design, section 4.2), so a module builds its rows from
  `config.pools`, with the helpers of `rhi_dashboards.rules`.
- `METRICS` declares where each metric name the dashboard queries comes from
  (`rhi_dashboards.rules.Declared`).

Adding a dashboard is adding its module. `write` puts each dashboard into the folder it is given,
as `<uid>.json`.

**The same configuration gives the same bytes.** Grafana's image holds these files, and a build
that differed from one run to the next would rebuild the image on every deployment. So modules
are taken in the order of their names and pools in the configuration's order, the build numbers
the panels in the order they were added, JSON keys are sorted, and nothing of the run (a time, a
random identifier, a path) is written.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import dashboard as models

import rhi_dashboards
from rhi_dashboards import rules

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from rhi.config import Config

# An identifier is also the file's name, and part of the dashboard's address in Grafana.
_UID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}", re.ASCII)


class BuildError(Exception):
    """A module's dashboard cannot be built into the folder. The message names the module."""


@dataclass(frozen=True)
class Built:
    """One dashboard: the module that defines it, the SDK's model and the module's declarations."""

    module: str
    model: models.Dashboard
    metrics: tuple[rules.Declared, ...]

    @property
    def uid(self) -> str:
        return str(self.model.uid)

    @property
    def title(self) -> str:
        return str(self.model.title)

    def text(self) -> str:
        """The dashboard's file: its JSON, the same for the same model."""
        return json.dumps(self.model, cls=JSONEncoder, sort_keys=True, indent=2) + "\n"

    def document(self) -> dict[str, Any]:
        """The dashboard as Grafana reads it."""
        return json.loads(self.text())


def modules(package: ModuleType = rhi_dashboards) -> list[ModuleType]:
    """The package's modules that define a dashboard, in the order of their names."""
    found = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda info: info.name):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        define = getattr(module, "dashboard", None)
        # Its own function: a module that imports another's, or the SDK's module of that name, defines none.
        if callable(define) and getattr(define, "__module__", None) == module.__name__:
            found.append(module)
    return found


def _number_panels(model: models.Dashboard) -> None:
    """Numbers the panels from 1, rows and the panels inside them included, in their order."""
    number = 0
    for panel in model.panels or []:
        number += 1
        panel.id_val = number
        for inner in panel.panels if isinstance(panel, models.RowPanel) else []:
            number += 1
            inner.id_val = number


def _build(module: ModuleType, config: Config) -> list[Built]:
    made = module.dashboard(config)
    builders = list(made) if isinstance(made, tuple) else [made]
    if not builders or not all(isinstance(builder, dashboard_builders.Dashboard) for builder in builders):
        raise BuildError(f"{module.__name__}: dashboard() must return the SDK's dashboard builder, or a tuple of them")
    declared = getattr(module, "METRICS", None)
    if not isinstance(declared, (tuple, list)) or not all(
        isinstance(one, (rules.FromEngineTable, rules.EmittedBy, rules.ExportedBy)) for one in declared
    ):
        raise BuildError(f"{module.__name__}: METRICS must declare where each metric name comes from")
    built = []
    for builder in builders:
        model = builder.build()
        if model.uid is None or _UID.fullmatch(model.uid) is None:
            raise BuildError(f"{module.__name__}: a dashboard needs an identifier of lower-case letters, digits, hyphens")
        if not model.title:
            raise BuildError(f"{module.__name__}: a dashboard needs a title")
        _number_panels(model)
        built.append(Built(module.__name__, model, tuple(declared)))
    return built


def dashboards(config: Config, package: ModuleType = rhi_dashboards) -> list[Built]:
    """Every dashboard of the package, built from the configuration.

    Two dashboards with one identifier, or with one title, raise `BuildError`: Grafana would keep
    one of them only.
    """
    built = [one for module in modules(package) for one in _build(module, config)]
    for what in ("uid", "title"):
        owners: dict[str, str] = {}
        for one in built:
            value = getattr(one, what)
            if value in owners:
                raise BuildError(f"{owners[value]} and {one.module} give their dashboards the same {what}")
            owners[value] = one.module
    return built


def write(config: Config, folder: Path, package: ModuleType = rhi_dashboards) -> list[Path]:
    """Builds every dashboard of the package and writes each into `folder`, as `<uid>.json`."""
    built = dashboards(config, package)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for one in built:
        path = folder / f"{one.uid}.json"
        path.write_text(one.text(), encoding="utf-8")
        written.append(path)
    return written
