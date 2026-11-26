# Task: finish the workshop's production plan

A bicycle workshop makes three products on one machine: frames, wheels and saddles. It plans six
weeks ahead. `model.py` is the start of a mixed-integer model of that plan, solved with HiGHS
through its Python package `highspy`. It is incomplete. Finish it, so that it finds the cheapest
plan and writes it, as numbers and as a LaTeX table.

## The files

| File | What it is |
| --- | --- |
| `instance.json` | The data: the weeks, the machine's hours in each week, and each product's demand, stock at the start, costs and machine hours. Do not change it |
| `model.py` | The model to finish. The places to complete are marked `TODO` |

The data are this copy's own: another copy of the task has other demands or other machine hours,
and another cheapest plan.

## The problem

Every week, each product has a demand, in units, which must be met in that week: from the stock
left by the weeks before, from what is made in the week, or from both. What is not sold stays in
stock for the weeks after. A product starts with its `initial_stock`. Units are whole.

A plan costs, in total:

- `unit_cost` for each unit made;
- `holding_cost` for each unit in stock at the end of a week, every week it stays there;
- `setup_cost` for each week the machine is set up for a product.

`model.py` already holds the units made and kept, the stock balance of each week, and the first
two costs.

### The machine

This is what the model does not know yet:

- A product is made only in a week the machine is set up for it. The machine is set up for a
  product, or it is not: there is no half setup. A setup lasts for its week only, so a product
  made in two weeks needs two setups.
- The machine works at most `machine_hours_per_week` hours in each week. A unit made takes its
  product's `hours_per_unit`, and a setup takes its product's `setup_hours`, out of the hours of
  that week.
- Each setup costs its product's `setup_cost`, as said above.

The plan to find is the one with the lowest total cost.

### The table

`write_table` writes the plan to `plan.tex` as one LaTeX `tabular` environment:

- a header row: the week, then one column for each product, under its name in `instance.json`;
- one row for each week: its number, then the units of each product made in that week;
- a last row with the plan's total cost, and no other number.

Write the total cost as a plain number. Besides the week rows and the last row, no row of the
table holds a number, and no row has more cells than the table has columns.

## What to keep

- Solve the model with HiGHS through `highspy`, which is installed in the Python you run this
  folder with. Install nothing and use no other solver.
- Keep `solve`, `write_plan`, `write_table` and `main` and what they take and return, and the
  fields of `Plan`. Fill `Plan.setups` with the setups of the plan.
- Keep `write_plan` as it is: it writes the plan's numbers to `plan.json`, which is what is
  checked.
- Find the plan by solving the model. Do not write a plan or a cost by hand.

## Running it

```sh
python model.py        # solves the instance, prints the total cost, writes plan.json and plan.tex
```

## How it is checked

The task is done when `python model.py` writes a `plan.json` and a `plan.tex` that hold the
cheapest plan for `instance.json`. Both files are read after the run, by a check that is not in
this folder. It establishes, from the numbers of `plan.json`:

- that the stock of every product balances in every week, and that a product is made only in a
  week with its setup;
- that units are whole and a setup is 0 or 1;
- that every week fits in the machine's hours;
- that the cost the plan states is what its weeks add up to, and that no plan is cheaper;
- that `plan.tex` holds the same units for every product and week, and the same total cost.

A number of `plan.json` may be off by a solver's rounding: one millionth of a unit or of an hour,
and one millionth of the cost, relatively. A number of the table may be off by 0.005, from the
number of `plan.json` it shows. Any cheapest plan is accepted, when there are several.
