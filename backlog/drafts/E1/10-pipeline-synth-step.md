+++
id = "E1-10"
title = "The synth step: configuration from SSM, a leak check, synthesis, tests; the first deployment"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E1-09", "E1-17"]
+++

## Context

The synth step is the one place that knows the real configuration (section 8.2, step 2). It reads
it from SSM into a file, checks that none of the private values is in the source tree (section 10:
no public pattern could name the real domain or e-mail address without leaking them), synthesizes
with lookups off, and runs the tests, which use the example configuration whatever the environment
(section 10). Its role can read the configuration prefix, /rhi/config/ (issue E1-17), and nothing
else. The SSM source and the writer that produces the file are issue E1-17's.

The private values are those of the keys the configuration loader names as private (issue E1-03):
the domain, the hosted zone ID, the admin e-mail address, the instructor's phone number and the
login app's client ID. Numbers, dates and public values such as a pool's model name are not
searched for: they would match everywhere. A hit fails the step, which is how the pipeline alerts
the owner; the value itself never appears in the step's output, which ends up in build logs.

**The fetched file is outside the source tree.** The source CodeBuild receives from the pipeline has
no `.git`, so the leak check cannot tell an untracked file from a tracked one: a configuration file
written in the checkout, such as config.local.toml at its root, would be found by the very check
that follows, and every execution would fail. So the step sets RHI_CONFIG_FILE to a file in the
build's temporary directory, outside the source tree; the fetch writes there, the leak check reads
the private values from there, and synthesis reads the source file from there (issue E1-03). The
leak check refuses to run when that file lies inside the tree it searches, so a later change that
moved it back would fail with that reason rather than with a leak.

**The scripts find the configuration loader.** The project is not installed (`package = false` in
`pyproject.toml`), so a script started as `python scripts/pipeline/fetch_config.py` sees only its own
folder. The step therefore gives every command the folders that `pyproject.toml` puts on pytest's
import path (issue E1-02) in PYTHONPATH, read from that file at synthesis, as `app.py` puts them on
its own import path (issue E1-04).

Synthesis here bundles the functions for real (issue E1-16): uv installs their wheels from the
package index, as `uv sync --locked` already does in the same step.

With this step in place the pipeline can be deployed: this is the issue that carries the owner's
first deployment and the first exit criterion of phase 0 (section 14), the pipeline deploying the
empty stage. The bounded bootstrap (issue E1-08) and the configuration in SSM, written by the
owner's walkthrough (issue E1-13, section 8.6, step 4), come before it.

`src/rhi/pipeline/stack.py` builds its synth step from the module this issue adds, in place of the
one issue E1-09 left without a configuration source. The synth step's project and role are created
by CDK Pipelines inside the pipeline construct of that module, so their cdk-nag findings and
wildcard actions belong to its acknowledgement file, `tests/policy/acknowledged/pipeline_stack.toml`.
`src/rhi/pipeline/` and `tests/policy/` are protected paths (ADR 0001, decision 10 and amendment 1);
the builder may change the files this issue's Deliverables name.

## Deliverables

- [ ] `src/rhi/pipeline/synth.py` defines the synth step, which runs in this order: a pinned uv and the CDK CLI of the npm lock file, `uv sync --locked`, the configuration fetch, the leak check, `cdk synth --lookups=false` with RHI_CONFIG set to file, the tests; the step sets RHI_CONFIG_FILE to a file in the build's temporary directory, outside the source tree, and PYTHONPATH to the folders pyproject.toml puts on pytest's import path; and `src/rhi/pipeline/stack.py` builds the pipeline's synth step from it.
- [ ] `scripts/pipeline/fetch_config.py` reads the configuration from SSM with the configuration loader, writes it with the configuration writer to the file RHI_CONFIG_FILE names, and prints no value.
- [ ] `scripts/pipeline/leak_check.py` reads the configuration from the file RHI_CONFIG_FILE names, searches the source tree for the value of every private key of that configuration, in any letter case, and exits non-zero with the file and line of each hit, never with the value; it exits non-zero, naming the path and not a value, when that file lies inside the tree it searches.
- [ ] The synth role may read the parameters under /rhi/config/ and no other parameter or secret; `tests/policy/test_pipeline.py` fails when it may read more.
- [ ] Every cdk-nag finding `src/rhi/pipeline/stack.py` brings with the new synth step is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_stack.toml`.
- [ ] `tests/unit/test_leak_check.py` covers the leak check, and `tests/synth/test_pipeline_synth.py` the step and its role.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_leak_check.py` passes: a private value planted in a scratch tree is reported by file and line, in any letter case, the value itself is in neither the output nor the exit message, and the value of a key that is not private is not reported.
- [ ] `tests/unit/test_leak_check.py::test_the_fetched_file_is_not_a_leak` shows a clean scratch tree passing while RHI_CONFIG_FILE names a file outside it that holds every private value, and the check exiting non-zero without printing a value when RHI_CONFIG_FILE names a file inside the tree.
- [ ] `uv run --locked pytest tests/synth/test_pipeline_synth.py tests/policy/test_pipeline.py` passes: the step's commands are in the order above, RHI_CONFIG_FILE names a path outside the source tree, PYTHONPATH holds the folders of pytest's import path, and the role reads /rhi/config/ only.
- [ ] The owner deploys the pipeline stack once from a laptop, with the configuration source ssm, after the bounded bootstrap and with the configuration in SSM (needs aws:account).
- [ ] The owner authorizes the pipeline's GitHub connection in the console, for this repository only (needs aws:console).
- [ ] A push to `main` starts an execution that passes the leak check and deploys the empty stage, which is the first exit criterion of phase 0 (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2, 8.6, 10, 14
- `docs/adr/0001-software-factory.md`, decision 10 and amendment 1
- `tests/policy/acknowledged/README.md`, delivered by the invariants issue
