"""The Grafana image refuses everything but the load balancer's identity (system design, 7.4).

The load balancer signs the admin in and sets a header that names them; Grafana trusts that
header and has no login of its own. This test does not take that on the word of `grafana.ini`:
it builds the image of `services/grafana/Dockerfile`, starts it, and asks the running server.

It needs a container runtime, so CI's job `container-tests` runs it and the local gate does not
(tests/container/README.md). Nothing here skips: without a runtime the fixture fails.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

CONTEXT = Path(__file__).resolve().parents[2] / "services" / "grafana"
# Grafana's port in the image, and the path prefix it is served under.
PORT = 3000
PREFIX = "/grafana"
# The header the load balancer sets after its sign-in.
IDENTITY_HEADER = "X-Amzn-Oidc-Identity"
# What the deployment environment gives the container, as placeholders.
ENVIRONMENT = {
    "GF_SERVER_DOMAIN": "example.com",
    "PROMETHEUS_URL": "https://prometheus.example.com/",
    "AWS_REGION": "us-east-1",
}
# The account Grafana creates on its first start unless told not to, as its documentation gives it.
DEFAULT_ADMIN = ("admin", "admin")
# The cookie that carries a Grafana session.
SESSION_COOKIE = "grafana_session"

BUILD_SECONDS = 900
START_SECONDS = 180

# Each route that shows a dashboard or reaches a data source, with the method it answers to. The
# image holds no dashboard until a deployment builds them, so `overview` stands for any identifier.
DASHBOARD_ROUTES = [
    ("GET", "/"),
    ("GET", "/dashboards"),
    ("GET", "/d/overview/overview"),
    ("GET", "/api/search"),
    ("GET", "/api/dashboards/home"),
    ("GET", "/api/dashboards/uid/overview"),
    ("GET", "/apis/dashboard.grafana.app/v1/namespaces/default/dashboards"),
]
DATA_SOURCE_ROUTES = [
    ("GET", "/connections/datasources"),
    ("GET", "/api/datasources"),
    ("GET", "/api/datasources/uid/rhi-prometheus"),
    ("GET", "/api/datasources/uid/rhi-cloudwatch"),
    ("GET", "/api/datasources/proxy/uid/rhi-prometheus/api/v1/query?query=up"),
    ("GET", "/api/datasources/uid/rhi-prometheus/resources/api/v1/labels"),
    ("POST", "/api/ds/query"),
]
# A query of the two data sources, as a dashboard panel sends it.
QUERY = {
    "queries": [
        {"refId": "A", "datasource": {"uid": "rhi-prometheus"}, "expr": "up"},
        {"refId": "B", "datasource": {"uid": "rhi-cloudwatch"}, "queryMode": "Logs"},
    ],
    "from": "now-5m",
    "to": "now",
}


def docker(*arguments: str, timeout: float = 60) -> str:
    """Run one docker command and return what it printed; a failure carries docker's own words."""
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False)
    assert done.returncode == 0, f"docker {arguments[0]} failed:\n{done.stdout}\n{done.stderr}"
    return done.stdout.strip()


def answers(base: str) -> bool:
    try:
        return httpx.get(f"{base}/api/health", timeout=5, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


def address(container: str) -> str:
    """Wait until the container's Grafana answers and return where; a failure carries its log."""
    try:
        port = docker("port", container, f"{PORT}/tcp").splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}{PREFIX}"
        deadline = time.monotonic() + START_SECONDS
        while not answers(base):
            assert docker("inspect", "--format={{.State.Running}}", container) == "true", "the container stopped"
            assert time.monotonic() < deadline, f"no answer in {START_SECONDS} seconds"
            time.sleep(1)
    except AssertionError as failure:
        log = subprocess.run(["docker", "logs", "--tail=80", container], capture_output=True, text=True, check=False)
        raise AssertionError(f"Grafana did not start: {failure}\n{log.stdout}\n{log.stderr}") from None
    return base


@pytest.fixture(scope="module")
def grafana() -> Iterator[str]:
    """The address of a running container of the image, under its prefix; the container ends here."""
    image = f"rhi-grafana-sign-in:{uuid4().hex}"
    docker("build", "--tag", image, str(CONTEXT), timeout=BUILD_SECONDS)
    settings = [f"--env={name}={value}" for name, value in ENVIRONMENT.items()]
    # Published on a free port of the loopback interface only.
    container = docker("run", "--detach", f"--publish=127.0.0.1::{PORT}", *settings, image)
    try:
        yield address(container)
    finally:
        # Stops and removes the container, whether it never answered, a test failed or all passed.
        subprocess.run(["docker", "rm", "--force", "--volumes", container], capture_output=True, check=False)


def ask(
    base: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: object = None,
    auth: tuple[str, str] | None = None,
) -> httpx.Response:
    """One request with nothing kept from another: no cookie jar, and no redirect followed."""
    return httpx.request(
        method, base + path, headers=headers, json=json, auth=auth, follow_redirects=False, trust_env=False, timeout=30
    )


def refused(response: httpx.Response) -> bool:
    """Whether Grafana turned the request away: an error for the API, the login page for a browser.

    A 404 is not a refusal: it would mean that the route asked for does not exist.
    """
    if response.status_code in {401, 403}:
        return True
    to_login = response.headers.get("location", "").split("?")[0].rstrip("/") == f"{PREFIX}/login"
    return response.status_code in {301, 302, 303, 307, 308} and to_login


@pytest.mark.parametrize(("method", "path"), DASHBOARD_ROUTES + DATA_SOURCE_ROUTES)
def test_without_the_identity_header_a_request_is_refused(grafana: str, method: str, path: str) -> None:
    response = ask(grafana, method, path, json=QUERY) if method == "POST" else ask(grafana, method, path)
    assert refused(response), f"{method} {path} answered {response.status_code}"
    assert SESSION_COOKIE not in response.cookies


def test_with_the_identity_header_a_request_is_served_as_that_user(grafana: str) -> None:
    name = str(uuid4())
    identity = {IDENTITY_HEADER: name}

    user = ask(grafana, "GET", "/api/user", headers=identity)
    assert user.status_code == 200
    assert user.json()["login"] == name

    # The routes refused above exist: the same requests are served once the header names a user.
    for path in ["/", "/api/search", "/api/dashboards/home", "/api/datasources"]:
        assert refused(ask(grafana, "GET", path))
        assert ask(grafana, "GET", path, headers=identity).status_code == 200, path
    # And the data sources behind them are the two of the image.
    sources = ask(grafana, "GET", "/api/datasources", headers=identity).json()
    assert {"rhi-prometheus", "rhi-cloudwatch"} <= {source["uid"] for source in sources}

    # The header is the whole sign-in: it left nothing that serves a later request without it.
    assert refused(ask(grafana, "GET", "/api/user"))


def test_the_default_admin_is_refused_on_the_login_route(grafana: str) -> None:
    name, password = DEFAULT_ADMIN
    response = ask(grafana, "POST", "/login", json={"user": name, "password": password})
    # A client error and no session. Which one depends on why: 401 for a wrong password, 400 from
    # a Grafana whose form is off, which has no sign-in by password at all.
    assert response.status_code in {400, 401, 403}
    assert SESSION_COOKIE not in response.cookies


@pytest.mark.parametrize("path", ["/api/user", "/api/org", "/api/admin/settings", "/api/datasources"])
def test_the_default_admin_is_refused_on_the_http_api(grafana: str, path: str) -> None:
    response = ask(grafana, "GET", path, auth=DEFAULT_ADMIN)
    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies


def test_the_name_of_the_default_admin_is_no_administrator(grafana: str) -> None:
    # Were the default account there, the header naming it would sign in as the server's admin.
    user = ask(grafana, "GET", "/api/user", headers={IDENTITY_HEADER: DEFAULT_ADMIN[0]})
    assert user.status_code == 200
    assert user.json()["isGrafanaAdmin"] is False
    settings = ask(grafana, "GET", "/api/admin/settings", headers={IDENTITY_HEADER: DEFAULT_ADMIN[0]})
    assert settings.status_code == 403
