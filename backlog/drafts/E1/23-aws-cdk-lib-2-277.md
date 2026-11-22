+++
id = "E1-23"
title = "aws-cdk-lib 2.277.0, with cdk-nag's runtime rule acknowledged on the CDK's custom resource function"
labels = ["size:S", "kind:infra"]
epic = 3
+++

## Context

Dependabot's pull request #573 bumps aws-cdk-lib from 2.276.0 to 2.277.0 and fails `synth` and
`tests`: with 2.277.0, cdk-nag's rule AwsSolutions-L1 ("the non-container Lambda function is not
configured to use the latest runtime version") fires on one function, the one the CDK puts at the
root of the Pools stack to run its AWS SDK custom resources (the group's zone rebalancing in
`src/rhi/pools/group.py`, the release's image digest in `src/rhi/pools/release.py`).
`src/rhi/pools/stack.py` already finds that function by `PROVIDER_FUNCTION_UUID`.

The project does not choose that function's runtime: the CDK does, and moves it when its own
default moves. The project's own Python functions already acknowledge AwsSolutions-L1 in their
modules' files (`pools_scaler.toml`, `pools_release.toml`, `access_issuer.toml`,
`pipeline_reporter.toml`), because they run on the Python that `.python-version` names. So the
finding is acknowledged in `pools_stack.toml`, the file that already holds that function's other
acknowledgements, with its reason; the provider's runtime is not overridden. A local trial of
exactly this change made `scripts/check.sh synth` pass and the tests under `tests/policy/` and
`tests/synth/` pass.

The acknowledged findings are an invariant (`tests/policy/acknowledged/README.md`), which is why
this issue names `tests/policy/acknowledged/pools_stack.toml`.

## Deliverables

- [ ] `pyproject.toml` pins aws-cdk-lib==2.277.0 and `uv.lock` locks aws-cdk-lib at version 2.277.0.
- [ ] `tests/policy/acknowledged/pools_stack.toml` acknowledges AwsSolutions-L1 under its nag table, with a reason saying that the CDK chooses the runtime of the function that runs the stack's AWS SDK custom resources.

## Acceptance criteria

- [ ] `grep -q '"aws-cdk-lib==2.277.0"' pyproject.toml` exits 0 and `uv sync --locked` exits 0.
- [ ] `scripts/check.sh synth` exits 0.
- [ ] `uv run --locked pytest tests/policy tests/synth` passes.

## References

- `tests/policy/acknowledged/README.md`
- `src/rhi/pools/stack.py`
- `docs/design/system-design.md`, section 8.4
