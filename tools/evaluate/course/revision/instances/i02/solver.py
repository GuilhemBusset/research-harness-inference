"""Choose the parcels a delivery van takes: the most fees its load allows.

    python solver.py CASE OUTPUT

`CASE` is a JSON file: the van's `capacity` in whole kilograms and its `parcels`, each with an `id`,
a `weight` in whole kilograms and a `fee`. The program writes to `OUTPUT` the parcels it chooses,
`chosen` (their ids), and what their fees add up to, `value`.

It takes the parcels that pay best for their weight first, while they fit.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Parcel:
    id: str
    weight: int
    fee: float


def choose(capacity: int, parcels: list[Parcel]) -> tuple[list[str], float]:
    """The ids of the parcels that earn the most within `capacity`, and their fees."""
    chosen = []
    room, value = capacity, 0.0
    for parcel in sorted(parcels, key=lambda one: one.fee / one.weight, reverse=True):
        if parcel.weight <= room:
            chosen.append(parcel.id)
            room -= parcel.weight
            value += parcel.fee
    return sorted(chosen), value


def main(case_path: str, output_path: str) -> None:
    case = json.loads(Path(case_path).read_text(encoding="utf-8"))
    parcels = [Parcel(one["id"], one["weight"], one["fee"]) for one in case["parcels"]]
    chosen, value = choose(case["capacity"], parcels)
    Path(output_path).write_text(json.dumps({"chosen": chosen, "value": value}) + "\n", encoding="utf-8")
    print(f"{len(chosen)} parcels, fees {value:g}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
