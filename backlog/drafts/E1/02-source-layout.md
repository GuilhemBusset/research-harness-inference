+++
id = "E1-02"
title = "Lay out the source tree so that every tool sees it"
labels = ["size:M", "kind:infra"]
epic = "E1"
+++

## Context

Section 8.1 of the design fixes where code lives. Later issues add modules to these packages in
parallel; if each of them also had to teach ruff, ty and pytest about a new folder, they would all
collide on the same configuration files. This issue creates every Python root once, empty, and
points the tools at them.

The package roots and their import names: `src` (`rhi`, the CDK constructs), `services/common`
(`rhi_common`, what the services share), `services/gateway` (`rhi_gateway`), `services/issuer`
(`rhi_issuer`), `services/scaler` (`rhi_scaler`), `services/reporter` (`rhi_reporter`),
`services/engine/lifecycle` (`rhi_lifecycle`), `services/engine/mock` (`rhi_mock_engine`), `client`
(`rhi_cli`), `admin` (`rhi_admin`) and `dashboards` (`rhi_dashboards`). All of them are in the
design's layout. `scripts/pipeline` is a root too: it holds the scripts the pipeline's steps run
(section 8.1), which are modules the tests import by name (`fetch_config`, `leak_check` and those
of later issues), not a package. ty refuses a root folder that does not exist, so that folder gets
a README now.

Several later issues give test files the same name in different folders (`tests/synth/` and
`tests/policy/` each get a `test_pipeline.py`, for instance). Under pytest's default import mode,
two test modules of one name without packages around them fail to collect, so the test layout has
to allow it from the start.

## Deliverables

- [ ] `src/rhi/__init__.py` and one empty package under each of `services/common/`, `services/gateway/`, `services/issuer/`, `services/scaler/`, `services/reporter/`, `services/engine/lifecycle/`, `services/engine/mock/`, `client/`, `admin/` and `dashboards/`, with the import names listed in Context.
- [ ] `scripts/pipeline/README.md` says that the folder holds the scripts the pipeline's steps run, one module each.
- [ ] `pyproject.toml` puts every package root and the scripts/pipeline folder on pytest's import path, and `ruff.toml` and `ty.toml` list the same roots and include the app's entry point, the roots and the tests.
- [ ] `pyproject.toml` configures pytest so that two test files of the same name in different folders of the tests tree are both collected and run.
- [ ] `tests/unit/test_layout.py` imports each package by its name.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_layout.py` passes.
- [ ] `tests/unit/test_layout.py::test_same_named_test_files_both_run` runs pytest, with the repository's configuration, over two test files of one name in two folders of a temporary directory, and both of their tests pass.
- [ ] `scripts/check.sh fast` exits 0 with the new roots in the scope of ruff and ty.

## References

- `docs/design/system-design.md`, section 8.1
- `AGENTS.md`, section "Where tests live"
