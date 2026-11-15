"""One run of a measurement becomes commit statuses and reads back (system design, section 8.4).

The statuses are public, so these tests hold what can reach one: an id of the list, a run label
and names of a fixed shape, and numbers. No number here is a measurement.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from typing import Any

import pytest

from rhi_reporter.measure import IDS, Run, Verdict, context_parts, decode, encode
from rhi_reporter.status import DESCRIPTION_LIMIT, CommitStatus, State

SHA = "1a2b3c4d" * 5


class Speed(StrEnum):
    """A job's enumeration of the names it posts."""

    TIME_TO_FIRST_TOKEN_P95 = "time_to_first_token_p95"
    OUTPUT_SPEED_MEDIAN = "output_speed_per_stream_median"
    PREEMPTIONS = "preemptions"


NUMBERS: dict[str, int | float] = {
    Speed.TIME_TO_FIRST_TOKEN_P95: 3.25,
    Speed.OUTPUT_SPEED_MEDIAN: 19.5,
    Speed.PREEMPTIONS: 0,
}
# A matrix like S5's: too many numbers for one description.
MATRIX_NAMES = [
    f"output_speed_s{streams}_{shape}_p{rank}" for streams in (1, 4, 8, 16) for shape in "ab" for rank in (10, 50)
]
MATRIX = {name: index * 1.5 if index % 2 else index for index, name in enumerate(MATRIX_NAMES)}


def test_a_run_reads_back_as_it_was_written() -> None:
    statuses = encode("S5", "8bit", Verdict.PASS, NUMBERS, names=Speed, commit=SHA)

    assert statuses == [
        CommitStatus(
            commit=SHA,
            state=State.SUCCESS,
            context="measure/S5/8bit/1",
            description="time_to_first_token_p95=3.25 output_speed_per_stream_median=19.5 preemptions=0",
        )
    ]
    assert decode(statuses) == [Run("S5", "8bit", Verdict.PASS, NUMBERS)]

    # Whole numbers stay whole, and no float loses a digit on the way.
    odd = {"a": -7, "b": 24.0, "c": 0.1 + 0.2, "d": 1.5e-7, "e": 2.0e22, "f": 2**70}
    [run] = decode(encode("S1", "cold", Verdict.FAIL, odd, names=odd, commit=SHA))
    assert run.numbers == odd
    assert [type(number) for number in run.numbers.values()] == [type(number) for number in odd.values()]


def test_the_state_says_the_verdict() -> None:
    for verdict, state in ((Verdict.PASS, "success"), (Verdict.FAIL, "failure"), (Verdict.NOT_MEASURED, "error")):
        [status] = encode("gpu-canary", "weekly", verdict, {}, names=(), commit=SHA)
        # A part number is there even when one status holds everything, here nothing.
        assert (status.state, status.context, status.description) == (state, "measure/gpu-canary/weekly/1", "")
        assert decode([status]) == [Run("gpu-canary", "weekly", verdict, {})]


def test_a_long_run_is_split_into_parts_and_joined_back() -> None:
    statuses = encode("S5", "8bit", Verdict.PASS, MATRIX, names=MATRIX_NAMES, commit=SHA)

    assert len(statuses) > 2
    assert [status.context for status in statuses] == [
        f"measure/S5/8bit/{part}" for part in range(1, len(statuses) + 1)
    ]
    assert all(0 < len(status.description) <= DESCRIPTION_LIMIT for status in statuses)
    assert {status.state for status in statuses} == {State.SUCCESS}
    # The parts join whatever order they come back in, and beside another reporter's statuses.
    deployment = CommitStatus(SHA, State.SUCCESS, "deploy/Prod", "The deployment succeeded.")
    [run] = decode([deployment, *reversed(statuses)])
    assert (run.measurement, run.label, run.verdict) == ("S5", "8bit", Verdict.PASS)
    assert list(run.numbers.items()) == list(MATRIX.items())

    # A pair that fills a description to the last character still fits, and one more does not.
    name = "n" * (DESCRIPTION_LIMIT - 2)
    assert (
        len(encode("S5", "8bit", Verdict.PASS, {name: 1}, names=[name], commit=SHA)[0].description) == DESCRIPTION_LIMIT
    )
    with pytest.raises(ValueError, match="do not fit"):
        encode("S5", "8bit", Verdict.PASS, {name: 10}, names=[name], commit=SHA)


def test_two_runs_of_one_measurement_have_contexts_that_differ() -> None:
    by_build = {
        label: encode("S5", label, Verdict.PASS, MATRIX, names=MATRIX_NAMES, commit=SHA) for label in ("8bit", "4bit")
    }
    contexts = [status.context for statuses in by_build.values() for status in statuses]

    assert len(set(contexts)) == len(contexts)
    assert not [context for context in contexts if context.startswith(("deploy/", "verify/"))]
    runs = decode([*by_build["8bit"], *by_build["4bit"]])
    assert [(run.label, run.numbers) for run in runs] == [("8bit", MATRIX), ("4bit", MATRIX)]
    # Every id of the list gives a context that reads back.
    assert len(IDS) == 13
    assert [
        context_parts(encode(each, "warm", Verdict.PASS, {}, names=(), commit=SHA)[0].context)[0] for each in IDS
    ] == list(IDS)


def test_refuses_what_is_not_of_the_fixed_shapes() -> None:
    # Built here, so that this file holds nothing shaped like the real thing.
    arn = ":".join(("arn", "aws", "s3", "", "", "bucket"))
    address = ".".join(("10", "0", "0", "7"))
    name, label, measurement = "a name is not lower-case", "the run label", "the id"
    refused: dict[str, tuple[str, dict[str, Any]]] = {
        "a name outside the enumeration": ("not in the enumeration", dict(numbers={"load_time": 1})),
        "a name shaped like an ARN": (name, dict(numbers={arn: 1}, names=[arn])),
        "a name shaped like an address": (name, dict(numbers={address: 1}, names=[address])),
        "a name shaped like a sentence": (name, dict(numbers={"it failed": 1}, names=["it failed"])),
        "a name that starts with a digit": (name, dict(numbers={"95th": 1}, names=["95th"])),
        "a name with an upper-case letter": (name, dict(numbers={"Preemptions": 1}, names=["Preemptions"])),
        "a name that would add a pair": (name, dict(numbers={"a=1 b": 2}, names=["a=1 b"])),
        "a run label with an upper-case letter": (label, dict(label="8Bit")),
        "a run label with a slash": (label, dict(label="8bit/1")),
        "a run label with a space": (label, dict(label="8 bit")),
        "an empty run label": (label, dict(label="")),
        "an unknown id": (measurement, dict(measurement="S12")),
        "an id in lower case": (measurement, dict(measurement="s5")),
        "a deployment's context as the id": (measurement, dict(measurement="deploy")),
        "a number that is not finite": ("finite number", dict(numbers={"preemptions": float("nan")})),
        "an infinite number": ("finite number", dict(numbers={"preemptions": float("inf")})),
        "a yes or no": ("finite number", dict(numbers={"preemptions": True})),
        "a number as text": ("finite number", dict(numbers={"preemptions": "0"})),
        "a verdict that is a state": ("the verdict", dict(verdict="success")),
        "a short commit": ("the commit", dict(commit=SHA[:7])),
    }
    good: dict[str, Any] = dict(measurement="S5", label="8bit", verdict=Verdict.PASS, names=Speed, commit=SHA)
    for fault, (message, change) in refused.items():
        # The fault comes after good numbers and, where it is a name, in the enumeration: it is
        # refused all the same, and nothing is returned.
        added = {"numbers": {**NUMBERS, **change.get("numbers", {})}, "names": [*Speed, *change.get("names", [])]}
        run: dict[str, Any] = {**good, **change, **added}
        with pytest.raises(ValueError, match=message) as refusal:
            encode(**run)
        # No message repeats what was refused.
        assert not [text for text in (arn, address, "it failed", "8Bit", "S12") if text in str(refusal.value)], fault


def test_decoding_refuses_statuses_that_are_not_one_run_each() -> None:
    statuses = encode("S5", "8bit", Verdict.PASS, MATRIX, names=MATRIX_NAMES, commit=SHA)
    first = statuses[0]

    def changed(**fields: Any) -> CommitStatus:
        return dataclasses.replace(first, **fields)

    refused = {
        "a part is missing": ("none missing", statuses[1:]),
        "a context comes twice": ("one context", [*statuses, first]),
        "the parts disagree on the state": ("one verdict", [changed(state=State.FAILURE), *statuses[1:]]),
        "a name comes twice": ("one name twice", [*statuses[:-1], changed(context=statuses[-1].context)]),
        "the state is no verdict": ("one verdict", [changed(state=State.PENDING)]),
        "the description is a sentence": ("not name=number pairs", [changed(description="The deployment failed.")]),
        "the number is not one": ("not name=number pairs", [changed(description="load_time=fast")]),
        "the number is too large to be finite": ("not finite", [changed(description="load_time=1e999")]),
        "the id is unknown": ("the context", [changed(context="measure/S12/8bit/1")]),
        "the part is not a number from 1": ("the context", [changed(context="measure/S5/8bit/0")]),
        "the context has no part": ("the context", [changed(context="measure/S5/8bit")]),
    }
    for fault, (message, faulty) in refused.items():
        with pytest.raises(ValueError, match=message):
            print(fault, decode(faulty))
