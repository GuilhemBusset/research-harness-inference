+++
id = "E1-07"
title = "Invariants: no wildcard IAM, no identity provider, no trust to another account"
labels = ["size:M", "kind:test"]
epic = "E1"
blocked_by = ["E1-04"]
+++

## Context

A merge to `main` is deployment rights in the account (section 8.4). Three of the invariants the
design asks for (control 2) can be written before any real construct exists, so that every later
issue is held to them from its first commit: no wildcard IAM, no identity provider, no cross-account
trust. The last one has no other control: no IAM condition key reads a role's trust policy, so the
bounded bootstrap (issue E1-08) cannot deny it, and this test over the templates is what holds it.

A template is environment-agnostic (issue E1-04), so the only way it can name the deploying account
is the AWS::AccountId pseudo parameter. A principal built from it is the same account; any other
account in a principal is another account.

**Acknowledged findings, one file per construct module.** Some IAM actions are granted by AWS only
on every resource (the Describe and List calls of EC2, Auto Scaling and Elastic Load Balancing,
`ecr:GetAuthorizationToken`, `cloudwatch:PutMetricData`, among others), and cdk-nag (issue E1-05)
raises findings that a construct cannot always fix. Both are accepted only with a reason, and the
list of accepted ones is itself an invariant (section 8.4, control 2). One shared list would make
every construct issue of the backlog change the same protected file, so the list is split by the
module that creates the resources:

- `tests/policy/acknowledged/` holds one TOML file per construct module, named after the module's
  path under `src/rhi/`, with `/` replaced by `_` and without `.py`: `src/rhi/pools/group.py` has
  `tests/policy/acknowledged/pools_group.toml`, `src/rhi/stage.py` has `stage.toml`, and
  `src/rhi/pipeline/stack.py` has `pipeline_stack.toml`.
- A file has at most two tables. `[nag]` maps a cdk-nag finding id, exactly as cdk-nag reports it
  (a rule id such as `AwsSolutions-S1`, or a rule id with its finding in brackets such as
  `AwsSolutions-IAM5[Resource::*]`: cdk-nag 3 acknowledges each finding by its own id and has no
  prefix matching), to its reason. `[wildcard_actions]` maps an IAM action that AWS grants only on
  every resource to its reason. A reason is at least one sentence.
- **Which module created a resource:** the module that defines the class of the resource's nearest
  enclosing construct whose class is defined under `src/rhi/`. A role's default policy is a child of
  the role, so a statement belongs to the module that created the role or policy that holds it.
  Every stack is a class defined under `src/rhi/`, so every resource has such a construct; one that
  has none fails the tests.

Every later issue that adds or changes a construct module names that module's file in its
Deliverables, so that issues on different modules never wait for each other on one file. Issue
E1-05 reads the `[nag]` tables; this issue reads the `[wildcard_actions]` tables and checks every
file.

"No wildcard IAM" applies to identity policies: AWS::IAM::Policy, AWS::IAM::ManagedPolicy and the
inline policies of AWS::IAM::Role. A resource policy, such as a KMS key policy, names its own
resource as `*` and is not read by this test. An action with a wildcard in its name (CDK's grants
write `s3:GetObject*`) is not "every action"; only `*` is.

The policy tests synthesize the app in their own protected fixture, so that no unprotected file
decides which templates they see, or which module a resource belongs to. The fixture synthesizes
with bundling skipped for every stack (the CDK context aws:cdk:bundling-stacks set to an empty
list), so that no test builds a function bundle (issue E1-16) or needs the network; a skipped bundle
changes no IAM statement.

This issue creates `tests/policy/conftest.py`, `tests/policy/acknowledged/README.md`,
`tests/policy/test_acknowledged.py`, `tests/policy/test_iam_wildcards.py` and
`tests/policy/test_trust.py`. `tests/policy/` is a protected path (ADR 0001, decision 10): the
builder may write these files because this issue names them. Later issues add their own files beside
them.

## Deliverables

- [ ] `tests/policy/conftest.py` gives the tests every template of the app synthesized from the example configuration with bundling skipped for every stack, the pipeline's own stack once it exists and every stack of the stage, and, for every resource of those templates, the module of the app's source tree that created it, by the rule in Context.
- [ ] `tests/policy/acknowledged/README.md` states the convention of Context: one file per construct module and how it is named, the two tables and what their keys are, a reason of at least one sentence for every entry, and the rule that says which module created a resource.
- [ ] `tests/policy/test_acknowledged.py` fails when a file of the acknowledged folder names a module that does not exist in the app's source tree, holds a table other than nag and wildcard_actions, or holds an entry whose reason is empty or not a sentence.
- [ ] `tests/policy/test_iam_wildcards.py` fails on an identity policy statement that allows every action, and on one that allows an action on every resource unless the acknowledgement file of the module that created the role or policy holding it lists that action in its wildcard_actions table.
- [ ] `tests/policy/test_trust.py` fails on an IAM user, an access key, a login profile, a SAML or OpenID Connect provider, and on a role whose trust policy names a principal of an account other than the deploying one.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_acknowledged.py tests/policy/test_iam_wildcards.py tests/policy/test_trust.py` passes on the app as it stands.
- [ ] `tests/policy/test_iam_wildcards.py::test_detects_a_wildcard` and `tests/policy/test_trust.py::test_detects_each_forbidden_resource` fail templates built in the test that carry each forbidden thing.
- [ ] `tests/policy/test_iam_wildcards.py::test_an_acknowledged_action_is_admitted_for_its_module_only` admits an action on every resource for a role whose module's file lists it, and refuses the same statement on a role created by another module.
- [ ] `tests/policy/test_acknowledged.py::test_refuses_a_bad_file` fails a folder, built in the test, holding a file for a module that does not exist, and one holding an entry without a reason.
- [ ] `tests/policy/test_trust.py::test_the_deploying_account_is_not_another` accepts a role, built in the test, whose trust policy names the AWS::AccountId pseudo parameter, and refuses the same role naming the account 123456789012.

## References

- `docs/design/system-design.md`, section 8.4
- `docs/adr/0001-software-factory.md`, decisions 9 and 10
- `docs/design/evidence.md`, E20
