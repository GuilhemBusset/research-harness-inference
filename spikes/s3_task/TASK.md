# Task: finish the workshop's production plan

A bicycle workshop makes three products on one machine: frames, wheels and saddles. It plans six
weeks ahead. `model.py` is the start of a mixed-integer model of that plan, solved with HiGHS
through its Python package `highspy`. It is incomplete. Finish it, so that it finds the cheapest
plan and writes it as a LaTeX table.

## The files

| File | What it is |
| --- | --- |
| `instance.json` | The data: the weeks, the machine's hours in each week, and each product's demand, stock at the start, costs and machine hours. Do not change it |
| `model.py` | The model to finish. The places to complete are marked `TODO` |
| `check_task.py` | The check that says whether the task is done. Do not change it |

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

- a header row: the week, then one column for each product;
- one row for each week: its number, then the units of each product made in that week;
- a last row with the plan's total cost.

Write the total cost as a plain number.

## What to keep

- Solve the model with HiGHS through `highspy`, which is installed in the Python you run this
  folder with. Install nothing and use no other solver.
- Keep `solve`, `write_table` and `main` and what they take and return, and the fields of `Plan`:
  the check calls `main` and reads the plan it returns. Fill `Plan.setups` with the setups of
  the plan.
- Find the plan by solving the model. Do not write a plan or a cost by hand.

## Running it

```sh
python model.py        # solves the instance, prints the total cost, writes plan.tex
python check_task.py   # exits 0 when the task is done, and says what is wrong otherwise
```

The task is done when `python check_task.py` prints `PASS` and exits 0.
