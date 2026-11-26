"""The production-planning task of the course suite, `optimization.t01`, and its three instances.

A trial is asked to finish a broken HiGHS model of a workshop's production plan and to write the
plan as a LaTeX table (`task/TASK.md`). The exemplar is the task of spike S3 (`spikes/s3_task/`):
instance `i01` holds its data, `i02` changes the demand and `i03` the machine's hours, so the
exemplar's plan, copied, is no answer to either.

**What a trial is given** is `workspace_files`: the task's statement, the broken model and one
instance, as `instance.json`. `materialize` copies them into a folder.

**What stays outside** is everything else here: the grader (`grader.py`), and `SEALED`, the folder
of the reference fix (`reference/model.py`) and of what it produces on each instance
(`reference/<instance>/`), whose cost is the instance's reference optimum. `grade` reads what a
run wrote, against the trusted copy of the instance: never the copy the run could change. What
the reference produced is committed, so grading needs no solver; it is made again by running the
reference fix as `model.py` in a workspace, which the tests do, holding it to the committed cost.

**Versions.** The instances and the grader are files of this folder, and the suite
(`suites/course-v1.toml`) names each instance with the digest of what a trial is given
(`fixture_digest`) and of what grades it (`grader_digest`). A change to either is another digest,
so another suite and another workload: `tests/test_optimization.py` holds the files to the suite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from evaluate.course.optimization import grader
from evaluate.course.optimization.grader import PROPERTIES, Instance, Property, Report

__all__ = [
    "INSTANCE_IDS",
    "PROPERTIES",
    "SEALED",
    "TASK_ID",
    "Instance",
    "Property",
    "Report",
    "fixture_digest",
    "grade",
    "grader_digest",
    "load_instance",
    "materialize",
    "reference_artifacts",
    "reference_optimum",
    "workspace_files",
]

HERE = Path(__file__).resolve().parent

TASK_ID = "optimization.t01"
INSTANCE_IDS = tuple(f"{TASK_ID}.i{number:02d}" for number in (1, 2, 3))

# What no workspace is given anything of: the reference fix and what it produces.
SEALED = HERE / "reference"
REFERENCE_MODEL = SEALED / "model.py"


def _name(instance_id: str) -> str:
    if instance_id not in INSTANCE_IDS:
        raise ValueError(f"no instance {instance_id!r} of {TASK_ID}")
    return instance_id.rpartition(".")[2]


def workspace_files(instance_id: str) -> dict[str, Path]:
    """What a trial's workspace holds at the start: each file's name there -> its source."""
    return {
        "TASK.md": HERE / "task" / "TASK.md",
        "model.py": HERE / "task" / "model.py",
        "instance.json": HERE / "instances" / f"{_name(instance_id)}.json",
    }


def materialize(instance_id: str, workspace: Path) -> None:
    """Copy the files of an instance's workspace into a folder, made if it is not there."""
    workspace.mkdir(parents=True, exist_ok=True)
    for name, source in workspace_files(instance_id).items():
        shutil.copyfile(source, workspace / name)


def load_instance(instance_id: str) -> Instance:
    """The trusted data of an instance."""
    source = workspace_files(instance_id)["instance.json"]
    return Instance.parse(json.loads(source.read_text(encoding="utf-8")))


def reference_artifacts(instance_id: str) -> Path:
    """The folder of what the reference fix produces on an instance: its `plan.json` and `plan.tex`."""
    return SEALED / _name(instance_id)


def reference_optimum(instance_id: str) -> float:
    """The cost of the reference plan of an instance: no plan is cheaper."""
    plan = json.loads((reference_artifacts(instance_id) / grader.PLAN).read_text(encoding="utf-8"))
    return float(plan["cost"])


def grade(instance_id: str, artifacts: Path) -> Report:
    """Grade the `plan.json` and `plan.tex` of a folder as an answer to an instance."""
    return grader.grade(load_instance(instance_id), reference_optimum(instance_id), artifacts)


def _digest(files: dict[str, Path]) -> str:
    whole = hashlib.sha256()
    for name in sorted(files):
        whole.update(name.encode("utf-8") + b"\0" + hashlib.sha256(files[name].read_bytes()).digest())
    return "sha256:" + whole.hexdigest()


def fixture_digest(instance_id: str) -> str:
    """The digest of what a trial is given: the names and the content of its workspace's files."""
    return _digest(workspace_files(instance_id))


def grader_digest(instance_id: str) -> str:
    """The digest of what grades an instance: the grader, the reference fix and what it produced."""
    produced = reference_artifacts(instance_id)
    return _digest(
        {
            "grader.py": Path(grader.__file__),
            "reference/model.py": REFERENCE_MODEL,
            "reference/plan.json": produced / grader.PLAN,
            "reference/plan.tex": produced / grader.TABLE,
        }
    )
