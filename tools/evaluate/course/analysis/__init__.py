"""The parallel-analysis task of the course suite, `analysis.t01`, and its three instances.

A trial is asked to delegate the two running totals of a paired data set to two workers at once,
to draw both in one SVG line chart and to report a named comparison of them (`task/TASK.md`). The
exemplar is the figure exercise of spike S3 (`spikes/s3_second_session/TASK.md`, part 1): on
instance `i01` the two totals are its x * x and 10 * x, for x from 0 to 10; `i02` changes the
data, the names and the labels, and `i03` the data and the comparison as well, so the exemplar's
figure and result, copied, are no answer to either.

**What a trial is given** is `workspace_files`: the task's statement and one instance, as
`data.csv` and `instance.json`. `materialize` copies them into a folder.

**What stays outside** is everything else here: the grader (`grader.py`), and `SEALED`, the folder
of the reference solution (`reference/analysis.py`) and of what it produces on each instance
(`reference/<instance>/`). `grade` reads what a run wrote, against the trusted copy of the
instance, with the lifecycle of the run's workers that the trusted side observed (the trial's
`Delegation`, or the `Span` of each worker): never anything the run could change. What the reference produced is committed; it is made again by
running the reference in a workspace, which the tests do.

**Versions.** The instances and the grader are files of this folder, and the suite
(`suites/course-v1.toml`) names each instance with the digest of what a trial is given
(`fixture_digest`) and of what grades it (`grader_digest`): `tests/test_analysis.py` holds the
files to the suite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from evaluate.course.analysis import grader
from evaluate.course.analysis.grader import PROPERTIES, Evidence, Instance, Property, Report, Span

__all__ = [
    "INSTANCE_IDS",
    "PROPERTIES",
    "SEALED",
    "TASK_ID",
    "Evidence",
    "Instance",
    "Property",
    "Report",
    "Span",
    "fixture_digest",
    "grade",
    "grader_digest",
    "load_instance",
    "materialize",
    "reference_artifacts",
    "workspace_files",
]

HERE = Path(__file__).resolve().parent

TASK_ID = "analysis.t01"
INSTANCE_IDS = tuple(f"{TASK_ID}.i{number:02d}" for number in (1, 2, 3))

# What no workspace is given anything of: the reference solution and what it produces.
SEALED = HERE / "reference"
REFERENCE = SEALED / "analysis.py"
PRODUCED = (grader.SERIES_A, grader.SERIES_B, grader.FIGURE, grader.RESULT)


def _name(instance_id: str) -> str:
    if instance_id not in INSTANCE_IDS:
        raise ValueError(f"no instance {instance_id!r} of {TASK_ID}")
    return instance_id.rpartition(".")[2]


def workspace_files(instance_id: str) -> dict[str, Path]:
    """What a trial's workspace holds at the start: each file's name there -> its source."""
    instance = HERE / "instances" / _name(instance_id)
    return {
        "TASK.md": HERE / "task" / "TASK.md",
        "data.csv": instance / "data.csv",
        "instance.json": instance / "instance.json",
    }


def materialize(instance_id: str, workspace: Path) -> None:
    """Copy the files of an instance's workspace into a folder, made if it is not there."""
    workspace.mkdir(parents=True, exist_ok=True)
    for name, source in workspace_files(instance_id).items():
        shutil.copyfile(source, workspace / name)


def load_instance(instance_id: str) -> Instance:
    """The trusted data of an instance."""
    files = workspace_files(instance_id)
    brief = json.loads(files["instance.json"].read_text(encoding="utf-8"))
    return Instance.parse(brief, files["data.csv"].read_text(encoding="utf-8"))


def reference_artifacts(instance_id: str) -> Path:
    """The folder of what the reference solution produces on an instance."""
    return SEALED / _name(instance_id)


def grade(instance_id: str, artifacts: Path, workers: Evidence) -> Report:
    """Grade what a folder holds, with the lifecycle of the run's workers, as an answer to an instance."""
    return grader.grade(load_instance(instance_id), artifacts, workers)


def _digest(files: dict[str, Path]) -> str:
    whole = hashlib.sha256()
    for name in sorted(files):
        whole.update(name.encode("utf-8") + b"\0" + hashlib.sha256(files[name].read_bytes()).digest())
    return "sha256:" + whole.hexdigest()


def fixture_digest(instance_id: str) -> str:
    """The digest of what a trial is given: the names and the content of its workspace's files."""
    return _digest(workspace_files(instance_id))


def grader_digest(instance_id: str) -> str:
    """The digest of what grades an instance: the grader, the reference solution and what it produced."""
    produced = reference_artifacts(instance_id)
    return _digest(
        {
            "grader.py": Path(grader.__file__),
            "reference/analysis.py": REFERENCE,
            **{f"reference/{name}": produced / name for name in PRODUCED},
        }
    )
