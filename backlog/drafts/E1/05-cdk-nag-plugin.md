+++
id = "E1-05"
title = "Fail synthesis on a cdk-nag finding, and acknowledge findings per construct module"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E1-04", "E1-07"]
+++

## Context

The design runs cdk-nag's AWS Solutions pack inside synthesis, as a policy-validation plugin, so
that a finding fails the build in CI and in the pipeline (section 8.2, step 2; evidence E20: cdk-nag
3 is a plugin and its old suppression API is gone). A finding that is accepted needs a reason, and
the list of accepted findings is one of the invariants of section 8.4 (control 2).

The accepted findings live in the per-module acknowledgement files that issue E1-07 defines:
`tests/policy/acknowledged/<area>_<module>.toml`, one per construct module under `src/rhi/`, whose
`[nag]` table maps a finding id, exactly as cdk-nag reports it, to its reason. The files are under
`tests/policy/`, a protected path, so an acknowledgement changes only through an issue that names
the file. Which module created a resource is the rule of that issue: the module that defines the
class of the resource's nearest enclosing construct whose class is defined under `src/rhi/`.

cdk-nag 3 takes acknowledgements through CDK's own API (`Validations.of(construct).acknowledge`
with an id and a reason), one per finding id, and can write every acknowledgement into the
CloudFormation metadata of the resource it covers (its option write_suppressions_to_cloud_formation).
That metadata is how the policy test sees what the plugin acknowledged, so that unprotected code
cannot acknowledge more than the protected files list.

The policy test reads the templates through the protected fixture of issue E1-07,
`tests/policy/conftest.py`, which also tells it which module created each resource.

This issue creates `tests/policy/test_nag_acknowledgements.py` under `tests/policy/`, a protected
path (ADR 0001, decision 10): the builder may write it because this issue names it. It adds no
acknowledgement file: the app holds no resource yet. Later issues that bring a finding to
acknowledge, the pipeline stack first (issue E1-09), name their module's file.

## Deliverables

- [ ] `app.py` adds cdk-nag's AWS Solutions pack as a policy-validation plugin, so that a finding fails synthesis, and has it write every acknowledgement into the template metadata of the resource the acknowledgement covers.
- [ ] `src/rhi/nag.py` acknowledges, on each resource of the app, every entry of the nag table of the acknowledgement file of the module that created it, with that entry's reason, and nothing else.
- [ ] `tests/policy/test_nag_acknowledgements.py`, through the policy fixture of issue E1-07, fails when a resource carries an acknowledgement in its template metadata that the file of its module does not list with the same reason.
- [ ] `tests/synth/test_nag.py` shows the plugin at work on a stack built in the test, with acknowledgement files the test writes to a temporary folder.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_nag.py` passes: a stack with a seeded finding fails synthesis, the same stack synthesizes when the file of the module that created the resource acknowledges the finding, and fails again when the same entry is in another module's file.
- [ ] `uv run --locked pytest tests/policy/test_nag_acknowledgements.py` passes on the app as it stands.
- [ ] `tests/policy/test_nag_acknowledgements.py::test_detects_an_unlisted_acknowledgement` fails a template, built in the test, whose resource carries an acknowledgement that its module's file lacks.
- [ ] `scripts/check.sh synth` exits 0 on the app as it stands.

## References

- `docs/design/system-design.md`, sections 8.2, 8.4
- `docs/design/evidence.md`, E20
- `docs/adr/0001-software-factory.md`, decision 10
- `tests/policy/acknowledged/README.md`, delivered by the invariants issue
