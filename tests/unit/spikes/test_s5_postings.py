"""How spike S5's postings reach its record (`spikes/s5_capacity.py`): a run is a pass only from
one whole posting, and what one job's preemptions say. No number here is a measurement; the keys
are generated.
"""

from __future__ import annotations

import dataclasses
import json
import tomllib
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import s5_capacity as s5
from rhi_common.keys import generate_key
from rhi_reporter.measure import Verdict, decode, read_event
from test_s5 import COMMIT, blank_record, job, settings, verdict  # the recorded runs of the verdict's own tests

if TYPE_CHECKING:
    from collections.abc import Mapping

LABEL = "8bit-baseline-1"


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Runs the job, its runs at the class size given, and returns the statuses its event becomes."""
    sent: list[dict[str, str]] = []
    events = SimpleNamespace(put_events=lambda Entries: sent.extend(Entries) or {"FailedEntryCount": 0})
    monkeypatch.setattr(s5.boto3, "client", lambda service: events)
    monkeypatch.setattr(s5.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(s5.common, "canary_keys", lambda: SimpleNamespace(students=(generate_key(),)))
    monkeypatch.setattr(s5.config, "read_ssm", settings)

    def posted(class_size: int = 20, **at_class: Any) -> list[Any]:
        monkeypatch.setattr(s5.Job, "measure", lambda self: job(class_size=class_size, **at_class))
        environ = {
            "AWS_REGION": "us-east-1", "RHI_SPIKE_WORKSPACE_URL": "https://example.com/workspace",
            "RHI_SPIKE_ENDPOINT": "example.com", "RHI_S5_BUILD": "8bit", "RHI_S5_CLASS_SIZE": str(class_size),
            "RHI_S5_DURATION_SECONDS": "600", "RHI_MEASUREMENT_BUS": "rhi-measurements",
            "CODEBUILD_RESOLVED_SOURCE_VERSION": COMMIT,
        }  # fmt: skip
        assert s5.main(environ) is Verdict.PASS
        return read_event({"detail": json.loads(sent[-1]["Detail"])})

    return posted


def listed(*postings: list[Any], at: str = "2026-11-20T10:00:00Z") -> list[dict[str, Any]]:
    """The statuses as GitHub lists them: each with its id, in the order they were posted, the
    newest first."""
    ids = count(1)
    found = [
        {"id": next(ids), "context": one.context, "state": str(one.state), "description": one.description,
         "created_at": at}
        for posting in postings
        for one in posting
    ]  # fmt: skip
    return found[::-1]


def recorded(tmp_path: Path, statuses: list[dict[str, Any]]) -> dict[str, Any]:
    path = blank_record(tmp_path / "s5.md")
    s5.record_statuses(statuses, COMMIT, pool="coursework", instance_type="g7e.2xlarge", path=path)
    return tomllib.loads(path.read_text(encoding="utf-8").split("+++")[1])


def at_class(held: dict[str, Any], name: str) -> set[float]:
    """The values of a quantity at the class's size."""
    return {
        row["value"] for row in held["rows"] if row["name"] == name and row["labels"].get("streams") in {None, "class"}
    }


def test_overlapping_postings_of_one_run_are_never_joined(post: Any, tmp_path: Path) -> None:
    older, newer = post(20, p95=4.0), post(30, p95=3.0)
    assert len(older) == len(newer) > 1
    # The newer posting began before the older one had ended: neither is read, and no row is written.
    for statuses in (listed(older[:1], newer[:1], older[1:]), listed(older[:1], newer[:1], older[1:], newer[1:])):
        held = recorded(tmp_path, statuses)
        assert [(one["run"], one["verdict"]) for one in held["runs"]] == [(LABEL, "pending")]
        assert held["verdict"] == "pending" and held.get("rows", []) == []
    # One after the other, the newer is read alone, even in the same second.
    held = recorded(tmp_path, listed(older, newer))
    assert held["verdict"] == "pass"
    assert at_class(held, "class_size") == {30} and at_class(held, "ttft_p95") == {3.0}


def test_a_posting_is_a_pass_only_once_its_last_part_is_listed(post: Any, tmp_path: Path) -> None:
    whole = post()
    cost = {"name": "cost_per_million_output_tokens", "session_shape": "several-agents", "streams": "class"}

    def has_cost(held: dict[str, Any]) -> bool:
        return any(cost.items() <= ({"name": row["name"]} | row["labels"]).items() for row in held["rows"])

    held = recorded(tmp_path, listed(whole[:-1]))
    assert [(one["run"], one["verdict"]) for one in held["runs"]] == [(LABEL, "pending")]
    assert held["verdict"] == "pending" and not has_cost(held)
    held = recorded(tmp_path, listed(whole))
    assert held["verdict"] == "pass" and has_cost(held)


def test_a_posting_missing_a_middle_part_keeps_the_rows_it_has(post: Any, tmp_path: Path) -> None:
    whole = post()
    assert len(whole) >= 3
    gapped = [whole[0], *whole[2:]]
    held = recorded(tmp_path, listed(gapped))
    assert [(one["run"], one["verdict"]) for one in held["runs"]] == [(LABEL, "pending")]
    assert held["verdict"] == "pending"
    assert len(held["rows"]) == sum(len(one.numbers) for one in decode(whole)) - len(whole[1].description.split(" "))
    held = recorded(tmp_path, listed(whole))
    assert held["verdict"] == "pass"
    assert len(held["rows"]) == sum(len(one.numbers) for one in decode(whole))


def test_a_retry_in_the_same_second_is_read_alone(post: Any, tmp_path: Path) -> None:
    first = post()
    retry = read_event({"detail": {
        "commit": COMMIT, "measurement": "S5", "label": LABEL, "verdict": "error",
        "names": [name.value for name in s5.Number], "numbers": {},
    }})  # fmt: skip
    held = recorded(tmp_path, listed(first, retry))
    assert [(one["run"], one["verdict"]) for one in held["runs"]] == [(LABEL, "pending")]
    assert held.get("rows", []) == []


def test_a_preemption_one_instance_saw_counts_whatever_another_left_unsaid() -> None:
    def at_class(*preemptions: float | None) -> list[s5.Measured]:
        runs = job()
        for at, one in enumerate(runs[-2:], start=len(runs) - 2):
            instance = one.instances["instance-a"]
            instances = {
                f"instance-{n}": dataclasses.replace(
                    instance, engine=dataclasses.replace(instance.engine, preemptions=p)
                )
                for n, p in enumerate(preemptions)
            }
            runs[at] = dataclasses.replace(one, instances=instances)
        return runs

    assert verdict(at_class(1, None)) is Verdict.FAIL
    assert verdict(at_class(0, None)) is Verdict.NOT_MEASURED
    assert verdict(at_class(0, 0)) is Verdict.PASS


def test_a_whole_posting_is_whole_in_any_order_of_its_numbers(post: Any, tmp_path: Path) -> None:
    whole = post()
    numbers = {name: value for one in decode(whole) for name, value in one.numbers.items()}
    # JSON's members have no order: the reporter may take the numbers in another, which moves the
    # class's size and the count to other parts.
    for order in (sorted(numbers), sorted(numbers, reverse=True)):
        event = {"commit": COMMIT, "measurement": "S5", "label": LABEL, "verdict": "pass",
                 "names": [name.value for name in s5.Number], "numbers": {name: numbers[name] for name in order}}  # fmt: skip
        reordered = read_event({"detail": event})
        held = recorded(tmp_path, listed(reordered))
        assert held["verdict"] == "pass"
        assert len(held["rows"]) == len(numbers)
        held = recorded(tmp_path, listed(reordered[1:2], reordered[:1]))
        assert held["verdict"] == "pending"


def test_a_status_listed_on_two_pages_is_read_once(post: Any, tmp_path: Path) -> None:
    statuses = listed(post())
    boundary = len(statuses) // 2
    pages = json.dumps(statuses[:boundary]) + "\n" + json.dumps(statuses[boundary - 1 :])
    held = recorded(tmp_path, s5.statuses_of(pages))
    assert held["verdict"] == "pass"
    # Two statuses of one part are two postings, and cannot be told apart.
    again = [*statuses[:boundary], {**statuses[boundary - 1], "id": 1_000}, *statuses[boundary:]]
    assert recorded(tmp_path, again)["verdict"] == "pending"


def test_a_job_served_by_two_instance_types_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    one_job = s5.Job(
        pool="coursework", engine="vllm", prices={}, lever="baseline", streams=(1,), class_size=1, duration=600,
        keys=[generate_key()], endpoint="https://example.com", gateway=None, workspace=None, log=lambda _: None,  # ty: ignore[invalid-argument-type] - no run reaches them
    )  # fmt: skip
    measured = {one.label.shape: one for one in job()[-len(s5.Shape) :]}  # one of each shape

    def served(by_shape: Mapping[s5.Shape, tuple[str, ...]]) -> Any:
        def run(planned: s5.Planned) -> s5.Measured:
            one = measured[planned.shape]
            return dataclasses.replace(
                one, label=dataclasses.replace(one.label, instance_types=by_shape[planned.shape])
            )

        return run

    monkeypatch.setattr(one_job, "run", served(dict.fromkeys(s5.Shape, ("g7e.2xlarge",))))
    assert {one.label.instance_types for one in one_job.measure()} == {("g7e.2xlarge",)}
    hands_on, several = s5.Shape
    for by_shape in (
        {hands_on: ("g7e.2xlarge",), several: ("g7e.4xlarge",)},
        dict.fromkeys(s5.Shape, ("g7e.2xlarge", "g7e.4xlarge")),
    ):
        monkeypatch.setattr(one_job, "run", served(by_shape))
        with pytest.raises(ValueError, match="instance type"):
            one_job.measure()
