+++
id = "E1-01"
title = "Pin the CDK toolchain"
labels = ["size:S", "kind:infra"]
epic = "E1"
+++

## Context

Nothing can be synthesized, locally, in CI or in the pipeline, until the CDK library and its CLI are
pinned. The design keeps one uv project for all Python and a Node package file that pins the CDK CLI
and nothing else (section 8.1). The versions below are the ones evidence E20 records; a later
release is fine, a range is not. Evidence E20 records no version for constructs: it is pinned at the
version the lock resolves for aws-cdk-lib.

The CDK libraries go in the project's own dependencies, not in a dependency group: they are what the
app runs on. The services' libraries go in dependency groups (issue E1-16), so that no function or
image carries the CDK.

**Node.** The CDK CLI supports Node 22 and 24, and CI's synth job installs Node 24. The synthesis
tests run aws-cdk-lib through jsii, which starts the `node` it finds on the path, so they need a
supported Node too. A machine's default Node may be newer (this host's is 26). So the repository
says which Node it expects in two places, and enforces neither, because a strict engine check would
make `npm ci` fail on such a machine instead of warning:

- `.node-version` names Node 24, the file version managers read (mise when its idiomatic version
  files are on, fnm, nodenv, and `actions/setup-node` through its node-version-file input);
- `package.json` declares the engines Node 22 or 24, which npm reports as a warning when another
  Node runs it.

`setup/README.md` says how to get that Node; making it the default on the factory's host is the
owner's step (it is on that host's PATH that the sandbox finds `node`).

`cdk.json` is not part of this issue. `scripts/check.sh synth` and CI's job synth start the CLI as
soon as `cdk.json` exists, and there is no `app.py` to start yet, so `cdk.json` arrives with the
empty app (issue E1-04) and `main` stays green in between.

AGENTS.md asks that every new tool be recorded, with what it installs and where, in the
installation inventory of `setup/README.md`.

## Deliverables

- [ ] `pyproject.toml` and `uv.lock` pin, in the project's dependencies, aws-cdk-lib, constructs and cdk-nag to exact versions, each at or above the version evidence E20 records (aws-cdk-lib 2.276.0, cdk-nag 3.0.2) and constructs at the version the lock resolves for aws-cdk-lib.
- [ ] `package.json` and `package-lock.json` pin the CDK CLI to an exact version, 2.1148.1 or later, declare the engines Node 22 or 24 without making them strict, and hold no other dependency.
- [ ] `.node-version` names Node 24, and `setup/README.md` lists, in its installation inventory, the CDK libraries installed into the project environment from the uv lock, the CDK CLI installed into node_modules from the npm lock and never globally, and the Node that `.node-version` names, on which the CLI and the synthesis tests run, with how a version manager picks it up.
- [ ] `tests/unit/test_toolchain.py` reads `pyproject.toml`, `package.json` and `.node-version` and checks the pins, the single Node dependency and the Node version.

## Acceptance criteria

- [ ] `uv sync --locked` succeeds with no AWS credentials in the environment.
- [ ] `uv run --locked pytest tests/unit/test_toolchain.py` passes: every CDK pin is exact, and `.node-version` names a Node major that the engines field allows.
- [ ] `tests/unit/test_toolchain.py::test_a_range_fails` shows the pin check refusing a copy of `pyproject.toml` in which a CDK pin is loosened to a range, and a copy of `package.json` whose CLI version is a range.
- [ ] `tests/unit/test_toolchain.py::test_the_cli_is_the_only_node_dependency` fails when `package.json` gains a second dependency.

## References

- `docs/design/system-design.md`, sections 8.1, 10
- `docs/design/evidence.md`, E20
- `AGENTS.md`, section "Toolchain"
