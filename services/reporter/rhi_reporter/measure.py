"""The numbers of a spike or a canary as commit statuses (system design, sections 8.4 and 12).

A measurement posts its result the way a deployment posts its verdict, under the same rule: fixed
names and numbers, no free text. One run of a measurement becomes one or more statuses:

- the context is `measure/<id>/<run label>/<part>`. It is never under `deploy/` or `verify/`,
  which the implement loop reads as facts of a deployment. The id is one of `IDS`. The run label
  is given by the job (`8bit`, `warm`, `cold`): a status replaces the one of the same context, so
  the label keeps two runs of one measurement on one commit apart. The part counts from 1 and is
  there even when one status holds every number;
- the state is the run's verdict: success for a pass, failure for a fail, and error when the job
  could not measure;
- the description is `name=number` pairs separated by single spaces, at most `DESCRIPTION_LIMIT`
  characters. A run with more numbers than that continues in the next part.

`encode` checks everything before it builds a status. A job declares the names of its numbers as
an enumeration and gives it here; the shape of each name is checked as well, since the reporter
that posts runs apart from the job and cannot trust what it is handed: nothing shaped like an
address, an ARN or a sentence passes as a name. No error message repeats what was refused.

`decode` reads the same form back, and joins the parts of each run. It holds no job's
enumeration, so it checks shapes only. A job that posts one run label twice on one commit, the
second time in fewer parts, leaves a part of the first behind: the parts then repeat a name, or
disagree on the state, and `decode` refuses them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from rhi_reporter.status import DESCRIPTION_LIMIT, CommitStatus, State

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

MEASURE_CONTEXT = "measure/"

# The validation spikes of the design's section 12, the weekly GPU canary of section 8.2 and the
# dry-run class of section 14. A record is named after its id in lower case
# (docs/measurements/README.md).
IDS = (*(f"S{number}" for number in range(1, 12)), "gpu-canary", "dry-run-class")

_RUN_LABEL = re.compile(r"[a-z0-9-]+")
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_PART = re.compile(r"[1-9][0-9]*")
_SHA = re.compile(r"[0-9a-fA-F]{40}")
# What `_written` gives: an integer in digits, or a float with a fraction, an exponent or both.
_PAIR = re.compile(r"([a-z][a-z0-9_]*)=(-?[0-9]+)(\.[0-9]+)?(e[+-]?[0-9]+)?")


class Verdict(StrEnum):
    """What a run found. The value is the state its statuses carry."""

    PASS = State.SUCCESS
    FAIL = State.FAILURE
    NOT_MEASURED = State.ERROR


@dataclass(frozen=True)
class Run:
    """One run of a measurement, as its statuses hold it."""

    measurement: str
    label: str
    verdict: Verdict
    numbers: Mapping[str, int | float]


def _written(number: float) -> str:
    """A number as the description carries it: the shortest text that reads back as the same number."""
    return repr(number)


def encode(
    measurement: str,
    label: str,
    verdict: Verdict,
    numbers: Mapping[str, int | float],
    *,
    names: Iterable[str],
    commit: str,
) -> list[CommitStatus]:
    """The statuses of one run, in the order of their parts. `names` is the enumeration the job
    declares: every key of `numbers` is one of its members. ValueError, before any status is built,
    for anything the module's rules do not allow."""
    if measurement not in IDS:
        raise ValueError("the id is not a measurement's")
    if not isinstance(label, str) or not _RUN_LABEL.fullmatch(label):
        raise ValueError("the run label is not lower-case letters, digits and hyphens")
    if not isinstance(verdict, Verdict):
        raise ValueError("the verdict is not pass, fail or not measured")  # noqa: TRY004 - one error type for every refusal
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise ValueError("the commit is not a full commit SHA")
    declared = frozenset(names)
    pairs = []
    for name, number in numbers.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("a name is not lower-case letters, digits and underscores after a letter")
        if name not in declared:
            raise ValueError("a name is not in the enumeration the job declares")
        if isinstance(number, bool) or not isinstance(number, int | float) or not math.isfinite(number):
            raise ValueError("a value is not a finite number")
        pair = f"{name}={_written(number)}"
        if len(pair) > DESCRIPTION_LIMIT:
            raise ValueError("a name and its number do not fit in one description")
        pairs.append(pair)

    descriptions = [""]
    for pair in pairs:
        joined = f"{descriptions[-1]} {pair}" if descriptions[-1] else pair
        if len(joined) > DESCRIPTION_LIMIT:
            descriptions.append(pair)
        else:
            descriptions[-1] = joined
    return [
        CommitStatus(
            commit=commit,
            state=State(verdict.value),
            context=f"{MEASURE_CONTEXT}{measurement}/{label}/{part}",
            description=description,
        )
        for part, description in enumerate(descriptions, start=1)
    ]


def context_parts(context: str) -> tuple[str, str, int]:
    """The id, the run label and the part number a measurement's context names. ValueError for
    any other context."""
    fields = context.split("/") if isinstance(context, str) else []
    if len(fields) != 4 or f"{fields[0]}/" != MEASURE_CONTEXT:
        raise ValueError("the context is not a measurement's")
    _, measurement, label, part = fields
    if measurement not in IDS or not _RUN_LABEL.fullmatch(label) or not _PART.fullmatch(part):
        raise ValueError("the context is not a measurement's")
    return measurement, label, int(part)


def _numbers(description: str) -> dict[str, int | float]:
    numbers: dict[str, int | float] = {}
    for pair in description.split(" ") if description else []:
        match = _PAIR.fullmatch(pair)
        if match is None:
            raise ValueError("a description is not name=number pairs")
        name, whole, fraction, exponent = match.groups()
        number = float(match.group(0).partition("=")[2]) if fraction or exponent else int(whole)
        if not math.isfinite(number):
            raise ValueError("a description holds a number that is not finite")
        if name in numbers:
            raise ValueError("a run gives one name twice")
        numbers[name] = number
    return numbers


def decode(statuses: Iterable[CommitStatus]) -> list[Run]:
    """The runs the statuses hold, in the order their first status comes, the parts of each joined
    in the order of their numbers. A status whose context is not under `measure/` is another
    reporter's and is passed over. ValueError for a context under `measure/` of another form, a
    state that is no verdict, a description that is not pairs, and a run whose parts are not
    exactly 1 to n, disagree on the state or give one name twice."""
    parts: dict[tuple[str, str], dict[int, CommitStatus]] = {}
    for status in statuses:
        if not status.context.startswith(MEASURE_CONTEXT):
            continue
        measurement, label, part = context_parts(status.context)
        of_run = parts.setdefault((measurement, label), {})
        if part in of_run:
            raise ValueError("two statuses have one context")
        of_run[part] = status

    runs = []
    for (measurement, label), of_run in parts.items():
        if sorted(of_run) != list(range(1, len(of_run) + 1)):
            raise ValueError("the parts of a run are not numbered from 1 with none missing")
        states = {str(status.state) for status in of_run.values()}
        if len(states) != 1 or not states <= set(Verdict):
            raise ValueError("the parts of a run do not give one verdict")
        numbers: dict[str, int | float] = {}
        for part in sorted(of_run):
            found = _numbers(of_run[part].description)
            if numbers.keys() & found.keys():
                raise ValueError("a run gives one name twice")
            numbers.update(found)
        runs.append(Run(measurement=measurement, label=label, verdict=Verdict(states.pop()), numbers=numbers))
    return runs
