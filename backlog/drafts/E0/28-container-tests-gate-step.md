+++
id = "E0-28"
title = "A gate step for the tests that start a container, kept out of the local gate"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

Some checks can only be made by starting a container: the first is the Grafana sign-in test of the
design's section 7.4 (issue E2-65). The coding agents' host has no container runtime. The design
therefore puts such tests under `tests/container/`, outside the local gate, and runs them in a CI
job of their own that is part of the required check (`docs/design/system-design.md`, section 8.3;
ADR 0001, amendment 5). An issue that needs one names the capability `host:container-runtime`.

`scripts/check.sh` stays the one gate script: CI's jobs call its steps. This issue adds the step and
everything around it that the loop can land by itself. The CI job that calls the step is issue
E0-17, a change to a workflow file that only the owner can land (ADR 0001, amendment 2; issue
E0-23). That change is kept to the one workflow file, so that while it waits for the owner it holds
nothing that `main` can move past: no shared file, no lock, no gate script.

The test of the workflow reads it as YAML. The standard library has no YAML parser, so the test uses
one that `uv.lock` pins for the dev group. The test holds rules that are true of CI's workflow
before and after issue E0-17 (every job is needed by `required` and checked by its step; no job is
given a secret; a job that runs the step container-tests is named container-tests, both its id and
its name, because issue E0-19 reads the check run by that name), so that it passes on the workflow
as it stands and then holds issue E0-17's change to the same rules.

This issue changes `scripts/check.sh`, a protected path on the safety surface (ADR 0001, decision
10); the builder may change it because this issue names it. It does not change any file under
`.github/`.

## Deliverables

- [ ] `scripts/check.sh` gains a step container-tests, which runs the tests under `tests/container/` with the locked pytest; the steps fast and all do not run it.
- [ ] The step fails when no container runtime answers, and, under CI, when one of its tests is skipped; while the folder holds no test it says so and passes.
- [ ] `pyproject.toml` keeps `tests/container/` out of the default test run, so that the step tests starts no container, and `pyproject.toml` and `uv.lock` pin in the dev group the YAML parser the workflow's test uses.
- [ ] `tests/container/README.md` says what belongs in the folder, that the local gate does not run it, that the CI job container-tests does, and that an acceptance item which rests on it ends with the capability host:container-runtime.
- [ ] `tests/unit/test_ci_workflow.py` reads CI's workflow file as YAML and fails when a job other than required is missing from the needs of required or from the list its step checks, when a job is given a secret, or when a job that runs the step container-tests is not named container-tests by its id and by its name; it also checks that the steps fast and all of the gate script do not run that step.
- [ ] `AGENTS.md` names the step and the folder where it describes validation and where tests live.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_ci_workflow.py` passes on `.github/workflows/ci.yml` as it stands.
- [ ] `tests/unit/test_ci_workflow.py::test_the_local_gate_starts_no_container` shows that the steps fast and all of `scripts/check.sh` do not run the step container-tests, and that the default test run collects nothing under `tests/container/`.
- [ ] `tests/unit/test_ci_workflow.py::test_detects_a_job_left_out_of_required` fails a workflow, built in the test, with a job that required does not need, and one whose job running the step container-tests has another name.

## References

- `docs/design/system-design.md`, sections 7.4, 8.3
- `docs/adr/0001-software-factory.md`, decision 10, amendments 2 and 5
