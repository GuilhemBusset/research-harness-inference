"""The trusted grader of the debug-and-revise task: what the final program wrote, against its cases.

The final program of a trial is run, in a fresh worker and never here, once on each sealed case of
its instance (`CASES`): `python solver.py <case>.json <output folder>/<case>.json`. This grader
then reads the output folder and answers the three properties of `PROPERTIES`, in that order, each
on its own:

- *original*: on the instance's original case, the exemplar the trial was given, the program
  chooses parcels that fit the van and earn the case's optimum;
- *changed*: so it does on the changed case, other weights, fees and capacity, where the
  exemplar's answer is no answer;
- *follow-up*: so it does on both cases once they carry the follow-up's `max_items`, which it
  takes no more parcels than.

Each answer is recomputed from what the program chose: the weight and the fees of the parcels it
names, never the value it states, which must still be what they add up to. The original
constraint (the capacity) is checked on every case, the revised one (`max_items`) on every case
that carries it, so a follow-up that breaks the original case fails *original*. An answer is held
to the case's optimum, not to the reference repair's choice: any optimal choice is accepted, in
any order.

It runs nothing a run submitted and imports no solver. **Tolerances.** A fee may be a decimal, so
`VALUE_TOLERANCE` (relative, and absolute below 1) is allowed between two values; weights and
counts are whole and compared exactly.

An output that is missing or cannot be read is a rejection, with the fault said: `grade` raises
only on what the trusted side gave it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

VALUE_TOLERANCE = 1e-6


class Property(StrEnum):
    ORIGINAL = "original"
    CHANGED = "changed"
    FOLLOW_UP = "follow_up"


# What a grade answers, in the order of the suite's `required_properties`.
PROPERTIES = tuple(Property)

# The sealed cases of an instance, by name, and the property each one answers.
CASES: Mapping[str, Property] = {
    "original": Property.ORIGINAL,
    "changed": Property.CHANGED,
    "follow-up-original": Property.FOLLOW_UP,
    "follow-up-changed": Property.FOLLOW_UP,
}


@dataclass(frozen=True)
class Parcel:
    id: str
    weight: int
    fee: float


@dataclass(frozen=True)
class Case:
    """The data of one case, as the program reads it."""

    capacity: int
    parcels: tuple[Parcel, ...]
    max_items: int | None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Case:
        parcels = tuple(Parcel(one["id"], one["weight"], one["fee"]) for one in raw["parcels"])
        case = cls(capacity=raw["capacity"], parcels=parcels, max_items=raw.get("max_items"))
        if len({parcel.id for parcel in parcels}) != len(parcels) or set(raw) - {"capacity", "parcels", "max_items"}:
            raise ValueError("a case holds a capacity, parcels of distinct ids and, if any, max_items")
        return case

    def to_json(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "capacity": self.capacity,
            "parcels": [{"id": one.id, "weight": one.weight, "fee": one.fee} for one in self.parcels],
        }
        if self.max_items is not None:
            raw["max_items"] = self.max_items
        return raw


@dataclass(frozen=True)
class Sealed:
    """A case and its optimum: the most fees a choice within its constraints earns."""

    case: Case
    optimum: float

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Sealed:
        return cls(Case.parse(raw["case"]), float(raw["optimum"]))


@dataclass(frozen=True)
class Report:
    """What the grader found: each fault with the property it breaks."""

    faults: tuple[tuple[Property, str], ...]

    def holds(self, one: Property) -> bool:
        return all(broken is not one for broken, _fault in self.faults)

    @property
    def properties(self) -> tuple[bool, ...]:
        """One answer for each property, in the order of `PROPERTIES`."""
        return tuple(self.holds(one) for one in PROPERTIES)

    @property
    def accepted(self) -> bool:
        return not self.faults


class _Unreadable(Exception):
    """An output that does not hold what the task asks for."""


def grade(cases: Mapping[str, Sealed], outputs: Path) -> Report:
    """Grade the output the program wrote for each case, `<outputs>/<case>.json`."""
    if set(cases) != set(CASES):
        raise ValueError(f"an instance has the cases {sorted(CASES)}")
    faults: list[tuple[Property, str]] = []
    for name, answers in CASES.items():
        try:
            chosen, stated = _read(outputs / f"{name}.json")
        except _Unreadable as error:
            faults.append((answers, f"{name}: {error}"))
            continue
        faults += [(answers, f"{name}: {fault}") for fault in _faults(chosen, stated, cases[name])]
    return Report(tuple(faults))


def _close(one: float, other: float) -> bool:
    return abs(one - other) <= VALUE_TOLERANCE * max(1.0, abs(other))


def _faults(chosen: list[str], stated: float, sealed: Sealed) -> list[str]:
    case = sealed.case
    by_id = {parcel.id: parcel for parcel in case.parcels}
    unknown = [one for one in chosen if one not in by_id]
    if unknown:
        return [f"the program chose parcels the case does not hold: {', '.join(sorted(set(unknown)))}"]
    if len(set(chosen)) != len(chosen):
        return ["the program chose a parcel more than once"]
    faults = []
    weight = sum(by_id[one].weight for one in chosen)
    value = sum(by_id[one].fee for one in chosen)
    if weight > case.capacity:
        faults.append(f"the parcels chosen weigh {weight:g}, over the capacity of {case.capacity:g}")
    if case.max_items is not None and len(chosen) > case.max_items:
        faults.append(f"the program chose {len(chosen)} parcels, over the limit of {case.max_items}")
    if not _close(stated, value):
        faults.append(f"the program says its parcels earn {stated:g}, and their fees add up to {value:g}")
    if not _close(value, sealed.optimum):
        faults.append(f"the parcels chosen earn {value:g}, which is not the optimum")
    return faults


def _read(path: Path) -> tuple[list[str], float]:
    if not path.is_file():
        raise _Unreadable("no output was written")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise _Unreadable("the output is not a JSON document") from error
    if not isinstance(raw, dict):
        raise _Unreadable("the output is not a JSON object")
    chosen, value = raw.get("chosen"), raw.get("value")
    if not isinstance(chosen, list) or not all(isinstance(one, str) for one in chosen):
        raise _Unreadable("`chosen` is not a list of parcel ids")
    # A boolean is no value, and neither is NaN, an infinity or an integer no float holds.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Unreadable("`value` is not a number")
    try:
        number = float(value)
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        raise _Unreadable("`value` is not a finite number")
    return chosen, number
