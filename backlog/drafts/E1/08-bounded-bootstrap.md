+++
id = "E1-08"
title = "A bootstrap with a project execution policy and a permissions boundary"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E0-02", "E1-06"]
+++

## Context

By default the CDK bootstrap gives CloudFormation administrator access, so any merged template could
do anything in the account (section 8.4, control 1; evidence E20). The design bootstraps the account
with a project policy and a permissions boundary instead, so that a merged change cannot create IAM
users, access keys or identity providers, whatever the template says.

Section 8.4 also names trust to another account among what a merged change cannot create. IAM has no
condition key that reads a role's trust policy, so neither the execution policy nor the boundary can
deny it. What they can do is make every role a deployment creates carry the boundary, which bounds
what such a role could do; the invariant itself is the policy test over the templates (issue
E1-07). The boundary must not claim more than IAM can enforce.

The policies are a CloudFormation template the owner deploys once, before the bootstrap (section
8.6, step 6). It names no account: the account and the region come from pseudo parameters, and the
region is the one of the owner's credentials, which decision 9 chooses.

This issue creates `tests/policy/test_bootstrap_policies.py` under `tests/policy/`, a protected path
(ADR 0001, decision 10), and `setup/bootstrap/`, which ADR 0001's first amendment makes a protected
path from the first issue that creates it (issue E0-02 adds it to the engine's list).

## Deliverables

- [ ] `setup/bootstrap/policies.yaml` creates two managed policies with fixed names: the execution policy CloudFormation deploys with, and the permissions boundary that every role created by a deployment must carry.
- [ ] Together they deny creating IAM users, access keys and login profiles; creating SAML and OpenID Connect providers; creating a role without the boundary; removing or replacing a role's boundary; and changing or deleting either policy.
- [ ] `setup/bootstrap/README.md` gives the owner's two commands, with placeholders only: deploy the policies, then bootstrap with the execution policy and the boundary; and says that trust to another account is held by the policy tests, not by these policies.
- [ ] `tests/policy/test_bootstrap_policies.py` asserts each denial on the parsed template.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_bootstrap_policies.py` passes, and fails when any one denial is removed from `setup/bootstrap/policies.yaml`.
- [ ] `uv run --locked pytest tests/synth/test_cfn_lint.py` passes with `setup/bootstrap/policies.yaml` among the templates it lints.
- [ ] The account is bootstrapped, in the region decision 9 chose, with the execution policy and the boundary, and CloudFormation's execution role no longer has administrator access (needs aws:account).

## References

- `docs/design/system-design.md`, sections 8.4, 8.6, 15
- `docs/adr/0001-software-factory.md`, amendment 1
- `docs/design/evidence.md`, E20
