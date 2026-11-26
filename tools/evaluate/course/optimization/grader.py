"""The trusted grader of the production-planning task: what a run produced, against its instance.

It reads two files a run wrote, `plan.json` (the plan's numbers) and `plan.tex` (its table), and
answers the five properties of `PROPERTIES`, in that order, each on its own:

- *feasibility*: nothing made or kept is negative, the stock of every product balances in every
  week, and a product is made only in a week the machine is set up for it;
- *integrality*: units made and kept are whole, and a setup is 0 or 1;
- *capacity*: the units made and the setups of a week fit in the machine's hours of that week;
- *objective*: the cost the plan states is what its weeks add up to, and that is the reference
  optimum of the instance;
- *table*: the table holds, for every week, the units of each product the plan makes, in its
  last row the cost of the plan, and no other number.

It runs nothing a run submitted, imports no model and no solver, and compares a plan with the
instance and with the optimum, never with the reference plan: any optimal plan is accepted, and so
is any table that says the same in another notation.

**Tolerances.** A number of the plan is a solver's: `UNIT_TOLERANCE` (absolute) is allowed on a
unit, a setup, a stock balance and a week's hours, and `COST_TOLERANCE` (relative) between two
costs. A number read from the table is a rounded one: it may differ from the plan's by
`TABLE_TOLERANCE` (absolute), that difference included.

A file that is missing or cannot be read is a rejection, with the fault said: `grade` raises only
on what the trusted side gave it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

PLAN = "plan.json"
TABLE = "plan.tex"

UNIT_TOLERANCE = 1e-6
COST_TOLERANCE = 1e-6
TABLE_TOLERANCE = 0.005


class Property(StrEnum):
    FEASIBILITY = "feasibility"
    INTEGRALITY = "integrality"
    CAPACITY = "capacity"
    OBJECTIVE = "objective"
    TABLE = "table"


# What a grade answers, in the order of the suite's `required_properties`.
PROPERTIES = tuple(Property)


@dataclass(frozen=True)
class Product:
    name: str
    demand: tuple[float, ...]
    initial_stock: float
    unit_cost: float
    holding_cost: float
    setup_cost: float
    hours_per_unit: float
    setup_hours: float


@dataclass(frozen=True)
class Instance:
    """The data of one instance, as `instance.json` gives them."""

    weeks: int
    machine_hours: tuple[float, ...]
    products: tuple[Product, ...]

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Instance:
        costs = ("initial_stock", "unit_cost", "holding_cost", "setup_cost", "hours_per_unit", "setup_hours")
        products = tuple(
            Product(name, tuple(fields["demand"]), *(fields[key] for key in costs))
            for name, fields in raw["products"].items()
        )
        instance = cls(weeks=raw["weeks"], machine_hours=tuple(raw["machine_hours_per_week"]), products=products)
        if not products or any(len(series) != instance.weeks for series in instance._series()):
            raise ValueError("an instance has a product, and one number for each week in every series")
        return instance

    def _series(self) -> list[tuple[float, ...]]:
        return [self.machine_hours, *(product.demand for product in self.products)]


@dataclass(frozen=True)
class Plan:
    """The numbers of `plan.json`: for each product, one number for each week."""

    cost: float
    production: Mapping[str, tuple[float, ...]]
    stock: Mapping[str, tuple[float, ...]]
    setups: Mapping[str, tuple[float, ...]]


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
    """A produced file that does not hold what the task asks for."""


def grade(instance: Instance, optimum: float, artifacts: Path) -> Report:
    """Grade the `plan.json` and `plan.tex` of a folder against an instance and its optimum."""
    faults: list[tuple[Property, str]] = []
    try:
        plan = _read_plan(artifacts / PLAN, instance)
    except _Unreadable as error:
        # No plan: no property of it is established, and a table has nothing to agree with.
        return Report(tuple((one, str(error)) for one in PROPERTIES))
    faults += _plan_faults(plan, instance, optimum)
    try:
        table = _read_table(artifacts / TABLE, instance)
    except _Unreadable as error:
        faults.append((Property.TABLE, str(error)))
    else:
        faults += [(Property.TABLE, fault) for fault in _table_faults(table, plan, instance)]
    return Report(tuple(faults))


def cost_of(plan: Plan, instance: Instance) -> float:
    """What the weeks of a plan add up to: the units made, the units kept and the setups."""
    return sum(
        product.unit_cost * plan.production[product.name][t]
        + product.holding_cost * plan.stock[product.name][t]
        + product.setup_cost * plan.setups[product.name][t]
        for product in instance.products
        for t in range(instance.weeks)
    )


def _plan_faults(plan: Plan, instance: Instance, optimum: float) -> list[tuple[Property, str]]:
    # Each test is written so that a sum that is no number (numbers too large to add) fails it.
    faults = []
    for product in instance.products:
        p = product.name
        made, kept, set_up = plan.production[p], plan.stock[p], plan.setups[p]
        for t in range(instance.weeks):
            week = f"week {t + 1}"
            if made[t] < -UNIT_TOLERANCE or kept[t] < -UNIT_TOLERANCE:
                faults.append((Property.FEASIBILITY, f"{week}: a negative number of {p} is made or kept"))
            before = product.initial_stock if t == 0 else kept[t - 1]
            if not abs(before + made[t] - product.demand[t] - kept[t]) <= UNIT_TOLERANCE:
                faults.append((Property.FEASIBILITY, f"{week}: the stock of {p} does not balance"))
            if made[t] > UNIT_TOLERANCE and set_up[t] < 1 - UNIT_TOLERANCE:
                faults.append((Property.FEASIBILITY, f"{week}: {p} is made without a setup"))
            if any(abs(units - round(units)) > UNIT_TOLERANCE for units in (made[t], kept[t])):
                faults.append((Property.INTEGRALITY, f"{week}: {p} is not made and kept in whole units"))
            if min(abs(set_up[t]), abs(set_up[t] - 1)) > UNIT_TOLERANCE:
                faults.append((Property.INTEGRALITY, f"{week}: the setup of {p} is neither 0 nor 1"))
    for t, hours in enumerate(instance.machine_hours):
        used = sum(
            product.hours_per_unit * plan.production[product.name][t]
            + product.setup_hours * plan.setups[product.name][t]
            for product in instance.products
        )
        if not used <= hours + UNIT_TOLERANCE:
            faults.append((Property.CAPACITY, f"week {t + 1}: the machine works {used:g} hours of {hours:g}"))
    cost = cost_of(plan, instance)
    if not abs(plan.cost - cost) <= COST_TOLERANCE * max(1.0, abs(cost)):
        faults.append((Property.OBJECTIVE, f"the plan says it costs {plan.cost:g}, and its weeks add up to {cost:g}"))
    if not abs(cost - optimum) <= COST_TOLERANCE * max(1.0, abs(optimum)):
        faults.append((Property.OBJECTIVE, f"the plan costs {cost:g}, which is not the optimum"))
    return faults


def _number(value: object) -> float:
    # A boolean is no number of a plan, and neither is NaN, an infinity or an integer no float holds.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Unreadable(f"{PLAN} holds something else where a number is expected")
    try:
        number = float(value)
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        raise _Unreadable(f"{PLAN} holds a number that is not finite")
    return number


def _read_plan(path: Path, instance: Instance) -> Plan:
    if not path.is_file():
        raise _Unreadable(f"{PLAN} was not written")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise _Unreadable(f"{PLAN} is not a JSON document") from error
    if not isinstance(raw, dict):
        raise _Unreadable(f"{PLAN} is not a JSON object")

    def series(field: str) -> dict[str, tuple[float, ...]]:
        table = raw.get(field)
        names = [product.name for product in instance.products]
        if not isinstance(table, dict) or sorted(table) != sorted(names):
            raise _Unreadable(f"{PLAN}: `{field}` does not hold the products of the instance, each once")
        if any(not isinstance(table[name], list) or len(table[name]) != instance.weeks for name in names):
            raise _Unreadable(f"{PLAN}: `{field}` does not hold one number for each week")
        return {name: tuple(_number(value) for value in table[name]) for name in names}

    return Plan(
        cost=_number(raw.get("cost")),
        production=series("production"),
        stock=series("stock"),
        setups=series("setups"),
    )


# --- The table -------------------------------------------------------------------------------
#
# A table is read as TASK.md lays it out. One row names every product: the header, which gives
# each product its column. Of the other rows, only those that hold a number say something: the
# last of them is the total's, and holds the cost and no other number; every one before it is a
# week's, with the week's number and the units of each product. A row that holds a number and is
# neither, a second row for a week, and a row with more cells than the tabular declares columns
# are faults: no number of the table is left unread, so none can say otherwise than the plan.


@dataclass(frozen=True)
class _Table:
    """What a table says: the units of each product in each week of the instance it has a row
    for, the number of its last row when that row holds one alone, and what is wrong with it."""

    production: Mapping[int, Mapping[str, Decimal]]
    total: Decimal | None
    faults: tuple[str, ...]


_GROUP = r"\{(?:[^{}]|\{[^{}]*\})*\}"
# A % starts a comment unless it is written \%: the end of a row before it (\\%) does not escape it.
_COMMENT = re.compile(r"(?<!\\)((?:\\\\)*)%.*")
_TABULAR = re.compile(
    r"\\begin\{tabular\}\s*(?:\[[^\]]*\])?\s*(\{(?:[^{}]|" + _GROUP + r")*\})(.*?)\\end\{tabular\}", re.DOTALL
)
# In a column specification, *{3}{r} is rrr.
_REPEAT = re.compile(r"\*\s*\{\s*(\d{1,3})\s*\}\s*(" + _GROUP + r")")
_LONGEST_SPECIFICATION = 10_000
# A thousands separator is part of its number: 10\,846, 10{,}846 and 10,846.
_THOUSANDS = re.compile(r"(?<=\d)(?:\\,|\{,\}|,)(?=\d{3}(?!\d))")
# So is a minus sign before it, however it is written: -12, $-$12, \textminus 12.
_MINUS = re.compile(r"−|\\textminus(?:\{\})?")
_SIGNED = re.compile(r"(-(?:[\s${}]|\\[()])*)?(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
# A rule, a space or a colour, with what it is given (\cline{2-4}), is no cell's content.
_RULE = re.compile(
    r"\\(?:hline|toprule|midrule|bottomrule|cline|cmidrule|addlinespace|specialrule|rule|vspace|hspace|noalign"
    r"|rowcolor|cellcolor)\*?(?:\s*(?:\[[^\]]*\]|\([^)]*\)|" + _GROUP + r"))*"
)
_ROW_END = re.compile(r"(?:\\\\\*?|\\(?:tabularnewline|cr)(?![A-Za-z]))(?:\s*\[[^\]]*\])?")
_CELL_END = re.compile(r"(?<!\\)&")
_MULTICOLUMN = re.compile(r"\\multicolumn\s*\{\s*(\d+)\s*\}\s*" + _GROUP)


def _numbers(text: str) -> list[Decimal]:
    """The numbers of a text, each as it is written."""
    return [-Decimal(digits) if minus else Decimal(digits) for minus, digits in _SIGNED.findall(text)]


def _agree(shown: Decimal, value: float) -> bool:
    """Whether a number of the table is a number of the plan, within `TABLE_TOLERANCE`. Both are
    compared as the decimals they are written as: a difference of the tolerance itself is within it."""
    try:
        return math.isfinite(value) and abs(shown - Decimal(repr(value))) <= Decimal(repr(TABLE_TOLERANCE))
    except ArithmeticError:  # a number of the table with more digits than can be subtracted from
        return False


def _width(specification: str) -> int:
    """The number of columns a tabular declares: {@{}l*{2}{r}p{2cm}} declares four."""
    columns = specification[1:-1]
    while repeat := _REPEAT.search(columns):
        if len(columns) > _LONGEST_SPECIFICATION:
            raise _Unreadable(f"{TABLE}: the columns of the tabular cannot be counted")
        repeated = int(repeat.group(1)) * repeat.group(2)[1:-1]
        columns = columns[: repeat.start()] + repeated + columns[repeat.end() :]
    # What is left in braces or brackets is given to a column (p{2cm}) or put beside one (@{}).
    return len(re.findall(r"[A-Za-z]", re.sub(_GROUP + r"|\[[^\]]*\]", "", columns)))


def _cells(row: str, width: int) -> list[str] | None:
    """The cells of a row, one for each column of the table: a cell that spans several is followed
    by empty ones, and so is a row that stops early. None for a row with more cells than columns."""
    cells: list[str] = []
    for cell in _CELL_END.split(row):
        spanning = _MULTICOLUMN.search(cell)
        digits = spanning.group(1) if spanning else "1"
        span = max(1, int(digits)) if len(digits) <= 3 else width + 1
        if len(cells) + span > width:
            return None
        cells += [_MULTICOLUMN.sub("", cell), *[""] * (span - 1)]
    return cells + [""] * (width - len(cells))


def _columns(cells: list[str], instance: Instance) -> dict[str, int] | None:
    """The column of each product, when a row is the header: each product named by one cell."""
    columns = {}
    for product in instance.products:
        name = re.compile(rf"(?<![A-Za-z]){re.escape(product.name)}s?(?![A-Za-z])", re.IGNORECASE)
        named = [index for index, cell in enumerate(cells) if name.search(cell)]
        if len(named) != 1:
            return None
        columns[product.name] = named[0]
    return columns if len(set(columns.values())) == len(columns) else None


def _read_table(path: Path, instance: Instance) -> _Table:
    if not path.is_file():
        raise _Unreadable(f"{TABLE} was not written")
    try:
        latex = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        raise _Unreadable(f"{TABLE} is not a text file") from error
    for specification, body in _TABULAR.findall(_COMMENT.sub(r"\1", latex)):
        width = _width(specification)
        plain = _RULE.sub(" ", _MINUS.sub("-", _THOUSANDS.sub("", body)))
        rows = [_cells(row, width) for row in _ROW_END.split(plain)]
        for header, cells in enumerate(rows):
            columns = _columns(cells, instance) if cells else None
            if columns:
                return _table_of(rows[:header] + rows[header + 1 :], columns, instance)
    raise _Unreadable(f"{TABLE} holds no tabular environment with a header row that names every product")


def _table_of(rows: list[list[str] | None], columns: Mapping[str, int], instance: Instance) -> _Table:
    """Read the rows of a table besides its header, given the column of each product."""
    faults = []
    if any(cells is None for cells in rows):
        faults.append("a row of the table has more cells than the table has columns")
    # The week is the first column that is no product's; a header that fills every column leaves none.
    week_column = next((index for index in range(len(columns) + 1) if index not in columns.values()), None)
    numbered = [cells for cells in rows if cells and any(_numbers(cell) for cell in cells)]
    production: dict[int, dict[str, Decimal]] = {}
    for cells in numbered[:-1]:
        week = _numbers(cells[week_column]) if week_column is not None and week_column < len(cells) else []
        units = {p: _numbers(cells[column]) for p, column in columns.items()}
        if len(week) != 1 or week[0] not in range(1, instance.weeks + 1) or any(len(u) != 1 for u in units.values()):
            faults.append("a row of the table before its last holds numbers that are not a week's units")
        elif int(week[0]) in production:
            faults.append(f"the table has more than one row for week {int(week[0])}")
        else:
            production[int(week[0])] = {p: read[0] for p, read in units.items()}
    total = [number for cell in numbered[-1] for number in _numbers(cell)] if numbered else []
    if len(total) != 1:
        faults.append("the last row of the table does not hold the total cost and no other number")
    return _Table(production=production, total=total[0] if len(total) == 1 else None, faults=tuple(faults))


def _table_faults(table: _Table, plan: Plan, instance: Instance) -> list[str]:
    faults = list(table.faults)
    for week in range(1, instance.weeks + 1):
        if week not in table.production:
            faults.append(f"the table has no row with the units of every product for week {week}")
            continue
        for p, units in table.production[week].items():
            if not _agree(units, plan.production[p][week - 1]):
                faults.append(
                    f"week {week}: the table says {units} of {p}, and the plan {plan.production[p][week - 1]:g}"
                )
    # The cost of a plan is the one it states or the one its weeks add up to: the objective holds
    # the two to each other, and a table may hold either.
    if table.total is not None and not any(_agree(table.total, cost) for cost in (plan.cost, cost_of(plan, instance))):
        faults.append(f"the last row of the table says the plan costs {table.total}, and the plan {plan.cost:g}")
    return faults
