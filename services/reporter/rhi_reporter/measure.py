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

**The measurement event.** A job cannot post a status: it holds no GitHub credential. It hands one
run to the reporter as one EventBridge event on the measurement bus, the one its environment
variable `BUS_VARIABLE` names (`src/rhi/pipeline/reporter.py`). `measurement_event` builds the
entry the job gives PutEvents, of source `SOURCE`, and `read_event` gives the statuses of an event
as EventBridge delivers it to the reporter. The event's detail holds, and nothing else of the event
is read:

- `commit`: the commit the job ran from, CodeBuild's `CODEBUILD_RESOLVED_SOURCE_VERSION`;
- `measurement`: the id, one of `IDS`;
- `label`: the run label;
- `verdict`: `pass`, `fail` or `error`;
- `names`: the enumeration of names the job declares, as a list;
- `numbers`: the named numbers, as an object.

`read_event` checks the shape of each, then hands them to `encode`, which checks them again as it
checks a job's own call: whatever either refuses raises ValueError, and no status is built.

**A posting cut short.** `still_to_post` says which of a run's statuses a commit does not hold
yet: those of the parts its newest posting lacks, when every part that posting holds is the run's
own, and every one of them otherwise. The reporter reads it before it posts, and so completes a
posting that stopped part-way rather than begin another.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from rhi_reporter.status import DESCRIPTION_LIMIT, CommitStatus, State

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

MEASURE_CONTEXT = "measure/"

# The measurement event: its source, which the reporter's rule on the measurement bus matches, its
# detail type, and the variable that names the bus to a job.
SOURCE = "rhi.measurement"
DETAIL_TYPE = "Measurement"
BUS_VARIABLE = "RHI_MEASUREMENT_BUS"
_DETAIL_KEYS = ("commit", "measurement", "label", "verdict", "names", "numbers")

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


# The verdict as the event's detail writes it.
_EVENT_VERDICTS = {Verdict.PASS: "pass", Verdict.FAIL: "fail", Verdict.NOT_MEASURED: "error"}


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
    declares: every member has the shape of a name, and every key of `numbers` is one of them. ValueError, before any status is built,
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
    for name in (*declared, *numbers):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("a name is not lower-case letters, digits and underscores after a letter")
    pairs = []
    for name, number in numbers.items():
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


def measurement_event(
    measurement: str,
    label: str,
    verdict: Verdict,
    numbers: Mapping[str, int | float],
    *,
    names: Iterable[str],
    commit: str,
    bus: str,
) -> dict[str, str]:
    """The entry of PutEvents that hands one run to the reporter on the measurement bus `bus`, from
    the commit the job ran from. ValueError for anything `encode` refuses: a job learns of it
    before it sends."""
    declared = list(names)
    encode(measurement, label, verdict, numbers, names=declared, commit=commit)
    detail = {
        "commit": commit,
        "measurement": measurement,
        "label": label,
        "verdict": _EVENT_VERDICTS[verdict],
        "names": declared,
        "numbers": dict(numbers),
    }
    return {"Source": SOURCE, "DetailType": DETAIL_TYPE, "Detail": json.dumps(detail), "EventBusName": bus}


def read_event(event: Mapping[str, object]) -> list[CommitStatus]:
    """The statuses of the measurement event `event`, as EventBridge delivers it, in the order of
    their parts, on the commit it names. Only its detail is read. ValueError, before any status is
    built, for a detail of another shape or anything `encode` refuses."""
    detail = event.get("detail") if isinstance(event, dict) else None
    if not isinstance(detail, dict) or not all(key in detail for key in _DETAIL_KEYS):
        raise ValueError("the event's detail is not a measurement's")
    commit, measurement, label, verdict, names, numbers = (detail[key] for key in _DETAIL_KEYS)
    verdicts = {written: verdict for verdict, written in _EVENT_VERDICTS.items()}
    if not isinstance(verdict, str) or verdict not in verdicts:
        raise ValueError("the verdict is not pass, fail or error")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("the declared names are not a list of names")
    if not isinstance(numbers, dict):
        raise ValueError("the numbers are not named")  # noqa: TRY004 - one error type for every refusal
    if not isinstance(commit, str) or not isinstance(measurement, str) or not isinstance(label, str):
        raise ValueError("the commit, the id or the run label is not text")  # noqa: TRY004 - as above
    # A SHA is posted in lower case, the way GitHub writes it.
    return encode(measurement, label, verdicts[verdict], numbers, names=names, commit=commit.lower())


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


def newest_posting(listed: Iterable[Mapping[str, object]], measurement: str, label: str) -> list[tuple[int, str, str]]:
    """The part, state and description of each status of the newest posting of the run `label`
    of `measurement` among `listed`, a commit's statuses as GitHub lists them, each with its `id`,
    `context`, `state` and `description`: the label's newest status of part 1, by GitHub's id, which
    grows with each status it takes, and each status of another part with a greater id, in the
    order of their ids. Empty when the label has no part 1. A status without an id is passed over."""
    held: list[tuple[int, int, str, str]] = []
    for status in listed:
        number, context, state = status.get("id"), status.get("context"), status.get("state")
        description = status.get("description") or ""
        if type(number) is not int or not isinstance(context, str) or not isinstance(state, str):
            continue
        try:
            of_measurement, of_label, part = context_parts(context)
        except ValueError:
            continue
        if (of_measurement, of_label) == (measurement, label) and isinstance(description, str):
            held.append((number, part, state, description))
    first = max((number for number, part, _, _ in held if part == 1), default=None)
    if first is None:
        return []
    return [(part, state, description) for number, part, state, description in sorted(held) if number >= first]


def still_to_post(statuses: Sequence[CommitStatus], listed: Iterable[Mapping[str, object]]) -> list[CommitStatus]:
    """Of `statuses`, one run's in the order of their parts, those its commit does not hold yet,
    in order: when every status of the run label's newest posting among `listed` (`newest_posting`)
    is equal, in state and description, to the status of `statuses` of its part, the statuses of the
    other parts; otherwise all of them, from part 1."""
    if not statuses:
        return []
    measurement, label, _ = context_parts(statuses[0].context)
    held = newest_posting(listed, measurement, label)
    own = {part: (str(status.state), status.description) for part, status in enumerate(statuses, start=1)}
    if any(own.get(part) != (state, description) for part, state, description in held):
        return list(statuses)
    parts = {part for part, _, _ in held}
    return [status for part, status in enumerate(statuses, start=1) if part not in parts]
