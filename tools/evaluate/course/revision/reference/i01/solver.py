"""Choose the parcels a delivery van takes: the most fees its load allows.

    python solver.py CASE OUTPUT

`CASE` is a JSON file: the van's `capacity` in whole kilograms and its `parcels`, each with an `id`,
a `weight` in whole kilograms and a `fee`, and, when the depot limits it, `max_items`: how many
parcels the van takes at most. The program writes to `OUTPUT` the parcels it chooses, `chosen`
(their ids), and what their fees add up to, `value`.

It solves the choice exactly, by dynamic programming over the number of parcels and the load.

The reference repair: the load loop now reaches a parcel that fills the room left exactly (it
stopped one kilogram short), and the follow-up's limit is a second dimension of the table.
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


def choose(capacity: int, parcels: list[Parcel], max_items: int | None = None) -> tuple[list[str], float]:
    """The ids of the parcels that earn the most within `capacity` and `max_items`, and their fees."""
    limit = len(parcels) if max_items is None else min(max_items, len(parcels))
    # best[count][room]: the most fees at most `count` of the parcels seen so far earn within `room`.
    best = [[0.0] * (capacity + 1) for _ in range(limit + 1)]
    taken = [[[False] * (capacity + 1) for _ in range(limit + 1)] for _ in parcels]
    for index, parcel in enumerate(parcels):
        for count in range(limit, 0, -1):
            for room in range(capacity, parcel.weight - 1, -1):
                earned = best[count - 1][room - parcel.weight] + parcel.fee
                if earned > best[count][room]:
                    best[count][room] = earned
                    taken[index][count][room] = True
    chosen = []
    count, room = limit, capacity
    for index in reversed(range(len(parcels))):
        if taken[index][count][room]:
            chosen.append(parcels[index].id)
            count, room = count - 1, room - parcels[index].weight
    return sorted(chosen), best[limit][capacity]


def main(case_path: str, output_path: str) -> None:
    case = json.loads(Path(case_path).read_text(encoding="utf-8"))
    parcels = [Parcel(one["id"], one["weight"], one["fee"]) for one in case["parcels"]]
    chosen, value = choose(case["capacity"], parcels, case.get("max_items"))
    Path(output_path).write_text(json.dumps({"chosen": chosen, "value": value}) + "\n", encoding="utf-8")
    print(f"{len(chosen)} parcels, fees {value:g}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
