"""The deployments dashboard, built from the example configuration (system design, sections 4.3, 7.2 and 7.3)."""

from __future__ import annotations

import importlib
import re
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from rhi import config as rhi_config
from rhi_common.engine_metrics import MOCK
from rhi_dashboards import build, check, deployments, rules
from rhi_lifecycle import serving
from rhi_lifecycle.host import Host
from rhi_lifecycle.release import Release, Settings


def release_labels() -> set[str]:
    """The labels the lifecycle agent gives its release metric, read from what an agent in service
    reports (made-up release and settings, stand-ins for AWS)."""
    registry = CollectorRegistry()
    served = Release(f"registry.example.com/engine@sha256:{'a' * 64}", "c" * 40, (), "b" * 64)
    settings = Settings(*(["example"] * 8))
    tables = SimpleNamespace(Table=lambda name: None)
    serving.Serving(served, settings, "instance", MOCK, dynamodb=tables, secrets=None, host=Host(), registry=registry)
    (family,) = [family for family in registry.collect() if family.name == serving.RELEASE_METRIC]
    (sample,) = family.samples
    return set(sample.labels)


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The pipeline's events module, which imports the CDK: its runtime with no package cache, as the app runs it."""
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    return importlib.import_module("rhi.pipeline.events")


def built(config: rhi_config.Config) -> build.Built:
    (one,) = [one for one in build.dashboards(config) if one.module == deployments.__name__]
    return one


def test_the_build_takes_the_module() -> None:
    assert deployments in build.modules()


def test_each_pool_has_a_release_panel_by_instance_digest_and_revision(example: rhi_config.Config) -> None:
    panels = built(example).document()["panels"]
    assert [panel["title"] for panel in panels if panel["type"] == "row"] == [pool.name for pool in example.pools]
    releases = [
        target["expr"]
        for panel in panels
        for target in panel.get("targets", ())
        if target["datasource"]["uid"] == rules.PROMETHEUS_UID
    ]
    assert len(releases) == len(example.pools)
    labels = release_labels()
    assert labels == {"image_digest", "model_revision"}
    for pool, expression in zip(example.pools, releases, strict=True):
        # The lifecycle agent's metric, for this pool, grouped by its two labels and by instance.
        assert f'{serving.RELEASE_METRIC}{{pool="{pool.name}"}}' in expression
        (grouped,) = re.findall(r"by \(([^)]*)\)", expression)
        assert {label.strip() for label in grouped.split(",")} == {"pool", "instance", *labels}


def test_one_panel_reads_the_executions_from_the_pipeline_events(
    example: rhi_config.Config, events: ModuleType
) -> None:
    def logs(panel: dict[str, Any]) -> list[dict[str, Any]]:
        return [t for t in panel.get("targets", ()) if t["datasource"]["uid"] == rules.CLOUDWATCH_UID]

    (panel,) = [panel for panel in built(example).document()["panels"] if logs(panel)]
    (target,) = logs(panel)
    assert target["queryMode"] == "Logs"
    assert target["logGroupNames"] == [events.LOG_GROUP_NAME]
    query = target["expression"]
    reporter = importlib.import_module("rhi.pipeline.reporter")
    assert f'`detail-type` = "{reporter.DETAIL_TYPES[0]}"' in query
    assert reporter.DETAIL_TYPES[0] == "CodePipeline Pipeline Execution State Change"
    for field in ("pipeline", "execution-id", "state"):
        assert field in events.FIELDS
        assert f"`{field}`" in query


def test_the_dashboard_follows_the_rules(example: rhi_config.Config) -> None:
    one = built(example)
    assert check.violations(one.document(), one.metrics, example.pools) == []
