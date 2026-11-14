+++
id = "E0-17"
title = "Run the tests that start a container in CI, as part of the required check"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
blocked_by = ["E0-23", "E0-28"]
+++

## Context

Tests that start a container live under `tests/container/` and run in a CI job of their own that is
part of the required check (`docs/design/system-design.md`, section 8.3; ADR 0001, amendment 5).
Issue E0-28 adds the gate step that runs them, keeps them out of the local gate, and writes the test
that holds CI's workflow to its rules. This issue adds the job.

The factory's GitHub identity cannot push a change to a workflow file (ADR 0001, amendment 2), so
the engine builds, verifies and certifies the change, then hands the branch to the owner, who
pushes it and merges the pull request the engine opens for it (issue E0-23;
`docs/factory/green-light.md`, step 7). While it waits for the owner, `main` moves. So the change is
`.github/workflows/ci.yml` and nothing else: no lock, no shared file, no gate script that another
issue could change in the meantime. Everything else that the job needs landed through the loop
first, with issue E0-28.

**Two more changes to the same file, which only the owner can land, ride with it:**

- CI's job tests installs no Node. From the empty app on (issue E1-04), the synthesis and policy
  tests run aws-cdk-lib through jsii, on whatever Node the runner ships. The job synth already
  installs Node 24 with a setup-node action pinned by commit; the job tests gets the same step, with
  the same pin, so that both run on a Node the CDK supports (`AGENTS.md`: Node 22 or 24).
- The job tests has a 20-minute timeout, which a growing set of synthesis tests will approach. It
  becomes 40 minutes. The design gives no value; 40 is a choice the owner may revisit.

E0-19 reads the job's check run by name, so the job's id and its name are both container-tests.

This issue changes `.github/workflows/ci.yml`, a protected path on the safety surface (ADR 0001,
decision 10); the builder may change it because this issue names it.

## Deliverables

- [ ] `.github/workflows/ci.yml` gains a job whose id and name are both container-tests, which runs the gate script's step container-tests on the runner's container runtime, with no secret and no AWS credential.
- [ ] The job required of `.github/workflows/ci.yml` needs container-tests and lists it among the jobs that must have succeeded.
- [ ] The job tests of `.github/workflows/ci.yml` installs Node 24 with the setup-node action at the commit the job synth pins, before it runs the tests, and its timeout is 40 minutes.
- [ ] The branch changes `.github/workflows/ci.yml` and no other file.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_ci_workflow.py` passes on the changed `.github/workflows/ci.yml`.
- [ ] `grep -q 'scripts/check.sh container-tests' .github/workflows/ci.yml` exits 0.
- [ ] On a pull request, the job container-tests runs, and required is green only when it succeeded (needs host:container-runtime).

## References

- `docs/design/system-design.md`, sections 7.4, 8.3
- `docs/adr/0001-software-factory.md`, decision 10, amendments 2 and 5
- `docs/factory/green-light.md`
- `.github/workflows/ci.yml`
