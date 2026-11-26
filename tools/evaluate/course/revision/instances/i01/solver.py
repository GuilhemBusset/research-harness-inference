"""Choose the parcels a delivery van takes: the most fees its load allows.

    python solver.py CASE OUTPUT

`CASE` is a JSON file: the van's `capacity` in whole kilograms and its `parcels`, each with an `id`,
a `weight` in whole kilograms and a `fee`. The program writes to `OUTPUT` the parcels it chooses,
`chosen` (their ids), and what their fees add up to, `value`.

It solves the choice exactly, by dynamic programming over the load.
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
    # best[room]: the most fees the parcels seen so far earn within `room` kilograms.
    best = [0.0] * (capacity + 1)
    taken = [[False] * (capacity + 1) for _ in parcels]
    for index, parcel in enumerate(parcels):
        for room in range(capacity, parcel.weight, -1):
            earned = best[room - parcel.weight] + parcel.fee
            if earned > best[room]:
                best[room] = earned
                taken[index][room] = True
    chosen = []
    room = capacity
    for index in reversed(range(len(parcels))):
        if taken[index][room]:
            chosen.append(parcels[index].id)
            room -= parcels[index].weight
    return sorted(chosen), best[capacity]


def main(case_path: str, output_path: str) -> None:
    case = json.loads(Path(case_path).read_text(encoding="utf-8"))
    parcels = [Parcel(one["id"], one["weight"], one["fee"]) for one in case["parcels"]]
    chosen, value = choose(case["capacity"], parcels)
    Path(output_path).write_text(json.dumps({"chosen": chosen, "value": value}) + "\n", encoding="utf-8")
    print(f"{len(chosen)} parcels, fees {value:g}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
