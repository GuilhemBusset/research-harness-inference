"""Choose the parcels a delivery van takes: the most fees its load allows.

    python solver.py CASE OUTPUT

`CASE` is a JSON file: the van's `capacity` in whole kilograms and its `parcels`, each with an `id`,
a `weight` in whole kilograms and a `fee`, and, when the depot limits it, `max_items`: how many
parcels the van takes at most. The program writes to `OUTPUT` the parcels it chooses, `chosen`
(their ids), and what their fees add up to, `value`.

It solves the choice exactly, by dynamic programming over the parcels, their number and the load,
and then reads back which parcels the best value takes.

The reference repair: reading back a parcel now takes its weight from the room left (the room
stayed the van's, so the parcels read back were not the ones the value adds up), and the
follow-up's limit is a dimension of the table.
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
    # best[index][count][room]: the most fees at most `count` of the first `index` parcels earn
    # within `room` kilograms.
    best = [[[0.0] * (capacity + 1) for _ in range(limit + 1)] for _ in range(len(parcels) + 1)]
    for index, parcel in enumerate(parcels, start=1):
        for count in range(limit + 1):
            for room in range(capacity + 1):
                best[index][count][room] = best[index - 1][count][room]
                if count > 0 and parcel.weight <= room:
                    earned = best[index - 1][count - 1][room - parcel.weight] + parcel.fee
                    best[index][count][room] = max(best[index][count][room], earned)
    chosen = []
    count, room = limit, capacity
    for index in range(len(parcels), 0, -1):
        if best[index][count][room] != best[index - 1][count][room]:
            chosen.append(parcels[index - 1].id)
            count, room = count - 1, room - parcels[index - 1].weight
    return sorted(chosen), best[len(parcels)][limit][capacity]


def main(case_path: str, output_path: str) -> None:
    case = json.loads(Path(case_path).read_text(encoding="utf-8"))
    parcels = [Parcel(one["id"], one["weight"], one["fee"]) for one in case["parcels"]]
    chosen, value = choose(case["capacity"], parcels, case.get("max_items"))
    Path(output_path).write_text(json.dumps({"chosen": chosen, "value": value}) + "\n", encoding="utf-8")
    print(f"{len(chosen)} parcels, fees {value:g}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
