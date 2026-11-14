+++
id = "E2-47"
title = "The public load balancer: the owner's domain, TLS 1.3, no port 80"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-46"]
+++

## Context

The edge is a public Application Load Balancer with an ACM certificate on the owner's domain
(section 6.5). Its idle timeout can reach 4000 seconds, and nothing else in the path caps a long
generation (section 2). The CDK's default security policy still allows TLS 1.1 (evidence E26), so
the listener names the TLS 1.3 security policy AWS recommends,
`ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` at the time of writing, and a policy test pins that
name. There is no listener on port 80: a redirect would mean the key already crossed in cleartext.
Both load balancers run a 1200-second idle timeout (section 5.3).

**The listener waits for the owner's domain.** The certificate is validated by DNS in the hosted
zone whose ID the configuration gives (issue E1-03). Until the owner's domain resolves through that
zone (section 8.6, step 2), validation never completes: the deployment waits, then fails, and every
merge after this one would fail with it. So the certificate, the HTTPS listener and the domain's
alias record exist only when the configuration's key `https_listener` is true. It is false by
default; the design gives no such key, and false keeps the deployed stage as it was. The load
balancer itself, its security group and its web ACL (issue E2-48) exist either way. The owner turns
the key on once the domain resolves. The stack exposes the listener, or none, to the services that
add their rules to it (issues E2-90, E2-52 and E2-60), and each adds them, with its target group, only when it exists.
The example configuration sets the key to true, so that every synthesis and policy test sees the
listener and the rules on it; a configuration that leaves the key out, as the owner's does until
the domain resolves, reads false.

The load balancer joins the network stack.

This issue creates `tests/policy/test_tls_policy.py`, the TLS invariant of section 8.4.
`tests/policy/` is a protected path (ADR 0001, decision 10): the builder may write the files named
below because this issue names them.

## Deliverables

- [ ] `src/rhi/network/edge.py` creates an internet-facing load balancer in the public subnets, with the public load balancer's security group of issue E2-46 and an idle timeout of 1200 seconds, and `src/rhi/network/stack.py` adds it and exposes it and its HTTPS listener, or none, to the other stacks.
- [ ] When https_listener is true, it has one HTTPS listener on port 443 whose default action answers a fixed 404, with an ACM certificate for the configured domain validated by DNS in the hosted zone whose ID comes from configuration, and an alias record points the domain at the load balancer; when it is false, none of the three exists; no hosted zone is looked up at synthesis.
- [ ] The listener names the security policy ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09, and there is no listener on port 80.
- [ ] `src/rhi/config.py` reads the optional key https_listener, false by default; `config.example.toml` gives it true, and `tests/unit/test_config.py` covers its default.
- [ ] Every cdk-nag finding `src/rhi/network/edge.py` or `src/rhi/network/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/network_edge.toml` or `tests/policy/acknowledged/network_stack.toml`.
- [ ] `tests/synth/test_edge.py` checks the load balancer with the key off and the listener with it on, and `tests/policy/test_tls_policy.py` fails when a listener of a public load balancer names another policy than that one, or listens on port 80.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_edge.py tests/policy/test_tls_policy.py` passes: with https_listener false, the load balancer has no listener and there is no certificate; with the example configuration, where it is true, one HTTPS listener on 443 names the policy, its certificate is validated in the configured zone, and nothing listens on port 80.
- [ ] `tests/policy/test_tls_policy.py::test_detects_the_default_policy` fails a template, built in the test, whose listener has no named policy, and one whose listener names the CDK's default policy.
- [ ] `uv run --locked pytest tests/unit/test_config.py` passes with a configuration that leaves out https_listener reading false.
- [ ] Once the owner's domain resolves through the hosted zone the configuration names, the owner sets https_listener to true in the configuration in SSM and the next deployment creates the listener (needs aws:account).
- [ ] In the account, the endpoint answers on the owner's domain over TLS 1.3 and refuses TLS 1.1 (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 2, 5.3, 6.5, 8.6
- `docs/design/evidence.md`, E13, E26
