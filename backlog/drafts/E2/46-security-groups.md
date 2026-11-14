+++
id = "E2-46"
title = "Security groups, and the invariant that nothing but the public load balancer faces the internet"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-07", "E2-45"]
+++

## Context

The public load balancer reaches only the gateway, the issuer and Grafana; only the gateway reaches
the internal load balancer; only the internal load balancer reaches port 8000. No security group for
an instance or a task admits traffic from the internet, and a test asserts it (section 6.5). That
test is the first invariant of section 8.4.

The design names one port, the engine's 8000 (section 4.1). The others are fixed here, and the
services that listen on them use the same numbers: the gateway's tasks listen on 8080 (issue E2-30),
Grafana's on 3000, its own default (issue E2-60), and the internal load balancer on 80 (issue E2-44).
The issuer is a function behind the public load balancer and needs no group.

The groups join the network stack. The public load balancer's group admits 443 from anywhere, which
cdk-nag reports; the finding is acknowledged with the reason that it is the edge.

This issue creates `tests/policy/test_no_public_ingress.py`. `tests/policy/` is a protected path
(ADR 0001, decision 10): the builder may write the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/network/security.py` creates one security group each for the public load balancer, the gateway's tasks, Grafana's tasks, the internal load balancer and the pools' instances, and `src/rhi/network/stack.py` exposes them to the other stacks.
- [ ] The public load balancer's group admits port 443 from anywhere and nothing else; the gateway's admits port 8080 and Grafana's port 3000, each from the public load balancer's group only; the internal load balancer's admits port 80 from the gateway's group only; the instances' admits port 8000 from the internal load balancer's group only.
- [ ] Every cdk-nag finding `src/rhi/network/security.py` or `src/rhi/network/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/network_security.toml` or `tests/policy/acknowledged/network_stack.toml`.
- [ ] `tests/policy/test_no_public_ingress.py` fails when any security group other than the public load balancer's admits an address range open to every IPv4 or IPv6 address, or when that one admits it on a port other than 443.
- [ ] `tests/synth/test_security_groups.py` checks the five groups and who reaches whom on which port.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_security_groups.py tests/policy/test_no_public_ingress.py` passes on the app as it stands: the five groups admit 443, 8080, 3000, 80 and 8000 as stated, each from its one source.
- [ ] `tests/policy/test_no_public_ingress.py::test_detects_an_open_rule` fails templates, built in the test, with a rule open to every address on an instance's group, as an inline rule and as a separate ingress resource, in IPv4 and in IPv6.

## References

- `docs/design/system-design.md`, sections 4.1, 6.5, 8.4
