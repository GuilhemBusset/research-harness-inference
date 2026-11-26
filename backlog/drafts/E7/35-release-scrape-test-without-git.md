+++
id = "E7-35"
title = "The release-scrape test of the evaluation controller runs without a git commit"
labels = ["size:S", "kind:test"]
epic = 744
+++

## Context

The pipeline's synth step runs `uv run --locked pytest` in a source that has no commit: the source CodeBuild receives has no `.git`, and the step runs `git init --quiet` before the tests (`src/rhi/pipeline/synth.py`), so `git rev-parse HEAD` exits 128 there. One test fails in that step and nowhere else: `test_a_host_serves_the_release_of_its_latest_scrape` in `tools/evaluate/tests/test_controller.py`. It calls `live.resolve`, which builds the run record in `_run_record` (`tools/evaluate/live.py`), which calls `source_commit()` (`tools/evaluate/controller.py`), which runs `git rev-parse HEAD` with `check=True`. The test stubs every other lookup of the pool's instruments with `monkeypatch` and not that one, so it raises `subprocess.CalledProcessError`. CI passes because its checkout has a commit. The failure blocks every deployment.

`GIT_DIR=/nonexistent` reproduces the failure on any checkout: with it the test fails today and every other test under `tools/evaluate/tests/` passes.

The fix is in the test. It stubs the source commit as it stubs the other lookups: `_run_record` imports `source_commit` from `evaluate.controller` when it is called, so replacing that module attribute with `monkeypatch` reaches it. A live run is unchanged: `source_commit()` and `_run_record` still record the checkout's commit and still fail when there is none, and this issue changes neither `tools/evaluate/controller.py` nor `tools/evaluate/live.py`.

Suggested branch: `fix/release-scrape-test-without-git`

## Deliverables

- [ ] `tools/evaluate/tests/test_controller.py` replaces the source commit in test_a_host_serves_the_release_of_its_latest_scrape with a fixed 40-character commit, so that the test runs no git command.
- [ ] The test is not skipped, deleted or marked xfail, and keeps its three assertions on the releases a host serves.

## Acceptance criteria

- [ ] `GIT_DIR=/nonexistent uv run --locked pytest tools/evaluate/tests/test_controller.py::test_a_host_serves_the_release_of_its_latest_scrape` exits 0.
- [ ] `GIT_DIR=/nonexistent uv run --locked pytest tools/evaluate/tests` exits 0.

## References

- `src/rhi/pipeline/synth.py`
- `tools/evaluate/live.py`
- `docs/design/agent-session-evaluation.md`
