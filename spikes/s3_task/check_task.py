"""Check the task: exit 0 when the model solves to the optimum and its table holds it.

    python check_task.py

It runs `model.py` (its `main`), checks the plan it returns against `instance.json` and against
the optimum, and reads the table the model wrote, `plan.tex`. It exits 0 when everything holds
and 1 otherwise, with one line for each thing that does not.

Not a test: a plain program, with nothing to install but the solver the model uses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTANCE = HERE / "instance.json"
TABLE = HERE / "plan.tex"

# The cost of the best plan for instance.json.
OPTIMUM = 10846
TOLERANCE = 1e-6


def plan_faults(plan, instance: dict) -> list[str]:
    """What keeps a plan from being the optimal one: a rule of TASK.md it breaks, or its cost."""
    faults = []
    weeks = range(instance["weeks"])
    products = instance["products"]
    hours = instance["machine_hours_per_week"]
    made, kept, set_up = plan.production, plan.stock, plan.setups

    for p, product in products.items():
        for t in weeks:
            week = f"week {t + 1}"
            if made[p][t] < 0 or kept[p][t] < 0 or any(abs(x - round(x)) > TOLERANCE for x in (made[p][t], kept[p][t])):
                faults.append(f"{week}: {p} is not made and kept in whole units")
            before = product["initial_stock"] if t == 0 else kept[p][t - 1]
            if abs(before + made[p][t] - product["demand"][t] - kept[p][t]) > TOLERANCE:
                faults.append(f"{week}: the stock of {p} does not balance")
            if set_up[p][t] not in (0, 1):
                faults.append(f"{week}: the setup of {p} is neither 0 nor 1")
            elif made[p][t] > TOLERANCE and set_up[p][t] != 1:
                faults.append(f"{week}: {p} is made without a setup")
    for t in weeks:
        used = sum(
            product["hours_per_unit"] * made[p][t] + product["setup_hours"] * set_up[p][t]
            for p, product in products.items()
        )
        if used > hours[t] + TOLERANCE:
            faults.append(f"week {t + 1}: the machine works {used:g} hours of {hours[t]}")

    cost = sum(
        product["unit_cost"] * made[p][t] + product["holding_cost"] * kept[p][t] + product["setup_cost"] * set_up[p][t]
        for p, product in products.items()
        for t in weeks
    )
    if abs(plan.cost - cost) > TOLERANCE * max(1.0, abs(cost)):
        faults.append(f"the plan says it costs {plan.cost:g}, and its weeks add up to {cost:g}")
    if abs(cost - OPTIMUM) > TOLERANCE * OPTIMUM:
        faults.append(f"the plan costs {cost:g}, which is not the optimum")
    return faults


def numbers_in_tables(latex: str) -> list[float]:
    """Every number inside a `tabular` environment, with its sign, comments left out."""
    latex = re.sub(r"(?<!\\)%.*", "", latex)
    numbers = []
    for body in re.findall(r"\\begin\{tabular\}(.*?)\\end\{tabular\}", latex, flags=re.DOTALL):
        # A thousands separator is part of its number: 10\,846, 10{,}846 and 10,846.
        plain = re.sub(r"(?<=\d)(?:\\,|\{,\}|,)(?=\d{3}(?!\d))", "", body)
        # So is a minus sign before it, however it is written: -10846, $-$10846, \textminus 10846.
        plain = re.sub(r"−|\\textminus(?:\{\})?", "-", plain)
        signed = r"(-(?:[\s${}]|\\[()])*)?(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])"
        numbers += [-float(digits) if minus else float(digits) for minus, digits in re.findall(signed, plain)]
    return numbers


def table_faults(path: Path) -> list[str]:
    if not path.is_file():
        return [f"{path.name} was not written"]
    numbers = numbers_in_tables(path.read_text(encoding="utf-8"))
    if not any(abs(number - OPTIMUM) < 0.005 for number in numbers):
        return [f"{path.name} holds no tabular environment with the optimal cost in it"]
    return []


def main() -> int:
    # A table left by an earlier run says nothing about this one.
    TABLE.unlink(missing_ok=True)
    sys.path.insert(0, str(HERE))
    try:
        import model

        plan = model.main()
        faults = plan_faults(plan, json.loads(INSTANCE.read_text(encoding="utf-8")))
    except Exception as error:  # whatever an unfinished model raises is a failed check
        faults = [f"model.py did not give a plan: {type(error).__name__}: {error}"]
    faults += table_faults(TABLE)

    for fault in faults:
        print(f"FAIL: {fault}")
    if not faults:
        print("PASS: the plan is optimal and the table holds its cost")
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main())
