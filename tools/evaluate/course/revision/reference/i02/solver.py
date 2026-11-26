"""Choose the parcels a delivery van takes: the most fees its load allows.

    python solver.py CASE OUTPUT

`CASE` is a JSON file: the van's `capacity` in whole kilograms and its `parcels`, each with an `id`,
a `weight` in whole kilograms and a `fee`, and, when the depot limits it, `max_items`: how many
parcels the van takes at most. The program writes to `OUTPUT` the parcels it chooses, `chosen`
(their ids), and what their fees add up to, `value`.

The reference repair: taking the parcels that pay best for their weight first is no exact method,
so the choice is searched instead, each parcel taken or left, remembering the best of what is left
for each room and number of parcels.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass(frozen=True)
class Parcel:
    id: str
    weight: int
    fee: float


def choose(capacity: int, parcels: list[Parcel], max_items: int | None = None) -> tuple[list[str], float]:
    """The ids of the parcels that earn the most within `capacity` and `max_items`, and their fees."""

    @cache
    def best(index: int, room: int, left: int) -> tuple[float, tuple[str, ...]]:
        # The most the parcels from `index` on earn within `room` kilograms and `left` parcels.
        if index == len(parcels) or left == 0:
            return 0.0, ()
        leave = best(index + 1, room, left)
        parcel = parcels[index]
        if parcel.weight > room:
            return leave
        value, rest = best(index + 1, room - parcel.weight, left - 1)
        take = value + parcel.fee, (parcel.id, *rest)
        return take if take[0] > leave[0] else leave

    value, chosen = best(0, capacity, len(parcels) if max_items is None else max_items)
    return sorted(chosen), value


def main(case_path: str, output_path: str) -> None:
    case = json.loads(Path(case_path).read_text(encoding="utf-8"))
    parcels = [Parcel(one["id"], one["weight"], one["fee"]) for one in case["parcels"]]
    chosen, value = choose(case["capacity"], parcels, case.get("max_items"))
    Path(output_path).write_text(json.dumps({"chosen": chosen, "value": value}) + "\n", encoding="utf-8")
    print(f"{len(chosen)} parcels, fees {value:g}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
