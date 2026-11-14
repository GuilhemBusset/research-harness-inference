+++
id = "E1-04"
title = "An environment-agnostic app that synthesizes with no credentials"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E1-01", "E1-02", "E1-03"]
+++

## Context

Every later issue adds a construct to this app and a test over its template. The app must synthesize
on a machine that has never seen an AWS account: no context lookup, no account or region in any
stack, no `cdk.context.json` (sections 8.3 and 10). Pull-request checks and the agents' sandbox both
depend on it.

`cdk.json` arrives here rather than with the toolchain (issue E1-01): `scripts/check.sh synth` and
CI's job synth start the CDK CLI as soon as it exists, so it comes with the app it starts.

**The app sees every package root.** The project is not installed (`package = false` in
`pyproject.toml`), and the package roots of issue E1-02 (`src`, `services/common` and the services')
are on pytest's import path only. `cdk synth` starts `app.py` as a script, which sees the
repository's root and nothing else, so the app could not import `rhi`, and the first construct
module that imports `rhi_common` (the configuration's pool parser, issue E2-69) would break
synthesis while every test passed. So `app.py`, before importing anything of the repository, puts
on its import path the folders that `pyproject.toml` puts on pytest's, read from that file with the
standard library: one list for the tests and the app. The pipeline's synth step gives its scripts
the same folders (issue E1-10).

The configuration loader fails closed when no source is named (issue E1-03). Pull-request checks
synthesize against the example configuration (section 8.3), and tests use it whatever the
environment (section 10), so the gate script names the example source itself when it synthesizes.
`scripts/check.sh` is a protected path on the safety surface (ADR 0001, decision 10); the builder
may change its synth step because this issue names it. The pipeline does not use the gate script:
its synth step names its own source (issue E1-10).

The pipeline stack (issue E1-09) will deploy this stage as a nested assembly of the cloud assembly,
so the tests read every template of the assembly, nested ones included, from the start. They also
synthesize with bundling skipped for every stack (the CDK context aws:cdk:bundling-stacks set to an
empty list): a function's bundle is built from the package index (issue E1-16), and a test never
needs the network. Nothing is bundled yet, so the setting changes nothing today.

**The first half of spike S11 is answered here.** Spike S11 asks whether ty, which is beta, gives a
clean gate on code written against the current aws-cdk-lib (section 12). This is the first issue
that puts CDK code in ty's scope, and CI fails on a warning, so the answer cannot wait for the
spike's own issue (E1-12): the gate must pass here. Where ty misreads aws-cdk-lib, the code carries a
ty ignore comment naming its rule and its reason, as AGENTS.md allows; issue E1-12 records how many
the CDK code needed.

## Deliverables

- [ ] `cdk.json` starts the app with the command `uv run --locked python app.py` and holds CDK feature flags only: no context value that names an account, a region or a resource.
- [ ] `app.py`, before importing anything of the repository, puts on its import path the folders that pyproject.toml puts on pytest's, read from that file, and then builds the app from the loaded configuration, with no account, no region and no context lookup.
- [ ] `src/rhi/stage.py` defines the stage the pipeline deploys, with one stack that holds no resource yet.
- [ ] `scripts/check.sh` synthesizes against the example configuration whatever the caller's environment, by setting RHI_CONFIG to example for its synth step only.
- [ ] `tests/synth/conftest.py` synthesizes the app once per test session from the example configuration, with bundling skipped for every stack, into a temporary directory, and gives the tests every template of the cloud assembly, those of nested stage assemblies included; `tests/synth/test_app.py` checks the templates, `cdk.json` and the app's import path.

## Acceptance criteria

- [ ] `scripts/check.sh synth` exits 0 with every `AWS_*` variable and RHI_CONFIG unset in the caller's shell, and no `cdk.context.json` in the tree.
- [ ] `uv run --locked pytest tests/synth/test_app.py` passes: no template holds a 12-digit account ID or a region name, and synthesis writes nothing outside its temporary directory.
- [ ] `tests/synth/test_app.py::test_cdk_json_holds_flags_only` fails when `cdk.json` gains a context key that is not a CDK feature flag.
- [ ] `tests/synth/test_app.py::test_the_app_sees_every_package_root` starts a child Python on `app.py`'s path setup, with PYTHONPATH unset and its working directory outside the repository, and finds every folder of pytest's import path on the child's import path and `rhi_common` importable in it.
- [ ] `scripts/check.sh types` exits 0 with `app.py` and `src/rhi/` in ty's scope, which answers the first half of spike S11.

## References

- `docs/design/system-design.md`, sections 8.1, 8.3, 10, 12
- `AGENTS.md`, section "Validating a change"
- `docs/adr/0001-software-factory.md`, decision 10
