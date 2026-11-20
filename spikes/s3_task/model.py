"""A production plan for a bicycle workshop, as a mixed-integer model solved with HiGHS.

The workshop makes a few products on one machine, week by week. `instance.json` gives each
product's weekly demand, its stock at the start, and its costs and machine hours; `TASK.md` states
the problem in full. Run this file to solve the instance and write the plan as a LaTeX table:

    python model.py

It prints the plan's total cost and writes `plan.tex` beside this file.

The model is incomplete: it knows nothing of the machine yet, and the table is not written. The
places to complete are marked TODO.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import highspy

HERE = Path(__file__).resolve().parent
INSTANCE = HERE / "instance.json"
TABLE = HERE / "plan.tex"


@dataclass(frozen=True)
class Plan:
    """A solved plan. Each list has one entry per week, in order."""

    cost: float  # the total cost: production, stock and setups
    production: dict[str, list[int]]  # product -> units made in each week
    stock: dict[str, list[int]]  # product -> units in stock at the end of each week
    setups: dict[str, list[int]]  # product -> 1 in a week the machine is set up for it, else 0


def load_instance(path: Path = INSTANCE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def solve(instance: dict) -> Plan:
    """Build the model of TASK.md for an instance, solve it to proven optimality and return the plan."""
    weeks = range(instance["weeks"])
    products = instance["products"]

    solver = highspy.Highs()
    solver.silent()
    # A proven optimum, and the same one on every run.
    solver.setOptionValue("mip_rel_gap", 0.0)
    solver.setOptionValue("mip_abs_gap", 0.0)
    solver.setOptionValue("threads", 1)

    # Units are whole: a product is made, and kept, in whole units.
    make = {(p, t): solver.addIntegral(lb=0, name=f"make_{p}_{t}") for p in products for t in weeks}
    keep = {(p, t): solver.addIntegral(lb=0, name=f"keep_{p}_{t}") for p in products for t in weeks}

    for p, product in products.items():
        for t in weeks:
            # Stock balance: what was in stock, plus what is made, covers the week's demand; the
            # rest is in stock at the end of the week.
            before = product["initial_stock"] if t == 0 else keep[p, t - 1]
            solver.addConstr(before + make[p, t] == product["demand"][t] + keep[p, t])

    # TODO: the machine (TASK.md, "The machine"). Nothing here says yet that a product is made
    # only in a week the machine is set up for it, or that the machine has only so many hours in
    # a week.

    # TODO: the cost of the setups is missing from the total cost.
    solver.minimize(
        solver.qsum(
            product["unit_cost"] * make[p, t] + product["holding_cost"] * keep[p, t]
            for p, product in products.items()
            for t in weeks
        )
    )
    if solver.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        raise RuntimeError(f"no optimal plan: {solver.modelStatusToString(solver.getModelStatus())}")

    def values(variables: dict, p: str) -> list[int]:
        return [round(solver.val(variables[p, t])) for t in weeks]

    return Plan(
        cost=solver.getObjectiveValue(),
        production={p: values(make, p) for p in products},
        stock={p: values(keep, p) for p in products},
        # TODO: the weeks the machine is set up for each product.
        setups={p: [0 for _ in weeks] for p in products},
    )


def write_table(plan: Plan, path: Path = TABLE) -> None:
    """Write the plan as the LaTeX table TASK.md describes."""
    # TODO: write the table (TASK.md, "The table").
    raise NotImplementedError("the table is not written yet")


def main() -> Plan:
    plan = solve(load_instance())
    write_table(plan)
    print(f"total cost: {plan.cost:g}")
    return plan


if __name__ == "__main__":
    main()
