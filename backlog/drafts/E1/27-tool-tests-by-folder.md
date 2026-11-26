+++
id = "E1-27"
title = "A bare pytest finds every tool's tests by folder, and the stale examiner probes are gone"
labels = ["size:M", "kind:test"]
epic = 3
+++

## Context

Two simplifications of the 2026-11-26 retrospective that the owner took on 2026-11-27. Both edit
the tools' configuration files (`pyproject.toml`, `ty.toml`, and `ruff.toml` for the first), so
they are one change.

**The probes in `tests/examiner/`.** The folder holds 8 tracked files, 440 lines, probes of issues
#32 and #51 from 2026-11-15. Builds that ran a probe from there deleted the tracked files while
removing it, had `git checkout` refused and restored the files by hand: 4 builds on 2026-11-25/26,
7 the day before. In #793's build, `test_issue51_student_key_probe.py::test_tree_hygiene_gate`
failed in the full run and passed on its own. What they check is held elsewhere:

- the student key (#51): `tools/hygiene/tests/test_student_key.py` checks that the scanner holds
  the key module's format, reports a key anywhere in a line and in a file name, masks it on
  redaction and stays quiet on look-alikes; the tree's hygiene is the gate's own step,
  `scripts/check.sh hygiene`, which `fast` runs;
- cdk-nag's acknowledgements (#32): `tests/synth/test_nag.py` checks that a finding fails the app's
  synthesis unless the file of its module acknowledges it, and that a resource carries what that
  file lists and nothing else; `tests/policy/test_nag_acknowledgements.py` checks that every
  acknowledgement of the app is listed, that an unlisted one is detected, and that metadata that is
  not a list of findings is refused.

The folder is named in `pythonpath` (`pyproject.toml`, with a comment), in `src` (`ruff.toml`) and
in `root` (`ty.toml`), and nowhere else (`git grep tests/examiner`). `tests/unit/test_layout.py`
holds those three lists together and refuses a listed folder that does not exist, so the folder
leaves all three with its files.

**The tools' tests.** `testpaths` names five `tools/<tool>/tests` folders, and ty's `include` names
the six tools one by one, so a new tool's tests run only once its issue adds them: #745
(`tools/evaluate`) failed 5 rounds on that item alone. With the folder `tools` in `testpaths` and the
engine kept out by `norecursedirs`, the way `tests/container` is, a new tool's tests are found with
no edit. The engine's tests keep running where they run today: `scripts/check.sh engine-tests` runs
`tools/implementation-loop/tests` by path, on the engine's own Python and pytest, and pytest applies
`norecursedirs` only to folders it recurses into, not to a path it is given, which `scripts/check.sh
container-tests` already relies on for `tests/container`. Checked on 2026-11-27: with `testpaths =
["tools", "tests"]` and `tools/implementation-loop` in `norecursedirs`, a bare collection lists the
same 259 test files as today, and the engine's 771 tests are still collected by path, under the
project's pytest and under the engine's pytest 8.3.5. For ty, `tools` holds exactly the six folders
`include` lists, the engine among them, so ty checks the same files.

`tools/evaluate/tests/test_checks.py` asserts that `testpaths` names `tools/evaluate/tests`
itself; it must accept an entry that covers the tool's folder, as its other assertions already do
with `covers_tool`.

A verifier command must run at least one test, and must not pipe pytest into `grep -q`, which
closes the pipe and makes pytest exit 3 (#745).

Suggested branch: `chore/tool-tests-by-folder`

## Deliverables

- [ ] The tracked files of `tests/examiner/` are deleted, and the folder leaves `pythonpath` and its comment in `pyproject.toml`, `src` in `ruff.toml` and `root` in `ty.toml`.
- [ ] `pyproject.toml` gives pytest the folders tools and tests as its testpaths, in place of the five per-tool test folders, and keeps the engine's folder, tools/implementation-loop, out through norecursedirs beside tests/container.
- [ ] `ty.toml` includes the folder tools in place of its six per-tool entries, so ty checks the same files as before, the engine's among them.
- [ ] `tools/evaluate/tests/test_checks.py` accepts a testpaths entry that covers the tool's folder, such as tools, in place of requiring tools/evaluate/tests itself.
- [ ] `tests/unit/test_layout.py` runs a pytest collection with no path at the repository root and checks that it lists every test file under each tool's tests folder but the engine's, and none under tools/implementation-loop or tests/container.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_layout.py tools/evaluate/tests/test_checks.py` passes.
- [ ] `test ! -e tests/examiner` exits 0.
- [ ] `bash -c "! git grep -n tests/examiner"` exits 0.
- [ ] `scripts/check.sh engine-tests` exits 0.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] `scripts/check.sh tests` exits 0.

## References

- `pyproject.toml`, `[tool.pytest.ini_options]`
- `ty.toml`, `[environment] root` and `[src] include`
- `ruff.toml`, `src`
- `scripts/check.sh`, the steps tests, engine-tests and container-tests
- `tests/unit/test_layout.py`
- `tests/unit/test_container_tests_collection.py`, a test that runs a collection the same way
