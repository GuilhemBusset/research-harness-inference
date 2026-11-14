+++
id = "E3-17"
title = "Spike S3's task: an optimisation model to complete, with a pinned open-source solver and a check kept out of the tests"
labels = ["size:S", "kind:test"]
epic = "E3"
blocked_by = ["E3-14"]
+++

## Context

Spike S3 asks whether each harness completes a multi-turn tool-calling task of the course's kind on
the coursework model (section 12; issue E3-41). The coursework is optimisation modelling in Python,
running solvers and writing LaTeX: long tool loops, not chat (section 4.2). This issue is the task
the harnesses are given, and nothing of the spike's run.

**A folder, not a repository.** The task is a folder of this repository, `spikes/s3_task/`; a nested
git repository cannot be committed. The spike copies it to a scratch folder before a harness works
on it. It holds an optimisation model in Python left incomplete, the instructions a harness reads,
and a check, `check_task.py`, a plain program that exits 0 when the model solves to the expected
optimum and the LaTeX table it writes holds that optimum, and non-zero otherwise. The reference fix
is kept apart from what a harness sees, in `spikes/s3_reference/`, as the files that differ.

**Kept out of the gate.** The check fails on the task as given, on purpose. So it is not a test file
and pytest never collects it: a test of this repository copies the task to a temporary folder and
runs the check in a subprocess, once as given and once with the reference laid over it. The task is
input to a harness, not this project's code, so ruff and ty leave both folders out.

**The solver.** The design names none. The task uses HiGHS, an open-source linear and mixed-integer
solver under the MIT licence, through its Python package highspy, which installs as a wheel with no
system library; the issue pins the newest release at least 7 days old when it is built, the
cooling-off section 17 asks before an update is adopted.

## Deliverables

- [ ] `spikes/s3_task/` is a folder holding an optimisation model in Python to complete, solved with HiGHS through highspy and reported as a LaTeX table, the instructions a harness reads, and `spikes/s3_task/check_task.py`, which exits non-zero on the folder as given.
- [ ] `spikes/s3_reference/` holds the reference fix, the files that differ from the task, with which the check exits 0.
- [ ] `pyproject.toml` pins highspy exactly in the dependency group the tests install, `uv.lock` records it, and `ruff.toml` and `ty.toml` leave `spikes/s3_task/` and `spikes/s3_reference/` out of their scope.
- [ ] `tests/unit/spikes/test_s3_task.py` copies the task to a temporary folder and runs its check in a subprocess, as given and with the reference fix laid over it.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s3_task.py` passes: the check exits non-zero on the task as given and 0 with the reference fix, and the task's folder is unchanged afterwards.
- [ ] `scripts/check.sh fast` exits 0 with both folders in the tree.
- [ ] `uv sync --locked` exits 0 with highspy pinned.

## References

- `docs/design/system-design.md`, sections 4.2, 12
