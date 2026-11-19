"""The dashboards' build, and the rules each built dashboard is held to (system design, sections 7.2 and 7.3).

The package's own dashboards are built from the example configuration. What the build and the
check must do is also shown on dashboards made here: sample modules in a package of their own, and
panels that break one rule each.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from grafana_foundation_sdk.builders import dashboard as sdk
from grafana_foundation_sdk.builders import prometheus as sdk_prometheus
from grafana_foundation_sdk.builders import stat as sdk_stat
from grafana_foundation_sdk.builders import timeseries as sdk_timeseries
from grafana_foundation_sdk.models import common as sdk_common
from grafana_foundation_sdk.models import dashboard as sdk_models

from rhi import config as rhi_config
from rhi_common.engine_metrics import Quantity
from rhi_common.pool_config import parse_pools
from rhi_dashboards import build, check, rules

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

    from rhi_common.pool_config import Pool

REPO = Path(__file__).resolve().parents[3]
SDK = "grafana-foundation-sdk"

# A dashboard of engine panels, one row a pool, and one of the gateway's numbers.
ENGINES_MODULE = """
from grafana_foundation_sdk.builders import dashboard as sdk
from rhi_common.engine_metrics import Quantity
from rhi_dashboards import rules

METRICS = (
    rules.FromEngineTable(Quantity.OUTPUT_TOKENS),
    rules.FromEngineTable(Quantity.RUNNING_REQUESTS),
    rules.FromEngineTable(Quantity.TIME_TO_FIRST_TOKEN),
)


def dashboard(config):
    built = sdk.Dashboard("Sample: engines").uid("sample-engines")
    for pool in config.pools:
        tokens = rules.engine_rate(pool, Quantity.OUTPUT_TOKENS)
        running = f"sum by (pool) ({rules.engine_selector(pool, Quantity.RUNNING_REQUESTS)})"
        built.with_row(sdk.Row(pool.name))
        built.with_panel(rules.engine_timeseries(pool, "Output", {"tokens": tokens}))
        built.with_panel(rules.engine_stat(pool, "Running", {"requests": running}))
        built.with_panel(rules.engine_percentile_timeseries(pool, "First token", Quantity.TIME_TO_FIRST_TOKEN))
    return built
"""
STUDENTS_MODULE = """
from grafana_foundation_sdk.builders import dashboard as sdk
from grafana_foundation_sdk.builders import timeseries
from rhi_dashboards import rules

METRICS = (
    rules.EmittedBy("rhi_gateway_student_requests", "services/gateway/rhi_gateway/metrics.py"),
    rules.ExportedBy("sample_request_seconds", "sample-exporter", "1.2.3"),
)


def dashboard(config):
    requests = rules.rate("rhi_gateway_student_requests_total", by=("alias",))
    return (
        sdk.Dashboard("Sample: students")
        .uid("sample-students")
        .with_panel(timeseries.Panel().title("Requests").with_target(rules.query(requests, "{{alias}}", "A")))
        .with_panel(rules.percentile_timeseries("Latency", "sample_request_seconds"))
    )
"""
# Not dashboards: a module of helpers, and one that only imports another's function.
HELPERS_MODULE = "NAMES = ('a', 'b')\n"
IMPORTER_MODULE = "from .engines import dashboard\n"
SAMPLES = {"engines": ENGINES_MODULE, "students": STUDENTS_MODULE, "helpers": HELPERS_MODULE, "zz": IMPORTER_MODULE}


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


@pytest.fixture
def two_pools(example: rhi_config.Config) -> rhi_config.Config:
    """The example configuration with a second pool of the first one's engine."""
    first = example.pools[0]
    return dataclasses.replace(example, pools=(first, dataclasses.replace(first, name="second")))


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[dict[str, str]], ModuleType]]:
    """Makes a package of the modules given, name to source, and imports it."""
    made: list[str] = []

    def make(modules: dict[str, str]) -> ModuleType:
        name = f"sample_dashboards_{uuid4().hex}"
        folder = tmp_path / name
        folder.mkdir()
        (folder / "__init__.py").write_text("", encoding="utf-8")
        for module, source in modules.items():
            (folder / f"{module}.py").write_text(source, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        made.append(name)
        return importlib.import_module(name)

    yield make
    for name in [loaded for loaded in sys.modules if loaded.split(".")[0] in made]:
        del sys.modules[name]


def files(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(folder.iterdir())}


def held(built: build.Built, pools: tuple[Pool, ...]) -> list[check.Violation]:
    return check.violations(built.document(), built.metrics, pools)


def document(*panels: Any) -> dict[str, Any]:
    """A dashboard built here, of the panels given."""
    builder = sdk.Dashboard("Built in the test").uid("built-in-the-test")
    for panel in panels:
        builder.with_panel(panel)
    return build.Built("test", builder.build(), ()).document()


def graph(*expressions: str) -> sdk_timeseries.Panel:
    """A panel made without the helpers' panels: one query an expression."""
    panel = sdk_timeseries.Panel().title("By hand")
    for index, expression in enumerate(expressions):
        panel.with_target(rules.query(expression, "", "ABCDEF"[index]))
    return panel


def rules_broken(found: list[check.Violation]) -> set[str]:
    return {violation.rule for violation in found}


def test_a_module_that_defines_a_dashboard_is_found_without_being_listed(
    package: Callable[[dict[str, str]], ModuleType], example: rhi_config.Config, tmp_path: Path
) -> None:
    made = package({name: SAMPLES[name] for name in ("engines", "helpers", "zz")})
    folder = tmp_path / "out" / "dashboards"

    written = build.write(example, folder, made)

    assert [path.name for path in written] == ["sample-engines.json"]
    assert sorted(files(folder)) == ["sample-engines.json"]

    # A module added to the package is a dashboard more: nothing else changes.
    (Path(made.__path__[0]) / "students.py").write_text(STUDENTS_MODULE, encoding="utf-8")
    importlib.invalidate_caches()
    build.write(example, folder, made)

    assert sorted(files(folder)) == ["sample-engines.json", "sample-students.json"]
    for name, content in files(folder).items():
        assert json.loads(content)["uid"] == name.removesuffix(".json")
    # The package's own modules of helpers define none.
    assert not {"build", "check", "rules"} & {module.__name__.rpartition(".")[2] for module in build.modules()}


def test_a_dashboard_is_built_from_the_configuration(
    package: Callable[[dict[str, str]], ModuleType], two_pools: rhi_config.Config
) -> None:
    engines = build.dashboards(two_pools, package(SAMPLES))[0].document()

    rows = [panel["title"] for panel in engines["panels"] if panel["type"] == "row"]
    assert rows == [pool.name for pool in two_pools.pools] == ["mock", "second"]
    # Each row's engine queries are for its pool, in the names of its pool's engine.
    for pool in two_pools.pools:
        series = rules.engine_selector(pool, Quantity.OUTPUT_TOKENS)
        assert any(series in target["expr"] for panel in engines["panels"] for target in panel.get("targets", []))


def test_a_dashboard_needs_what_the_build_reads(
    package: Callable[[dict[str, str]], ModuleType], example: rhi_config.Config
) -> None:
    no_declaration = ENGINES_MODULE.replace("METRICS = (", "OTHER = (")
    no_identifier = STUDENTS_MODULE.replace('.uid("sample-students")', "")
    not_the_sdk = "METRICS = ()\n\ndef dashboard(config):\n    return {'title': 'By hand', 'uid': 'by-hand'}\n"

    for source in (no_declaration, no_identifier, not_the_sdk):
        with pytest.raises(build.BuildError):
            build.dashboards(example, package({"only": source}))


def test_the_sdk_is_pinned_and_the_dashboards_are_written_with_it(
    package: Callable[[dict[str, str]], ModuleType], example: rhi_config.Config
) -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    pins = [requirement for requirement in project["dependencies"] if requirement.startswith(SDK)]
    locked = [entry["version"] for entry in lock["package"] if entry["name"] == SDK]

    assert len(locked) == 1
    assert pins == [f"{SDK}=={locked[0]}"]

    for built in build.dashboards(example, package(SAMPLES)):
        assert isinstance(built.model, sdk_models.Dashboard)
        assert built.document()["panels"]
    assert isinstance(rules.query("up", "", "A"), sdk_prometheus.Dataquery)
    assert isinstance(rules.percentile_timeseries("Latency", "sample_request_seconds"), sdk_timeseries.Panel)


def test_every_dashboard_follows_the_rules(
    package: Callable[[dict[str, str]], ModuleType], example: rhi_config.Config, two_pools: rhi_config.Config
) -> None:
    # The package's own dashboards, from the example configuration.
    for built in build.dashboards(example):
        assert held(built, example.pools) == [], built.module

    # Dashboards made with the helpers follow the rules.
    samples = build.dashboards(two_pools, package(SAMPLES))
    assert [built.uid for built in samples] == ["sample-engines", "sample-students"]
    for built in samples:
        assert held(built, two_pools.pools) == [], built.module
    engines, students = samples
    pool = two_pools.pools[0]
    pools = two_pools.pools
    tokens = rules.engine_rate(pool, Quantity.OUTPUT_TOKENS)
    seconds = rules.ExportedBy("sample_request_seconds", "sample-exporter", "1.2.3")
    requests = rules.EmittedBy("rhi_gateway_student_requests", "services/gateway/rhi_gateway/metrics.py")
    per_series = rules.rate("rhi_gateway_student_requests_total")
    assert check.violations(document(graph(per_series)), [requests], pools) == []

    # A rate, or an increase, over a sum.
    for function in ("rate", "increase"):
        over_a_sum = f"{function}(sum by (pool) (rhi_gateway_student_requests_total)[5m:])"
        assert rules_broken(check.violations(document(graph(over_a_sum)), [requests], pools)) == {"rate"}

    # An engine panel without the pool's in-service count: the bare query, the query kept to the
    # pool in service with nothing to read asleep, and the helper's panel less its state query.
    engine = [rules.FromEngineTable(Quantity.OUTPUT_TOKENS)]
    assert check.violations(document(rules.engine_timeseries(pool, "Output", {"tokens": tokens})), engine, pools) == []
    assert rules_broken(check.violations(document(graph(tokens)), engine, pools)) == {"asleep"}
    assert rules_broken(check.violations(document(graph(rules.awake(pool.name, tokens))), engine, pools)) == {"asleep"}
    without_state = engines.document()
    for panel in without_state["panels"]:
        panel["targets"] = [target for target in panel.get("targets", []) if target["refId"] != rules.ASLEEP_REF]
    found = check.violations(without_state, engines.metrics, pools)
    assert rules_broken(found) == {"asleep"}
    assert len(found) == len([panel for panel in without_state["panels"] if panel["type"] != "row"])
    # A state query that is disabled is on no panel: nothing reads asleep.
    state_disabled = engines.document()
    for panel in state_disabled["panels"]:
        for target in panel.get("targets", []):
            target["hide"] = target["refId"] == rules.ASLEEP_REF
    found = check.violations(state_disabled, engines.metrics, pools)
    assert rules_broken(found) == {"asleep"}
    assert len(found) == len([panel for panel in state_disabled["panels"] if panel["type"] != "row"])
    # The other pool's count is not this pool's.
    other = graph(rules.awake("second", tokens), rules.asleep("second"))
    assert rules_broken(check.violations(document(other), engine, pools)) == {"asleep"}

    # A percentile without its count, or with a count over another window.
    alone = graph(rules.quantile(0.95, "sample_request_seconds", window="5m"))
    another_window = graph(
        rules.quantile(0.95, "sample_request_seconds", window="5m"),
        rules.request_count("sample_request_seconds", window="1h"),
    )
    same_window = graph(
        rules.quantile(0.95, "sample_request_seconds", window="5m"),
        rules.request_count("sample_request_seconds", window="5m"),
    )
    assert rules_broken(check.violations(document(alone), [seconds], pools)) == {"percentile"}
    assert rules_broken(check.violations(document(another_window), [seconds], pools)) == {"percentile"}
    assert check.violations(document(same_window), [seconds], pools) == []
    # The count is shown, as a query of its own: a disabled one is on no panel, and a mean that
    # holds the same increase shows a duration.
    latency = document(rules.percentile_timeseries("Latency", "sample_request_seconds"))
    assert check.violations(latency, [seconds], pools) == []
    count = latency["panels"][0]["targets"][-1]
    assert count["expr"] == rules.request_count("sample_request_seconds")
    count["hide"] = True
    assert rules_broken(check.violations(latency, [seconds], pools)) == {"percentile"}
    mean = f"{rules.increase('sample_request_seconds_sum')} / {rules.increase('sample_request_seconds_count')}"
    count.update(hide=False, expr=mean)
    assert rules_broken(check.violations(latency, [seconds], pools)) == {"percentile"}
    # An engine's percentile is held to the same: its count is kept to the pool in service, and shown.
    count_disabled = engines.document()
    for panel in count_disabled["panels"]:
        for target in panel.get("targets", []):
            target["hide"] = (panel["title"], target["legendFormat"]) == ("First token", "requests")
    found = check.violations(count_disabled, engines.metrics, pools)
    assert rules_broken(found) == {"percentile"}
    assert len(found) == 2 * len(pools)

    # A metric name the module does not declare: the gateway's, and an engine's.
    assert rules_broken(check.violations(students.document(), students.metrics[1:], pools)) == {"undeclared"}
    assert rules_broken(check.violations(engines.document(), engines.metrics[1:], pools)) == {"undeclared"}
    assert rules_broken(check.violations(document(graph("up")), [], pools)) == {"undeclared"}
    # A sample's suffix is covered after the name of a declared family, and not on its own.
    assert check.violations(document(graph("sample_request_seconds_sum")), [seconds], pools) == []
    assert rules_broken(check.violations(document(graph("_count")), [seconds], pools)) == {"undeclared"}
    assert rules_broken(check.violations(document(graph("request_seconds_sum")), [seconds], pools)) == {"undeclared"}

    # A name declared from a file that does not contain it, or from no file of the repository.
    for path in ("services/scaler/rhi_scaler/metrics.py", "services/gateway/no_such_file.py", "../metrics.py"):
        elsewhere = [rules.EmittedBy("rhi_gateway_student_requests", path), seconds]
        assert rules_broken(check.violations(students.document(), elsewhere, pools)) == {"declaration"}, path
    # A name the file holds only as the start of a longer one is not in it.
    shorter = [rules.EmittedBy("rhi_gateway_student", "services/gateway/rhi_gateway/metrics.py")]
    assert "declaration" in rules_broken(check.violations(document(graph("rhi_gateway_student")), shorter, pools))
    # An exporter is named with one exact version.
    for version in ("", "4.9 or later", ">=4.9.0", "latest"):
        loose = [requests, rules.ExportedBy("sample_request_seconds", "sample-exporter", version)]
        assert rules_broken(check.violations(students.document(), loose, pools)) == {"declaration"}, version


def test_a_query_names_a_fixed_data_source_and_the_row_of_its_pools_engine(example: rhi_config.Config) -> None:
    pool = example.pools[0]
    requests = [rules.EmittedBy("rhi_gateway_student_requests", "services/gateway/rhi_gateway/metrics.py")]
    expression = rules.rate("rhi_gateway_student_requests_total")

    def by_hand(source: sdk_common.DataSourceRef | None) -> sdk_stat.Panel:
        query = sdk_prometheus.Dataquery().expr(expression).ref_id("A")
        return sdk_stat.Panel().title("By hand").with_target(query if source is None else query.datasource(source))

    assert check.violations(document(by_hand(rules.PROMETHEUS)), requests, example.pools) == []
    for source in (
        None,
        sdk_common.DataSourceRef(type_val="prometheus", uid="another-prometheus"),
        sdk_common.DataSourceRef(type_val="prometheus", uid="${datasource}"),
        sdk_common.DataSourceRef(type_val="prometheus"),
    ):
        found = check.violations(document(by_hand(source)), requests, example.pools)
        assert "data-source" in rules_broken(found), source

    # An engine's metric is queried for a configured pool, in the names of that pool's engine.
    engine = [rules.FromEngineTable(Quantity.OUTPUT_TOKENS)]
    entry = rhi_config.pool_entry(pool) | {"engine": "vllm", "model": "example/model", "revision": "0" * 40}
    of_another_engine = parse_pools({"other": entry | {"gpu_hours_per_day": 1}})[0]
    queries = {"tokens": rules.engine_rate(pool, Quantity.OUTPUT_TOKENS)}
    panel = document(rules.engine_timeseries(pool, "Output", queries))

    assert check.violations(panel, engine, [pool]) == []
    assert rules_broken(check.violations(panel, engine, [])) == {"engine-row"}
    assert rules_broken(check.violations(panel, engine, [dataclasses.replace(of_another_engine, name=pool.name)])) == {
        "engine-row"
    }
    every_pool = rules.rate(rules.engine_series(pool, Quantity.OUTPUT_TOKENS))
    assert rules_broken(check.violations(document(graph(every_pool)), engine, [pool])) == {"engine-row"}


def test_the_build_is_stable(
    package: Callable[[dict[str, str]], ModuleType],
    example: rhi_config.Config,
    two_pools: rhi_config.Config,
    tmp_path: Path,
) -> None:
    # The package's own dashboards, from the example configuration.
    build.write(example, tmp_path / "first")
    build.write(example, tmp_path / "second")
    own = build.dashboards(example)

    assert files(tmp_path / "first") == files(tmp_path / "second")
    assert sorted(files(tmp_path / "first")) == sorted(f"{built.uid}.json" for built in own)
    assert len({built.uid for built in own}) == len({built.title for built in own}) == len(own)

    # The same on sample dashboards: built twice, and built again from the configuration loaded again.
    made = package(SAMPLES)
    build.write(two_pools, tmp_path / "third", made)
    build.write(two_pools, tmp_path / "fourth", made)
    again = rhi_config.load({"RHI_CONFIG": "example"})
    build.write(dataclasses.replace(again, pools=two_pools.pools), tmp_path / "fifth", package(SAMPLES))

    assert len(files(tmp_path / "third")) == 2
    assert files(tmp_path / "third") == files(tmp_path / "fourth") == files(tmp_path / "fifth")
    for content in files(tmp_path / "third").values():
        numbers = [panel["id"] for panel in json.loads(content)["panels"]]
        assert numbers == list(range(1, len(numbers) + 1))

    # Two dashboards never share an identifier or a title: the build refuses them.
    same_identifier = STUDENTS_MODULE.replace("sample-students", "sample-engines")
    same_title = STUDENTS_MODULE.replace("Sample: students", "Sample: engines")
    for clash in (same_identifier, same_title):
        with pytest.raises(build.BuildError):
            build.dashboards(two_pools, package(SAMPLES | {"students": clash}))
