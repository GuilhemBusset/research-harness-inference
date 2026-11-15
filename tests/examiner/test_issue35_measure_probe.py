"""Independent checks of measurement status encoding and recording, using invented numbers."""

import importlib.util
import json
import math
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from rhi_reporter import measure
from rhi_reporter.status import CommitStatus, State

ROOT = Path(__file__).resolve().parents[2]
FIRST = "c" * 40
SECOND = "d" * 40


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WRITER = load_module("issue35_record", ROOT / "tools/measurements/record.py")
FORMAT = load_module("issue35_format", ROOT / "tests/unit/test_measurements.py")


@pytest.mark.parametrize(
    ("verdict", "state"),
    [(measure.Verdict.PASS, "success"), (measure.Verdict.FAIL, "failure"),
     (measure.Verdict.NOT_MEASURED, "error")],
)
def test_encoding_parts_are_bounded_and_round_trip(verdict, state):
    numbers = {f"latency_stream_{i}_p95": i + 0.125 for i in range(18)}
    numbers.update(preemptions=0, change=-3, tiny=1e-200, huge=1e200)
    statuses = measure.encode("S5", "8bit", verdict, numbers, names=tuple(numbers), commit=FIRST)
    assert len(statuses) > 1
    decoded_pairs = {}
    for part, status in enumerate(statuses, 1):
        assert status.context == f"measure/S5/8bit/{part}"
        assert status.state == state and status.commit == FIRST
        assert 0 < len(status.description) <= 140
        for pair in status.description.split(" "):
            name, value = pair.split("=")
            assert name in numbers and math.isfinite(float(value))
            decoded_pairs[name] = float(value)
    assert decoded_pairs == numbers
    other = measure.encode("S5", "4bit", verdict, numbers, names=numbers, commit=FIRST)
    assert {s.context for s in statuses}.isdisjoint(s.context for s in other)
    runs = measure.decode([*reversed(statuses), *other])
    assert {(r.measurement, r.label, r.verdict) for r in runs} == {
        ("S5", "8bit", verdict), ("S5", "4bit", verdict),
    }
    assert all(r.numbers == numbers for r in runs)


@pytest.mark.parametrize("fault", ["id", "upper", "slash", "space", "name", "undeclared", "nan", "inf"])
def test_invalid_inputs_build_no_status(monkeypatch, fault):
    arguments: dict[str, Any] = dict(measurement="S1", label="cold", verdict=measure.Verdict.PASS,
                     numbers={"duration": 1, "count": 2}, names=("duration", "count"), commit=FIRST)
    if fault == "id":
        arguments["measurement"] = "S0"
    elif fault in {"upper", "slash", "space"}:
        arguments["label"] = {"upper": "Cold", "slash": "cold/1", "space": "cold run"}[fault]
    elif fault == "name":
        arguments["numbers"] = {"duration": 1, "bad.name": 2}
        arguments["names"] = ("duration", "bad.name")
    elif fault == "undeclared":
        arguments["names"] = ("duration",)
    else:
        arguments["numbers"] = {"duration": 1, "count": float(fault)}

    def unexpected_status(*args, **kwargs):
        raise AssertionError("validation built a status before refusing the input")

    monkeypatch.setattr(measure, "CommitStatus", unexpected_status)
    with pytest.raises(ValueError):
        measure.encode(**arguments)


def test_decoder_joins_handwritten_parts_and_ignores_deployments():
    statuses = [
        CommitStatus(FIRST, State.FAILURE, "measure/S4/download/2", "throughput=4.25e+1 retries=2"),
        CommitStatus(FIRST, State.SUCCESS, "deploy/Test", "The deployment succeeded."),
        CommitStatus(FIRST, State.FAILURE, "measure/S4/download/1", "load_time=101.5"),
    ]
    [run] = measure.decode(statuses)
    assert (run.measurement, run.label, run.verdict) == ("S4", "download", measure.Verdict.FAIL)
    assert run.numbers == {"load_time": 101.5, "throughput": 42.5, "retries": 2}


def api_status(label, description, state="success", at="2026-03-01T23:30:00-02:00", part=1):
    return {"context": f"measure/S5/{label}/{part}", "description": description,
            "state": state, "created_at": at}


def test_raw_api_history_records_runs_and_replaces_only_the_named_run(tmp_path):
    given = [
        api_status("8bit", "speed=18.5"),
        api_status("8bit", "speed=2", "failure", "2026-02-28T10:00:00Z"),
        api_status("4bit", "speed=8.5", "failure"),
        {"context": "verify/Test", "state": "success"},
    ]
    WRITER.record_statuses(given, FIRST, tmp_path, units={"speed": "tokens/s"},
                           pass_conditions={"S5": "Median speed at least 15 tokens/s"})
    path = tmp_path / "s5.md"
    first = FORMAT.front_matter(path.read_text())
    assert FORMAT.folder_problems(tmp_path) == []
    assert first["verdict"] == "fail"
    assert {r["run"] for r in first["runs"]} == {"8bit", "4bit"}
    assert all(r["date"] == date(2026, 3, 2) and r["commit"] == FIRST for r in first["runs"])
    kept_run = next(r for r in first["runs"] if r["run"] == "4bit")
    kept_rows = [r for r in first["rows"] if r["labels"]["run"] == "4bit"]
    path.write_text(path.read_text() + "\nCommentary to keep.\n")
    WRITER.record_statuses([
        api_status("8bit", "speed=21", at="2026-03-04T00:00:00Z"),
        api_status("8bit", "retries=0", at="2026-03-03T23:59:59Z", part=2),
    ], SECOND, tmp_path, units={"retries": "count"})
    updated = FORMAT.front_matter(path.read_text())
    assert next(r for r in updated["runs"] if r["run"] == "4bit") == kept_run
    assert [r for r in updated["rows"] if r["labels"]["run"] == "4bit"] == kept_rows
    assert next(r for r in updated["runs"] if r["run"] == "8bit") == {
        "run": "8bit", "date": date(2026, 3, 4), "commit": SECOND, "verdict": "pass",
    }
    assert {r["name"]: r["value"] for r in updated["rows"] if r["labels"]["run"] == "8bit"} == {
        "speed": 21, "retries": 0,
    }
    WRITER.record_statuses([api_status("8bit", None, "error")], SECOND, tmp_path)
    pending = FORMAT.front_matter(path.read_text())
    assert next(r for r in pending["runs"] if r["run"] == "8bit")["reason"] == "not measured"
    assert next(r for r in pending["runs"] if r["run"] == "8bit")["verdict"] == "pending"
    assert pending["rows"] == kept_rows and pending["verdict"] == "fail"
    WRITER.record_statuses([api_status("4bit", "speed=22")], SECOND, tmp_path)
    assert FORMAT.front_matter(path.read_text())["verdict"] == "pending"
    assert path.read_text().endswith("\nCommentary to keep.\n")
    assert FORMAT.folder_problems(tmp_path) == []


def test_standard_library_command_creates_canary_record(tmp_path):
    statuses = tmp_path / "input.json"
    statuses.write_text(json.dumps([{
        "context": "measure/gpu-canary/weekly/1", "state": "error",
        "description": None, "created_at": "2026-03-05T12:00:00Z",
    }]))
    folder = tmp_path / "records"
    result = subprocess.run([
        sys.executable, "-I", "-S", str(ROOT / "tools/measurements/record.py"),
        "--commit", FIRST, "--statuses", str(statuses), "--folder", str(folder),
        "--pass-condition", "gpu-canary=GPU smoke tests pass",
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    record = FORMAT.front_matter((folder / "gpu-canary.md").read_text())
    assert record["id"] == "gpu-canary" and record["verdict"] == "pending"
    assert record["runs"][0]["reason"] == "not measured" and not record.get("rows")
    assert FORMAT.folder_problems(folder) == []


def test_default_discovery_includes_the_requested_suites():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "tools/measurements/tests" in config["tool"]["pytest"]["ini_options"]["testpaths"]
    assert (ROOT / "tests/unit/reporter/test_measure.py").is_file()
    assert (ROOT / "tools/measurements/tests/test_record.py").is_file()
