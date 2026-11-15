"""Independent configuration and rejection checks for issue 79."""

from __future__ import annotations

import configparser
import importlib.util
import re
import shlex
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "services/grafana"


def ini() -> configparser.ConfigParser:
    result = configparser.ConfigParser(interpolation=None)
    assert result.read(IMAGE / "grafana.ini")
    return result


def documents(directory: Path) -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in directory.rglob("*")
        if path.suffix in {".yaml", ".yml"}
    ]


def test_image_pin_and_provisioned_dashboard_path() -> None:
    instructions = [
        shlex.split(line)
        for line in (IMAGE / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    bases = [parts[1] for parts in instructions if parts[0].upper() == "FROM"]
    assert len(bases) == 1
    assert re.fullmatch(
        r"(?:docker.io/)?grafana/(?:grafana|grafana-oss)(?::[^@\s]+)?@sha256:[a-f0-9]{64}", bases[0]
    )
    copies = {parts[1].rstrip("/"): parts[2].rstrip("/") for parts in instructions if parts[0].upper() == "COPY"}
    assert copies["grafana.ini"] == "/etc/grafana/grafana.ini"
    assert copies["provisioning"] == "/etc/grafana/provisioning"
    providers = [provider for doc in documents(IMAGE / "provisioning/dashboards") for provider in doc["providers"]]
    assert len(providers) == 1
    assert providers[0]["type"] == "file"
    assert copies["dashboards"] == providers[0]["options"]["path"].rstrip("/")


def test_repository_dashboard_directory_is_empty() -> None:
    folder = IMAGE / "dashboards"
    assert folder.is_dir()
    assert not [path for path in folder.rglob("*") if path.is_file() and path.name != ".gitkeep"]


def test_proxy_identity_and_subpath() -> None:
    config = ini()
    assert config.getint("server", "http_port") == 3000
    assert urlsplit(config["server"]["root_url"]).path == "/grafana/"
    assert config.getboolean("server", "serve_from_sub_path")
    assert config.getboolean("auth.proxy", "enabled")
    assert config["auth.proxy"]["header_name"].lower() == "x-amzn-oidc-identity"
    assert config["auth.proxy"]["header_property"] == "username"
    assert config.getboolean("auth.proxy", "auto_sign_up")


@pytest.mark.parametrize(
    ("section", "key", "expected"),
    [
        ("auth", "disable_login_form", True),
        ("auth.basic", "enabled", False),
        ("users", "allow_sign_up", False),
        ("auth.anonymous", "enabled", False),
        ("security", "disable_initial_admin_creation", True),
    ],
)
def test_alternative_logins_disabled(section: str, key: str, expected: bool) -> None:
    assert ini().getboolean(section, key) is expected


def test_data_sources_use_task_identity_and_environment() -> None:
    docs = documents(IMAGE / "provisioning")
    sources = [source for doc in docs for source in doc.get("datasources", [])]
    assert len(sources) == 2
    assert {source["type"] for source in sources} == {"prometheus", "cloudwatch"}
    assert len({source["uid"] for source in sources}) == 2
    for source in sources:
        assert re.fullmatch(r"[A-Za-z0-9_-]+", source["uid"])
        assert not source.get("secureJsonData")
        assert not source.get("basicAuth")
    by_type = {source["type"]: source for source in sources}
    prometheus = by_type["prometheus"]
    assert re.fullmatch(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", prometheus["url"])
    assert prometheus["jsonData"]["sigV4Auth"] is True
    assert prometheus["jsonData"]["sigV4AuthType"] == "default"
    assert prometheus["jsonData"]["sigV4Region"] in {"$AWS_REGION", "${AWS_REGION}"}
    assert ini().getboolean("auth", "sigv4_auth_enabled")
    assert by_type["cloudwatch"]["jsonData"]["authType"] == "default"
    assert by_type["cloudwatch"]["jsonData"]["defaultRegion"] in {"$AWS_REGION", "${AWS_REGION}"}
    assert {path.name for path in (IMAGE / "provisioning").iterdir()} == {"datasources", "dashboards"}


@pytest.mark.parametrize("change", ["third-source", "credential", "missing-uid", "url-credential"])
def test_named_data_source_test_rejects_invalid_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    spec = importlib.util.spec_from_file_location("grafana_settings_under_review", ROOT / "tests/unit/grafana/test_settings.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provisioning = tmp_path / "provisioning"
    shutil.copytree(IMAGE / "provisioning", provisioning)
    monkeypatch.setattr(module, "PROVISIONING", provisioning)
    module.test_only_two_data_sources()
    path = provisioning / "datasources/datasources.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if change == "third-source":
        extra = {"apiVersion": 1, "datasources": [{"name": "extra", "uid": "extra", "type": "prometheus"}]}
        (provisioning / "datasources/extra.yaml").write_text(yaml.safe_dump(extra), encoding="utf-8")
    else:
        source = document["datasources"][0]
        if change == "credential":
            source["secureJsonData"] = {"basicAuthPassword": uuid4().hex}
        elif change == "missing-uid":
            del source["uid"]
        else:
            source["url"] = urlunsplit(("https", "viewer:" + uuid4().hex + "@example.com", "/", "", ""))
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(AssertionError):
        module.test_only_two_data_sources()
