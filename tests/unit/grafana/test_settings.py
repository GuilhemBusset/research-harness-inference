"""Grafana's image: no login of its own, two data sources, a pinned base (system design, 7.4)."""

from __future__ import annotations

import configparser
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

GRAFANA = Path(__file__).resolve().parents[3] / "services" / "grafana"
PROVISIONING = GRAFANA / "provisioning"

# The two data sources, by fixed identifier, and the type each must have.
EXPECTED_SOURCES = {"rhi-prometheus": "prometheus", "rhi-cloudwatch": "cloudwatch"}
# Keys that would carry a credential in a data source.
CREDENTIAL_KEYS = {
    "securejsondata",
    "password",
    "user",
    "basicauthuser",
    "basicauthpassword",
    "accesskey",
    "secretkey",
    "sigv4accesskey",
    "sigv4secretkey",
}


def settings() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(GRAFANA / "grafana.ini", encoding="utf-8")
    return parser


def dockerfile_instructions() -> list[list[str]]:
    lines = (GRAFANA / "Dockerfile").read_text(encoding="utf-8").splitlines()
    return [line.split() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def copies() -> dict[str, str]:
    return {parts[1].rstrip("/"): parts[2].rstrip("/") for parts in dockerfile_instructions() if parts[0] == "COPY"}


def yaml_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.suffix in {".yaml", ".yml"})


def walk_keys(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        pairs = list(value.items())
        return pairs + [pair for item in value.values() for pair in walk_keys(item)]
    if isinstance(value, list):
        return [pair for item in value for pair in walk_keys(item)]
    return []


def datasource_problems(provisioning: Path) -> list[str]:
    """Everything in the provisioning files that breaks "two data sources, no credential"."""
    sources = [
        source
        for path in yaml_files(provisioning)
        for source in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("datasources") or []
    ]
    problems = []
    uids = [source.get("uid") for source in sources]
    if any(not uid for uid in uids):
        problems.append("a data source has no fixed identifier")
    if sorted(map(str, uids)) != sorted(EXPECTED_SOURCES):
        problems.append(f"data sources {uids}, expected {sorted(EXPECTED_SOURCES)}")
    for source in sources:
        uid = source.get("uid")
        if uid in EXPECTED_SOURCES and source.get("type") != EXPECTED_SOURCES[uid]:
            problems.append(f"{uid} has type {source.get('type')}")
        for key, value in walk_keys(source):
            if key.lower() in CREDENTIAL_KEYS:
                problems.append(f"{uid} carries {key}")
            if key == "basicAuth" and value:
                problems.append(f"{uid} uses basic authentication")
            if key in {"authType", "sigV4AuthType"} and value != "default":
                problems.append(f"{uid} authenticates with {value}, not the task's role")
    return problems


def sources() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load((PROVISIONING / "datasources" / "datasources.yaml").read_text(encoding="utf-8"))
    return {source["uid"]: source for source in document["datasources"]}


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("auth", "disable_login_form", True),
        ("auth.basic", "enabled", False),
        ("users", "allow_sign_up", False),
        ("auth.anonymous", "enabled", False),
        ("security", "disable_initial_admin_creation", True),
    ],
)
def test_five_settings_are_off(section: str, key: str, value: bool) -> None:
    assert settings().getboolean(section, key) is value


def test_serves_on_3000_under_grafana() -> None:
    server = settings()["server"]
    assert server.getint("http_port") == 3000
    assert urlsplit(server["root_url"]).path == "/grafana/"
    assert server.getboolean("serve_from_sub_path") is True


def test_proxy_sign_in_trusts_the_load_balancer_header_and_creates_its_user() -> None:
    proxy = settings()["auth.proxy"]
    assert proxy.getboolean("enabled") is True
    assert proxy["header_name"].lower() == "x-amzn-oidc-identity"
    assert proxy["header_property"] == "username"
    assert proxy.getboolean("auto_sign_up") is True


def test_base_image_is_upstream_grafana_oss_pinned_by_digest() -> None:
    froms = [parts for parts in dockerfile_instructions() if parts[0] == "FROM"]
    assert len(froms) == 1
    assert re.fullmatch(r"grafana/grafana-oss(:[\w.-]+)?@sha256:[0-9a-f]{64}", froms[0][1])
    assert not [parts for parts in dockerfile_instructions() if parts[0] == "RUN"]


def test_image_adds_configuration_provisioning_and_dashboards() -> None:
    provider = yaml.safe_load((PROVISIONING / "dashboards" / "dashboards.yaml").read_text(encoding="utf-8"))
    (only,) = provider["providers"]
    assert only["type"] == "file"
    assert copies() == {
        "grafana.ini": "/etc/grafana/grafana.ini",
        "provisioning": "/etc/grafana/provisioning",
        "dashboards": only["options"]["path"].rstrip("/"),
    }


def test_dashboards_folder_holds_no_dashboard() -> None:
    assert (GRAFANA / "dashboards").is_dir()
    assert not list((GRAFANA / "dashboards").rglob("*.json"))


def test_prometheus_signs_requests_to_the_workspace_from_the_environment() -> None:
    prometheus = sources()["rhi-prometheus"]
    assert prometheus["url"] in {"$PROMETHEUS_URL", "${PROMETHEUS_URL}"}
    assert prometheus["jsonData"]["sigV4Auth"] is True
    assert settings().getboolean("auth", "sigv4_auth_enabled") is True


def test_only_two_data_sources() -> None:
    assert datasource_problems(PROVISIONING) == []


def _third_source(root: Path) -> None:
    (root / "datasources" / "extra.yml").write_text(
        "apiVersion: 1\ndatasources:\n  - {name: Loki, uid: rhi-loki, type: loki, url: http://loki:3100}\n",
        encoding="utf-8",
    )


def _edit(change: str) -> Any:
    def mutate(root: Path) -> None:
        path = root / "datasources" / "datasources.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        prometheus, cloudwatch = document["datasources"]
        if change == "credential":
            cloudwatch["secureJsonData"] = {"secretKey": "generated-at-test-time"}
        elif change == "basic-auth":
            prometheus.update(basicAuth=True, basicAuthUser="viewer")
        elif change == "keys":
            cloudwatch["jsonData"]["authType"] = "keys"
        elif change == "no-uid":
            del prometheus["uid"]
        path.write_text(yaml.safe_dump(document), encoding="utf-8")

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [_third_source, _edit("credential"), _edit("basic-auth"), _edit("keys"), _edit("no-uid")],
    ids=["third-source", "credential", "basic-auth", "keys", "no-uid"],
)
def test_only_two_data_sources_refuses(tmp_path: Path, mutate: Any) -> None:
    root = tmp_path / "provisioning"
    shutil.copytree(PROVISIONING, root)
    assert datasource_problems(root) == []
    mutate(root)
    assert datasource_problems(root)
