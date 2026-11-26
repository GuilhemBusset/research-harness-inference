"""The debug-and-revise task of the course suite, `debugging.t01`, and its three instances.

A trial is given a solver that chooses the parcels a delivery van takes (a 0/1 knapsack) and gets
a wrong answer on the case it is given. In one conversation it is sent two prompts, in order
(`prompts`): the first asks it to run the solver, find out why its result is wrong and fix it
(`task/01-fix.md`); the second, once that is answered, adds a constraint, at most `max_items`
parcels on a case that carries it (`task/02-follow-up.md`). Each instance has its own solver, with
its own fault: `i01` stops its load loop a kilogram short, so a parcel that fills the room left is
never taken; `i02` takes the parcels that pay best for their weight first, which is no exact
method; `i03` finds the best value but reads back the wrong parcels for it.

**What a trial is given** is `workspace_files`: the broken solver, as `solver.py`, and the
instance's exemplar case, as `case.json`. `materialize` copies them into a folder. The prompts are
sent, not given as files: the follow-up is not in the workspace before it is asked.

**What stays outside** is everything else here, in `SEALED`: the grader (`grader.py`), the
reference repair of each instance (`reference/<instance>/solver.py`), and its four cases
(`cases/<instance>/`), each with its optimum: `original` (the exemplar), `changed` (other weights,
fees and capacity, where the exemplar's answer is no answer), and both again with the follow-up's
`max_items`. The final program is run on each case in a fresh worker: `write_inputs` writes the
cases as the program reads them, with no optimum, and `command` is how it is run on one. `grade`
then reads what it wrote, against the trusted cases.

**Versions.** The instances, the prompts and the grader are files of this folder, and the suite
(`suites/course-v1.toml`) names each instance with the digest of what a trial is given and asked
(`fixture_digest`) and of what grades it (`grader_digest`). A change to either is another digest,
so another suite and another workload: `tests/test_revision.py` holds the files to the suite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from evaluate.course.revision import grader
from evaluate.course.revision.grader import CASES, PROPERTIES, Case, Property, Report, Sealed

__all__ = [
    "CASES",
    "INSTANCE_IDS",
    "PROPERTIES",
    "SEALED",
    "SOLVER",
    "TASK_ID",
    "Case",
    "Property",
    "Report",
    "Sealed",
    "command",
    "fixture_digest",
    "grade",
    "grader_digest",
    "load_cases",
    "materialize",
    "prompts",
    "reference_solver",
    "workspace_files",
    "write_inputs",
]

HERE = Path(__file__).resolve().parent

TASK_ID = "debugging.t01"
INSTANCE_IDS = tuple(f"{TASK_ID}.i{number:02d}" for number in (1, 2, 3))

# The program a trial fixes and revises, as its workspace names it.
SOLVER = "solver.py"
# The prompts of the conversation, in the order they are sent.
PROMPTS = (HERE / "task" / "01-fix.md", HERE / "task" / "02-follow-up.md")

# What no workspace is given anything of: the reference repairs, and the cases with their optima.
REFERENCE = HERE / "reference"
SEALED_CASES = HERE / "cases"
SEALED = (REFERENCE, SEALED_CASES)


def _name(instance_id: str) -> str:
    if instance_id not in INSTANCE_IDS:
        raise ValueError(f"no instance {instance_id!r} of {TASK_ID}")
    return instance_id.rpartition(".")[2]


def workspace_files(instance_id: str) -> dict[str, Path]:
    """What a trial's workspace holds at the start: each file's name there -> its source."""
    folder = HERE / "instances" / _name(instance_id)
    return {SOLVER: folder / SOLVER, "case.json": folder / "case.json"}


def materialize(instance_id: str, workspace: Path) -> None:
    """Copy the files of an instance's workspace into a folder, made if it is not there."""
    workspace.mkdir(parents=True, exist_ok=True)
    for name, source in workspace_files(instance_id).items():
        shutil.copyfile(source, workspace / name)


def prompts() -> tuple[str, ...]:
    """The prompts of the conversation, in order: the fix, then the follow-up."""
    return tuple(prompt.read_text(encoding="utf-8") for prompt in PROMPTS)


def _case_files(instance_id: str) -> dict[str, Path]:
    folder = SEALED_CASES / _name(instance_id)
    return {name: folder / f"{name}.json" for name in CASES}


def load_cases(instance_id: str) -> dict[str, Sealed]:
    """The trusted cases of an instance, each with its optimum."""
    return {
        name: Sealed.parse(json.loads(path.read_text(encoding="utf-8")))
        for name, path in _case_files(instance_id).items()
    }


def write_inputs(instance_id: str, folder: Path) -> dict[str, Path]:
    """Write each case of an instance as the program reads it, with no optimum: case name -> file."""
    folder.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, sealed in load_cases(instance_id).items():
        written[name] = folder / f"{name}.json"
        written[name].write_text(json.dumps(sealed.case.to_json(), indent=2) + "\n", encoding="utf-8")
    return written


def command(case: str, output: str) -> list[str]:
    """How the final program is run, in its workspace, on one case file: `grade` reads the file it
    writes, `output`, as `<case name>.json` of the output folder."""
    return ["python", SOLVER, case, output]


def reference_solver(instance_id: str) -> Path:
    """The reference repair of an instance: its solver fixed and revised."""
    return REFERENCE / _name(instance_id) / SOLVER


def grade(instance_id: str, outputs: Path) -> Report:
    """Grade the outputs of a folder, one `<case>.json` for each case, as an answer to an instance."""
    return grader.grade(load_cases(instance_id), outputs)


def _digest(files: dict[str, Path]) -> str:
    whole = hashlib.sha256()
    for name in sorted(files):
        whole.update(name.encode("utf-8") + b"\0" + hashlib.sha256(files[name].read_bytes()).digest())
    return "sha256:" + whole.hexdigest()


def fixture_digest(instance_id: str) -> str:
    """The digest of what a trial is given and asked: its workspace's files and the prompts."""
    return _digest(workspace_files(instance_id) | {f"prompts/{prompt.name}": prompt for prompt in PROMPTS})


def grader_digest(instance_id: str) -> str:
    """The digest of what grades an instance: the grader, the reference repair and the cases."""
    cases = {f"cases/{path.name}": path for path in _case_files(instance_id).values()}
    return _digest({"grader.py": Path(grader.__file__), "reference/solver.py": reference_solver(instance_id)} | cases)
