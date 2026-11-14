+++
id = "E2-48"
title = "A web ACL that limits rates and never inspects or samples a request"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-47"]
+++

## Context

Section 6.5 asks for a per-address rate limit (generous: a campus shares one address), tighter rules
scoped to the issuer and to Grafana, and the AWS reputation list. It forbids two things.
Body-inspection rule groups: the cross-site-scripting rule blocks real coding prompts (evidence
E18). And request sampling that shows the key headers: API keys would sit in WAF samples in
cleartext (evidence E26).

The three rate limits are configuration, with the defaults of section 17, in requests per 5 minutes
per source address: the key `rate_limits`, a table with `per_address` 2,000, `issuer` 100 and
`grafana` 300, each optional. They are read at synthesis and reach no running service. The issuer's
routes all live under `/auth/` (section 6.4) and Grafana's under `/grafana/` (issue E2-60).

The web ACL attaches to the load balancer, which exists whether or not its HTTPS listener is on
(issue E2-47), and joins the network stack.

## Deliverables

- [ ] `src/rhi/network/waf.py` attaches a web ACL to the public load balancer with: a rate limit per source address of rate_limits.per_address requests per 5 minutes; a lower one scoped to the issuer's path prefix /auth/ of rate_limits.issuer; one scoped to Grafana's path prefix /grafana/ of rate_limits.grafana; and the AWS IP reputation list; and `src/rhi/network/stack.py` adds it to the network stack.
- [ ] It holds no rule group that inspects request bodies.
- [ ] Request sampling is off for the ACL and for every rule, and any logging redacts the two key headers.
- [ ] `src/rhi/config.py` reads the optional key rate_limits with per_address 2,000, issuer 100 and grafana 300 by default; `config.example.toml` gives those values, and `tests/unit/test_config.py` covers the defaults.
- [ ] Every cdk-nag finding `src/rhi/network/waf.py` or `src/rhi/network/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/network_waf.toml` or `tests/policy/acknowledged/network_stack.toml`.
- [ ] `tests/synth/test_waf.py` checks the rules, the absence of body inspection and the sampling settings.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_waf.py` passes: three rate rules with limits of 2,000, 100 and 300 requests per 5 minutes under the example configuration, the reputation list, and no rule of the ACL with sampling on.
- [ ] `tests/synth/test_waf.py::test_no_body_inspection` fails an ACL, built in the test, that adds the common rule set or the cross-site-scripting rules.
- [ ] `uv run --locked pytest tests/unit/test_config.py` passes with a configuration that leaves out rate_limits reading 2,000, 100 and 300.

## References

- `docs/design/system-design.md`, sections 6.4, 6.5, 17
- `docs/design/evidence.md`, E18, E26
