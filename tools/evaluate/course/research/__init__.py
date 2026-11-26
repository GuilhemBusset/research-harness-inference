"""The grounded-research task of the course suite, `research.t01`, and its three instances.

A trial answers a question about an insulation panel from a frozen packet of four sources, one of
them a PDF, with a local search tool over it, and writes an evidence table and a short conclusion
(`task/TASK.md`). Every source is self-authored and invented for the course (`build_packet.py`
writes them). The instances ask for the mass of panel a wall needs: `i01` of the cork panel with
its dry conductivity, `i02` of the same panel conditioned in humid air (another table of the same
page of the report), and `i03` of the wood fibre panel and another thermal resistance. An answer
copied from one instance is no answer to another: its facts are other facts.

**What a trial is given** is `workspace_files`: the task's statement, the instance's question as
`instance.json`, and the packet under `packet/`; and `TOOLS`, the folders of its local tools, by
name: `search`, whose launcher the trial's `PATH` finds (`adapters`, *local research tools*).
`materialize` copies the files into a folder.

**What stays outside** is `SEALED`: the key of each instance (`key/<instance>.json`: each fact's
value, unit, source and place, and the result), and the reference answer
(`reference/<instance>/answer.json`). `grade` reads what a run wrote against the key, which the
grader holds to the trusted copy of the packet's index (`grader.Key.parse`), and never against the
reference answer: it is one answer the grader must accept, not the one it compares with.

**Versions.** The packet, the tool, the instances and the grader are files of this folder, and the
suite (`suites/course-v1.toml`) names each instance with the digest of what a trial is given
(`fixture_digest`) and of what grades it (`grader_digest`). A change to either is another digest,
so another suite and another workload: `tests/test_research.py` holds the files to the suite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from evaluate.course.research import grader
from evaluate.course.research.grader import PROPERTIES, Key, Packet, Property, Report

__all__ = [
    "INSTANCE_IDS",
    "PROPERTIES",
    "SEALED",
    "TASK_ID",
    "TOOLS",
    "Key",
    "Packet",
    "Property",
    "Report",
    "fixture_digest",
    "grade",
    "grader_digest",
    "load_key",
    "load_packet",
    "materialize",
    "reference_artifacts",
    "workspace_files",
]

HERE = Path(__file__).resolve().parent

TASK_ID = "research.t01"
INSTANCE_IDS = tuple(f"{TASK_ID}.i{number:02d}" for number in (1, 2, 3))

PACKET = HERE / "packet"
# The local tools of a trial, by the name the worker mounts each under (`workers.Spec.tools`).
TOOLS = {"search": HERE / "tools" / "search"}
# The files of the search tool: its launcher, its program and the packet's index, which is what
# it ranks and the grader's map of the packet.
SEARCH_FILES = ("search", "search.py", "index.json")
INDEX = TOOLS["search"] / "index.json"

# What no workspace is given anything of: the keys and the reference answers.
KEYS = HERE / "key"
REFERENCE = HERE / "reference"
SEALED = (KEYS, REFERENCE)


def _packet_files() -> list[str]:
    """The files of the packet's sources, as its index names them."""
    return sorted(source["file"] for source in json.loads(INDEX.read_text(encoding="utf-8"))["sources"].values())


def _name(instance_id: str) -> str:
    if instance_id not in INSTANCE_IDS:
        raise ValueError(f"no instance {instance_id!r} of {TASK_ID}")
    return instance_id.rpartition(".")[2]


def workspace_files(instance_id: str) -> dict[str, Path]:
    """What a trial's workspace holds at the start: each file's name there -> its source."""
    return {
        "TASK.md": HERE / "task" / "TASK.md",
        "instance.json": HERE / "instances" / f"{_name(instance_id)}.json",
        **{f"packet/{file}": PACKET / file for file in _packet_files()},
    }


def materialize(instance_id: str, workspace: Path) -> None:
    """Copy the files of an instance's workspace into a folder, made if it is not there."""
    for name, source in workspace_files(instance_id).items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, workspace / name)


def load_packet() -> Packet:
    """The trusted map of the packet: its sources and their passages."""
    return Packet.parse(json.loads(INDEX.read_text(encoding="utf-8")))


def load_key(instance_id: str) -> Key:
    """The trusted answer of an instance, held to the packet."""
    raw = json.loads((KEYS / f"{_name(instance_id)}.json").read_text(encoding="utf-8"))
    return Key.parse(raw, load_packet())


def reference_artifacts(instance_id: str) -> Path:
    """The folder of an instance's reference answer: its `answer.json`."""
    return REFERENCE / _name(instance_id)


def grade(instance_id: str, artifacts: Path) -> Report:
    """Grade the `answer.json` of a folder as an answer to an instance."""
    return grader.grade(load_key(instance_id), load_packet(), artifacts)


def _tool_files() -> dict[str, Path]:
    return {f"tools/search/{file}": TOOLS["search"] / file for file in SEARCH_FILES}


def _digest(files: dict[str, Path]) -> str:
    whole = hashlib.sha256()
    for name in sorted(files):
        whole.update(name.encode("utf-8") + b"\0" + hashlib.sha256(files[name].read_bytes()).digest())
    return "sha256:" + whole.hexdigest()


def fixture_digest(instance_id: str) -> str:
    """The digest of what a trial is given: its workspace's files and its tools, names and content."""
    return _digest({**workspace_files(instance_id), **_tool_files()})


def grader_digest(instance_id: str) -> str:
    """The digest of what grades an instance: the grader, the packet's index, the key and the reference."""
    return _digest(
        {
            "grader.py": Path(grader.__file__),
            "index.json": INDEX,
            "key.json": KEYS / f"{_name(instance_id)}.json",
            "reference/answer.json": reference_artifacts(instance_id) / grader.ANSWER,
        }
    )
