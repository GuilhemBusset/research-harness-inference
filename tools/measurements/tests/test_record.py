"""The statuses of a commit become measurement records (docs/measurements/README.md).

The statuses are built in the shape GitHub's API lists them, from what the reporter's encoder
gives, and every record written is held to the format by the format's own test file. No number
here is a measurement, and nothing is written into the repository's own folder of records.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import record
from rhi_reporter.measure import Verdict, encode

REPO = Path(__file__).resolve().parents[3]
FIRST, LATER = "a" * 40, "b" * 40
UNITS = {"output_speed_median": "tokens/s", "preemptions": "count", "start_to_serving_p90": "s"}
CONDITIONS = {"S5": "The median stream at 15 tokens per second or more", "S1": "Warm start under 5 min"}


def format_of_the_records() -> Any:
    """The format's own test file, as a module: its functions are the format as code."""
    spec = importlib.util.spec_from_file_location("measurement_format", REPO / "tests/unit/test_measurements.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FORMAT = format_of_the_records()


def listed(
    commit: str, at: str, measurement: str, label: str, verdict: Verdict, **numbers: float
) -> list[dict[str, Any]]:
    """One run, as GitHub lists the statuses the reporter posted for it."""
    return [
        {
            "url": f"https://api.github.com/repos/octo-org/hello-world/statuses/{commit}",
            "id": 1000 + part,
            "state": str(status.state),
            "description": status.description or None,
            "target_url": None,
            "context": status.context,
            "created_at": at,
            "updated_at": at,
            "creator": {"login": "octo-reporter[bot]", "type": "Bot"},
        }
        for part, status in enumerate(encode(measurement, label, verdict, numbers, names=numbers, commit=commit))
    ]


def front_matter(path: Path) -> dict[str, Any]:
    return FORMAT.front_matter(path.read_text(encoding="utf-8"))


def write(folder: Path, statuses: list[dict[str, Any]], commit: str) -> list[Path]:
    return record.record_statuses(statuses, commit, folder, units=UNITS, pass_conditions=CONDITIONS)


# Two runs of S5 and a run of S1 that could not measure, on one commit, beside a deployment's
# status and an older status of a context that a newer one replaced.
ON_FIRST = [
    *listed(FIRST, "2026-01-15T10:00:00Z", "S5", "8bit", Verdict.PASS, output_speed_median=19.5, preemptions=0),
    *listed(FIRST, "2026-01-15T23:30:00-02:00", "S5", "4bit", Verdict.FAIL, output_speed_median=9.0, preemptions=2),
    *listed(FIRST, "2026-01-15T11:00:00Z", "S1", "cold", Verdict.NOT_MEASURED),
    {
        "state": "success",
        "description": "The deployment succeeded.",
        "context": "deploy/Prod",
        "created_at": "2026-01-15T09:00:00Z",
    },
    *listed(FIRST, "2026-01-14T08:00:00Z", "S5", "8bit", Verdict.FAIL, output_speed_median=1.0),
]


def test_the_statuses_of_a_commit_become_records_of_the_format(tmp_path: Path) -> None:
    assert sorted(path.name for path in write(tmp_path, ON_FIRST, FIRST)) == ["s1.md", "s5.md"]
    assert FORMAT.folder_problems(tmp_path) == []

    s5 = front_matter(tmp_path / "s5.md")
    assert (s5["id"], s5["pass_condition"], s5["verdict"]) == ("S5", CONDITIONS["S5"], "fail")
    # Each run has the commit and the day, in UTC, of its newest statuses.
    assert s5["runs"] == [
        {"run": "8bit", "date": date(2026, 1, 15), "commit": FIRST, "verdict": "pass"},
        {"run": "4bit", "date": date(2026, 1, 16), "commit": FIRST, "verdict": "fail"},
    ]
    assert s5["rows"] == [
        {"name": "output_speed_median", "value": 19.5, "unit": "tokens/s", "labels": {"run": "8bit"}},
        {"name": "preemptions", "value": 0, "unit": "count", "labels": {"run": "8bit"}},
        {"name": "output_speed_median", "value": 9.0, "unit": "tokens/s", "labels": {"run": "4bit"}},
        {"name": "preemptions", "value": 2, "unit": "count", "labels": {"run": "4bit"}},
    ]


def test_a_run_posted_as_error_is_pending_and_not_measured(tmp_path: Path) -> None:
    write(tmp_path, ON_FIRST, FIRST)

    s1 = front_matter(tmp_path / "s1.md")
    assert s1["runs"] == [
        {"run": "cold", "date": date(2026, 1, 15), "commit": FIRST, "verdict": "pending", "reason": "not measured"}
    ]
    assert "rows" not in s1
    assert s1["verdict"] == "pending"

    # The run that then measures replaces it, and an error after that takes the rows away again.
    write(
        tmp_path, listed(LATER, "2026-02-01T10:00:00Z", "S1", "cold", Verdict.PASS, start_to_serving_p90=262.5), LATER
    )
    s1 = front_matter(tmp_path / "s1.md")
    assert ([run["verdict"] for run in s1["runs"]], len(s1["rows"]), s1["verdict"]) == (["pass"], 1, "pass")
    write(tmp_path, listed(FIRST, "2026-02-02T10:00:00Z", "S1", "cold", Verdict.NOT_MEASURED), FIRST)
    assert "rows" not in front_matter(tmp_path / "s1.md")
    assert FORMAT.folder_problems(tmp_path) == []


def test_a_later_run_replaces_the_run_of_its_label_and_keeps_the_others(tmp_path: Path) -> None:
    write(tmp_path, ON_FIRST, FIRST)
    path = tmp_path / "s5.md"
    # What a person added to the record since: a label on a row, and prose.
    by_hand = path.read_text(encoding="utf-8").replace('{ run = "4bit" }', '{ run = "4bit", build = "4bit" }')
    path.write_text(by_hand + "\nThe 4-bit build failed: it was preempted twice.\n+++\n", encoding="utf-8")

    # The later commit's run comes in two parts, and needs no unit given again.
    later = listed(
        LATER, "2026-02-01T10:00:00Z", "S5", "8bit", Verdict.PASS, output_speed_median=21.0, **{"n" * 130: 1}
    )
    assert len(later) == 2
    record.record_statuses(later, LATER, tmp_path, units={"n" * 130: "count"})

    assert FORMAT.folder_problems(tmp_path) == []
    s5 = front_matter(path)
    assert s5["runs"] == [
        {"run": "8bit", "date": date(2026, 2, 1), "commit": LATER, "verdict": "pass"},
        {"run": "4bit", "date": date(2026, 1, 16), "commit": FIRST, "verdict": "fail"},
    ]
    assert [(row["name"], row["value"], row["labels"]) for row in s5["rows"]] == [
        ("output_speed_median", 21.0, {"run": "8bit"}),
        ("n" * 130, 1, {"run": "8bit"}),
        ("output_speed_median", 9.0, {"run": "4bit", "build": "4bit"}),
        ("preemptions", 2, {"run": "4bit", "build": "4bit"}),
    ]
    assert (s5["pass_condition"], s5["verdict"]) == (CONDITIONS["S5"], "fail")
    assert path.read_text(encoding="utf-8").endswith("+++\n\nThe 4-bit build failed: it was preempted twice.\n+++\n")

    # Only the owner writes a waiver, and it stands when a run is recorded.
    waived = path.read_text(encoding="utf-8").replace('verdict = "fail"', 'verdict = "waived"\nwaiver = "docs/x.md"', 1)
    path.write_text(waived, encoding="utf-8")
    write(tmp_path, listed(LATER, "2026-02-03T10:00:00Z", "S5", "4bit", Verdict.PASS, preemptions=0), LATER)
    s5 = front_matter(path)
    assert (s5["verdict"], s5["waiver"], [run["verdict"] for run in s5["runs"]]) == (
        "waived",
        "docs/x.md",
        ["pass", "pass"],
    )
    assert FORMAT.folder_problems(tmp_path) == []


def test_refuses_before_any_file_is_written(tmp_path: Path) -> None:
    (tmp_path / "s1.md").write_text("Prose with no front matter.\n", encoding="utf-8")
    s1 = listed(FIRST, "2026-01-15T10:00:00Z", "S1", "warm", Verdict.PASS, start_to_serving_p90=262.5)
    s5 = listed(FIRST, "2026-01-15T10:00:00Z", "S5", "8bit", Verdict.PASS, preemptions=0)
    stale = listed(FIRST, "2026-01-14T10:00:00Z", "S5", "8bit", Verdict.PASS, **{"n" * 130: 1, "preemptions": 3})
    given: dict[str, Any] = dict(commit=FIRST, folder=tmp_path, units=UNITS, pass_conditions=CONDITIONS)
    refused: dict[str, tuple[dict[str, Any], str]] = {
        "an existing file that is not a record": (dict(statuses=[*s5, *s1]), "not a measurement record"),
        "a number with no unit": (dict(statuses=s5, units={}), "no unit"),
        "a new record with no pass condition": (dict(statuses=s5, pass_conditions={}), "no pass condition"),
        "a short commit": (dict(statuses=s5, commit=FIRST[:7]), "commit"),
        "a part left behind by an earlier, longer posting of the run": (dict(statuses=[*s5, *stale]), "twice"),
        "a status with no date": (dict(statuses=[{**s5[0], "created_at": None}]), "its date"),
        "a context under measure/ of another form": (
            dict(statuses=[{**s5[0], "context": "measure/S5/8bit"}]),
            "context",
        ),
        "something that is not a list of statuses": (dict(statuses=["measure/S5/8bit/1"]), "GitHub's list"),
    }
    for fault, (change, message) in refused.items():
        with pytest.raises(ValueError, match=message):
            print(fault, record.record_statuses(**{**given, **change}))
    assert [path.name for path in tmp_path.iterdir()] == ["s1.md"]


def test_the_command_needs_the_standard_library_only(tmp_path: Path) -> None:
    statuses = tmp_path / "statuses.json"
    statuses.write_text(json.dumps(ON_FIRST), encoding="utf-8")
    folder = tmp_path / "records"
    options = [f"--unit={name}={unit}" for name, unit in UNITS.items()]
    options += [f"--pass-condition={measurement}={text}" for measurement, text in CONDITIONS.items()]

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        # -I and -S: no site-packages and no path from the environment, so only the standard library is there.
        command = [sys.executable, "-I", "-S", str(REPO / "tools/measurements/record.py"), *arguments]
        return subprocess.run(command, capture_output=True, text=True, check=False, cwd=tmp_path)

    done = run("--commit", FIRST, "--statuses", str(statuses), "--folder", str(folder), *options)
    assert (done.returncode, done.stderr) == (0, "")
    assert sorted(Path(line).name for line in done.stdout.splitlines()) == ["s1.md", "s5.md"]
    assert FORMAT.folder_problems(folder) == []
    assert len(front_matter(folder / "s5.md")["runs"]) == 2

    # A refusal is one fixed line, and writes nothing.
    refused = run("--commit", FIRST, "--statuses", str(statuses), "--folder", str(tmp_path / "none"))
    assert (refused.returncode, refused.stdout) == (1, "")
    assert refused.stderr == "record: a new record has no pass condition: give the one of its measurement\n"
    assert not (tmp_path / "none").exists()
