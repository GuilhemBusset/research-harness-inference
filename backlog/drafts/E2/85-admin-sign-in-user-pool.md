+++
id = "E2-85"
title = "The admin's sign-in: a Cognito user pool with one user and MFA required"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-05", "E2-49"]
+++

## Context

Grafana sits behind the load balancer's own sign-in, which authenticates against a Cognito user pool
with one admin user and mandatory MFA; unauthenticated requests never reach Grafana (section 7.4).
The only manual setup is the admin's e-mail address in configuration (section 7.4), a key the owner
already writes (issue E1-03). This issue creates the user pool; the Grafana service and the load
balancer rule that authenticates against it are issue E2-60, which waits for it, so that Grafana is
never reachable before its sign-in exists.

MFA is required with an authenticator app (time-based one-time passwords): SMS would need a role
that can send text messages and a phone number, and the design asks for neither.

**The sign-in domain.** The load balancer's authentication action needs the user pool's hosted
sign-in domain, whose prefix becomes a host name under AWS's Cognito domain. A host name of a
deployed resource must not be in the repository (section 10), so the prefix is computed when the
stack is deployed, from the stack's own ID, which CloudFormation gives at deployment: no template
synthesized from the repository holds it. The client the load balancer uses has a secret, which
the load balancer requires and Cognito generates, so that no template holds it; it has the
authorization code flow, the openid scope and, as its callback, the configured domain's path /oauth2/idpresponse,
which is where an Application Load Balancer receives the answer.

**State.** The user pool holds the admin's account and MFA enrolment; replacing it would lock the
admin out. It is a stateful resource: it has deletion protection and a retain policy, and its
logical ID is recorded, with the protections issue E2-88 lists for a user pool.

This issue adds the user pool to `tests/policy/stateful_logical_ids.json`, and changes
`tests/policy/acknowledged/`. `tests/policy/` is a protected path (ADR 0001, decision 10): the
builder may write the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/access/sign_in.py` creates a Cognito user pool with self sign-up off, MFA required with an authenticator app, and one admin user whose address is the configured admin e-mail address; `src/rhi/access/stack.py` holds it.
- [ ] The user pool has a hosted sign-in domain whose prefix is computed at deployment from the stack's ID, and a client for the load balancer with a secret Cognito generates, the authorization code flow, the openid scope and the configured domain's path /oauth2/idpresponse as its callback.
- [ ] The user pool has deletion protection and a retain policy on deletion and on replacement, and `tests/policy/stateful_logical_ids.json` records it.
- [ ] Every cdk-nag finding `src/rhi/access/sign_in.py` or `src/rhi/access/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/access_sign_in.toml` or `tests/policy/acknowledged/access_stack.toml`.
- [ ] `tests/synth/test_sign_in.py` checks the user pool, its domain and its client.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_sign_in.py tests/policy/test_stateful_resources.py` passes: sign-up is off, MFA is required, there is one user, the user pool has deletion protection and a retain policy, and its logical ID is recorded.
- [ ] `tests/synth/test_sign_in.py::test_the_domain_prefix_is_computed_at_deployment` finds the domain's prefix built from the stack's ID by an intrinsic function, and no literal prefix in any template.
- [ ] `scripts/check.sh synth` exits 0 with the user pool in the stage.

## References

- `docs/design/system-design.md`, sections 7.4, 10
