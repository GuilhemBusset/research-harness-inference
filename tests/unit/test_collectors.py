"""The collectors' configurations: what each scrapes, where it writes, and the labels it adds.

The node's collector is the only way an instance's metrics and the lines of its units leave it,
and the gateway's the only way a task's metrics do (system design, sections 4.3 and 7.1). The
dashboards take rates per instance, so every series says which instance or task it comes from
(section 7.2). Both files are public: what is particular to a deployment reaches them through the
environment.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

SERVICES = Path(__file__).resolve().parents[2] / "services"
NODE = SERVICES / "engine" / "collector" / "config.yaml"
GATEWAY = SERVICES / "gateway" / "collector" / "config.yaml"

# The units of an instance (section 4.3) and the boot unit that starts them.
UNITS = {f"rhi-{name}.service" for name in ("boot", "engine", "lifecycle", "collector", "dcgm")}
# What a deployment's address, region, account or ARN looks like, anywhere in a file.
DEPLOYMENT = re.compile(r"://|amazonaws|\barn:|\b[a-z]{2}(-[a-z]+)+-\d\b|\d{12}")
ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Journal options that would read other entries than the listed units', or another journal.
WIDER = {"matches", "merge", "files", "directory", "namespace", "dmesg"}


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def pipelines(config: dict[str, Any], signal: str) -> list[dict[str, Any]]:
    """The pipelines of one signal: `metrics`, or `metrics/<name>`."""
    return [body for name, body in config["service"]["pipelines"].items() if name.split("/")[0] == signal]


def targets(config: dict[str, Any]) -> list[str]:
    """Every address the metrics pipelines scrape."""
    receivers = {name for pipeline in pipelines(config, "metrics") for name in pipeline["receivers"]}
    jobs = [
        job
        for name in receivers
        for job in (config["receivers"][name] or {}).get("config", {}).get("scrape_configs", [])
    ]
    assert all(
        {"job_name", "static_configs"} <= set(job) <= {"job_name", "scrape_interval", "static_configs"} for job in jobs
    )
    return sorted(target for job in jobs for static in job["static_configs"] for target in static["targets"])


def origin(config: dict[str, Any], pipeline: dict[str, Any], detector: str) -> dict[str, dict[str, Any]]:
    """What the pipeline writes on the resource of everything that passes, after it has asked
    `detector` alone, and refused to start without an answer."""
    detection, *rest = pipeline["processors"]
    detected = config["processors"][detection]
    assert detection.split("/")[0] == "resource_detection"
    assert detected["detectors"] == [detector]
    assert detected["fail_on_missing_metadata"] is True
    assert all(name.split("/")[0] == "resource" for name in rest)
    actions = [action for name in rest for action in config["processors"][name]["attributes"]]
    off = {
        key for key, value in detected.get(detector, {}).get("resource_attributes", {}).items() if not value["enabled"]
    }
    assert not off & {action.get("from_attribute") for action in actions}
    return {action["key"]: {k: v for k, v in action.items() if k != "key"} for action in actions}


def remote_write(config: dict[str, Any]) -> dict[str, Any]:
    """The one exporter of the metrics pipelines, which signs what it writes to the workspace."""
    assert pipelines(config, "metrics")
    assert all(pipeline["exporters"] == ["prometheus_remote_write"] for pipeline in pipelines(config, "metrics"))
    exporter = config["exporters"]["prometheus_remote_write"]
    signer = exporter["http"]["auth"]["authenticator"]
    assert exporter["http"]["endpoint"] == "${env:RHI_WORKSPACE_URL}"
    assert signer in config["service"]["extensions"]
    assert config["extensions"][signer] == {"region": "${env:RHI_REGION}", "service": "aps"}
    return exporter


def test_each_collector_scrapes_localhost_and_the_node_its_host() -> None:
    node = load(NODE)
    assert targets(node) == ["127.0.0.1:8000", "127.0.0.1:9110"]
    assert targets(load(GATEWAY)) == ["127.0.0.1:9091"]
    for pipeline in pipelines(node, "metrics"):
        assert "host_metrics" in pipeline["receivers"]
    assert {"cpu", "memory", "disk"} <= set(node["receivers"]["host_metrics"]["scrapers"])


def test_one_exporter_for_each_signal() -> None:
    node, gateway = load(NODE), load(GATEWAY)
    remote_write(node)
    remote_write(gateway)
    assert set(node["exporters"]) == {"prometheus_remote_write", "awscloudwatchlogs"}
    assert set(gateway["exporters"]) == {"prometheus_remote_write"}
    assert set(gateway["service"]["pipelines"]) == {"metrics"}


def test_nothing_of_a_deployment_is_written() -> None:
    names = {
        NODE: {"RHI_WORKSPACE_URL", "RHI_REGION", "RHI_POOL", "RHI_LOG_GROUP"},
        GATEWAY: {"RHI_WORKSPACE_URL", "RHI_REGION"},
    }
    for path, expected in names.items():
        text = path.read_text(encoding="utf-8")
        assert DEPLOYMENT.search(text) is None
        assert set(ADDRESS.findall(text)) == {"127.0.0.1"}
        # Every reference is to the environment, and to the names the unit or the task gives.
        assert set(re.findall(r"\$\{([^}]*)\}", text)) == {f"env:{name}" for name in expected}


def test_the_units_logs_reach_the_pool_log_group() -> None:
    node = load(NODE)
    (pipeline,) = pipelines(node, "logs")
    assert pipeline["receivers"] == ["journald"]
    journal = node["receivers"]["journald"]
    assert set(journal["units"]) == UNITS
    assert not WIDER & set(journal)
    # No other receiver of the file reads a log: the others are the two of the metrics pipelines.
    assert set(node["receivers"]) == {"journald", "prometheus", "host_metrics"}

    assert pipeline["exporters"] == ["awscloudwatchlogs"]
    exporter = node["exporters"]["awscloudwatchlogs"]
    assert exporter["log_group_name"] == "${env:RHI_LOG_GROUP}"
    assert exporter["region"] == "${env:RHI_REGION}"
    # One stream per instance: the exporter's {InstanceId} is the resource's service.instance.id.
    assert exporter["log_stream_name"] == "{InstanceId}"
    assert origin(node, pipeline, "ec2")["service.instance.id"] == {"from_attribute": "host.id", "action": "upsert"}


def test_every_series_says_where_it_comes_from() -> None:
    # The remote-write exporter makes the resource's service.instance.id the label `instance`, in
    # place of the scrape address, and the resource's attributes it is told to, labels.
    node, gateway = load(NODE), load(GATEWAY)
    for pipeline in pipelines(node, "metrics"):
        written = origin(node, pipeline, "ec2")
        assert written["service.instance.id"] == {"from_attribute": "host.id", "action": "upsert"}
        assert written["instance_type"] == {"from_attribute": "host.type", "action": "upsert"}
        assert written["pool"] == {"value": "${env:RHI_POOL}", "action": "upsert"}
    assert {"pool", "instance_type"} <= set(remote_write(node)["resource_constant_labels"]["included"])

    for pipeline in pipelines(gateway, "metrics"):
        written = origin(gateway, pipeline, "ecs")
        assert written["service.instance.id"] == {"from_attribute": "aws.ecs.task.id", "action": "upsert"}
    remote_write(gateway)
