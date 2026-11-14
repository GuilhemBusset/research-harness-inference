+++
id = "E2-59"
title = "A Grafana image with no login of its own"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-16"]
+++

## Context

Grafana OSS runs on Fargate behind the load balancer's own sign-in (section 7.4; decision 11).
Grafana's own GitHub sign-in cannot be restricted to one user: it admits every GitHub user as a
viewer, and a viewer may run any query against the data sources (evidence E24). So Grafana trusts
the identity header the load balancer sets, has its login form and its default admin account
disabled, and accepts connections from the load balancer only (issue E2-60). Dashboards are files in
the image: nothing imperative.

The load balancer authenticates against a Cognito user pool that admits one admin user and no one
else (issue E2-60), so the user the header names is created on its first request. The default admin
account would otherwise keep its documented default password (evidence E24).

Grafana shares the endpoint's one domain and certificate (issue E2-47), under the path prefix
/grafana/, which is also the path the web ACL's Grafana rule is scoped to (issue E2-48). Grafana
reads Managed Prometheus, CloudWatch metrics and CloudWatch Logs (section 7.1). The
dashboards are built from the deployed configuration at synthesis (issues E2-61 and E2-80) and
reach the image through its build context; in the repository the dashboards folder holds none. Each
data source has a fixed identifier, so that a dashboard names the one it queries. The image
installs no Python library: its build context is its own folder (issue E2-80 adds the dashboards),
not the context issue E1-16 fixes for a Python service's image. Grafana listens on its own default
port, 3000, which the load balancer's target group of issue E2-60 uses.

Spike S10 asks that the pinned configuration refuses everything else. This issue makes the
assertions that need no container, on the configuration files. The test that starts the pinned image
is issue E2-65, under `tests/container/` (section 8.3), and the deployed check is on issue E2-60.

## Deliverables

- [ ] `services/grafana/Dockerfile` builds from the upstream Grafana OSS image pinned by digest, adds the configuration and the provisioning files, and copies the dashboard files of its build context's dashboards folder.
- [ ] `services/grafana/dashboards/` exists in the repository and holds no dashboard.
- [ ] `services/grafana/grafana.ini` serves Grafana on port 3000 under the path prefix /grafana/, trusts the identity header the load balancer sets and signs in the user it names, creating that user on its first request.
- [ ] It turns off the login form, basic authentication, the sign-up form, anonymous access and the creation of the initial admin account.
- [ ] `services/grafana/provisioning/` declares two data sources, each with a fixed identifier, and nothing else: the Prometheus workspace, with signed requests and its address from the environment, and CloudWatch, for metrics and logs; and a provider that loads dashboards from the image's dashboards folder.
- [ ] `tests/unit/grafana/test_settings.py` reads the three and checks each setting.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/grafana/test_settings.py` passes: each of the five settings is off, Grafana serves on port 3000 from the path prefix /grafana/, the proxy header is the one the load balancer sets, proxy sign-in creates its user, and the base image has a digest.
- [ ] `tests/unit/grafana/test_settings.py::test_only_two_data_sources` fails when a provisioning file declares a third data source or a credential, or when a data source has no fixed identifier.

## References

- `docs/design/system-design.md`, sections 7.1, 7.4, 8.3, 12, 15
- `docs/design/evidence.md`, E24
