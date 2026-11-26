"""The command line of the evaluation tool, behind `scripts/evaluate.sh`
(docs/design/agent-session-evaluation.md, "How the operator runs it").

    scripts/evaluate.sh plan --suite course-v1 --profile qualify --students 8 --instances 1

`plan` is offline: it reads the suite and the example configuration, and prints what a run of the
profile would do and what booking it needs. It holds no credentials, calls no service and writes
nothing. `--json` prints the same plan as one JSON document.

**What it prints.** The workload hash, which is the `workload_id` of the manifest of any run of
this plan (`contracts.workload_identity`); the cells in order, each with its trials; the canary
identities and the agent slots needed at once; the trial counts; the scratch space; the bookings,
each with the shortest window that holds it and the command that runs it; the host-hours; and
their price.

**The window and the rate.** A booking is at most `--for` long, the capacity wait included. Left
out, it is `session_max_hours` of `config.example.toml`. The price is the host-hours at `--rate`,
in dollars a host-hour. Left out, it is the example configuration's price of its default pool.
Each is labelled `supplied` or `example`: no real rate or session limit is in the repository, and
the deployed ones are read by `run`, not here.

**The commands.** The batch commands name `run`, which this module does not have yet, and leave
the pool and the experiment issue to the operator's shell (`$pool`, `$experiment_issue`): neither
is known offline.

It exits 0 with a plan and 2 with a refusal, whose reason is one of `plan.Refusal`.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from evaluate import plan as planning
from evaluate.contracts import Profile
from evaluate.plan import Plan, PlanError, Refusal, Schedule

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config.example.toml"
MINUTE_MS = 60_000
_DURATION = re.compile(r"([1-9][0-9]{0,4})([hm])")


class Source:
    """Where a value of the estimate comes from."""

    EXAMPLE = "example"
    SUPPLIED = "supplied"


def _duration_ms(text: str) -> int:
    found = _DURATION.fullmatch(text)
    if not found:
        raise argparse.ArgumentTypeError("needs whole hours or minutes, such as 3h or 90m")
    return int(found[1]) * (60 if found[2] == "h" else 1) * MINUTE_MS


def _rate(text: str) -> float:
    rate = float(text)
    if not math.isfinite(rate) or not 0 < rate < 10**6:
        raise argparse.ArgumentTypeError("needs dollars a host-hour, above zero")
    return rate


def _example() -> tuple[int, float]:
    """The longest session and the price of a host-hour of the example configuration's default pool."""
    config = tomllib.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    pool = config["pools"][config["default_pool"]]
    return config["session_max_hours"] * 60 * MINUTE_MS, float(pool["prices"][pool["instance_types"][0]])


def _minutes(duration_ms: int) -> int:
    return -(-duration_ms // MINUTE_MS)


def _commands(args: argparse.Namespace, bookings: Schedule) -> list[str]:
    """The command of each batch. The first opens the experiment; the others resume it, each with
    a booking of its own."""
    seeds = "".join(
        f" --{name}-seed {seed}"
        for name, seed in (("input", args.input_seed), ("schedule", args.schedule_seed))
        if seed is not None
    )
    same = f'--suite {args.suite} --profile {args.profile} --pool "$pool" --students {args.students} --instances {args.instances}{seeds}'
    return [
        f"scripts/evaluate.sh run {same} --for {_minutes(batch.minimum_window_ms)}m --issue new"
        if batch.batch == 0
        else f'scripts/evaluate.sh run --resume {same} --for {_minutes(batch.minimum_window_ms)}m --issue "$experiment_issue"'
        for batch in bookings.batches
    ]


def document(args: argparse.Namespace, plan: Plan, bookings: Schedule, sources: dict[str, Any]) -> dict[str, Any]:
    """The plan as one JSON value: what `--json` prints, and what the text is written from."""
    host_hours = round(bookings.host_hours(args.instances), 3)
    commands = _commands(args, bookings)
    return {
        "suite": args.suite,
        "profile": args.profile,
        "students": args.students,
        "instances": args.instances,
        "workload_hash": plan.workload_hash,
        "workload": plan.workload.to_json(),
        "cells": [
            cell.identity.to_json()
            | cell.planned()
            | {
                "arrivals_ms": list(cell.arrivals_ms),
                "reserved_ms": plan.reserved_ms(cell),
                "trials": list(cell.trials),
            }
            for cell in plan.cells
        ],
        "trials": [
            trial.to_json()
            | {"loads": list(plan.loads_of(trial)), "deadline_ms": plan.workload.limits.trial_wall_time_ms}
            for trial in plan.workload.trials
        ],
        "counts": {
            "cells": len(plan.cells),
            "trials": len(plan.workload.trials),
            "trials_by_load": dict(plan.trials_by_load),
            "canary_identities": plan.canary_identities,
            "agent_slots": plan.agent_slots,
        },
        "scratch_mib": plan.scratch_mib,
        "window": {"window_ms": bookings.window_ms, "source": sources["window"]},
        "minimum_window_ms": bookings.minimum_window_ms,
        "batches": [
            {
                "batch": batch.batch,
                "cells": list(batch.cells),
                "minimum_window_ms": batch.minimum_window_ms,
                "command": command,
            }
            for batch, command in zip(bookings.batches, commands, strict=True)
        ],
        "host_hours": host_hours,
        "price": {
            "usd_per_host_hour": sources["rate_value"],
            "source": sources["rate"],
            "estimate_usd": round(host_hours * sources["rate_value"], 2),
        },
    }


def render(planned: dict[str, Any]) -> str:
    """The plan as the operator reads it."""
    counts, price = planned["counts"], planned["price"]
    lines = [
        (
            f"plan: suite {planned['suite']}, profile {planned['profile']}, "
            f"{planned['students']} student(s), {planned['instances']} instance(s)"
        ),
        f"workload: {planned['workload_hash']}",
        "",
        "ordered manifest:",
    ]
    trials = planned["trials"]
    for cell in planned["cells"]:
        lines.append(
            f"  cell {cell['cell']}: {cell['phase']} {cell['shape']} slots={cell['agent_slots']} "
            f"repetition={cell['repetition']} roles={','.join(cell['roles'])} identities={cell['identities']} "
            f"admissions={cell['admissions_ms']}ms drain={cell['drain_ms']}ms "
            f"seeds={cell['input_seed']}/{cell['schedule_seed']} namespace={cell['prefix_namespace']}"
        )
        if cell["phase"] == "class_opening":
            lines.append(f"    arrivals_ms: {' '.join(str(start) for start in cell['arrivals_ms'])}")
        lines += [
            f"    trial {trial['trial']}: {trial['instance_id']} on {trial['harness']} attempt={trial['attempt']} "
            f"loads={','.join(trial['loads'])} deadline={trial['deadline_ms']}ms"
            for trial in (trials[at] for at in cell["trials"])
        ]
    by_load = ", ".join(f"{count} at {load}" for load, count in counts["trials_by_load"].items())
    lines += [
        "",
        f"cells: {counts['cells']}",
        f"trials: {counts['trials']} ({by_load})",
        f"canary identities: {counts['canary_identities']}",
        f"agent slots: {counts['agent_slots']}",
        f"scratch estimate: {planned['scratch_mib']} MiB",
        f"booking window: {_minutes(planned['window']['window_ms'])} min ({planned['window']['source']})",
        f"minimum window: {_minutes(planned['minimum_window_ms'])} min, for the longest cell",
        f"host-hours: {planned['host_hours']:.3f}",
        f"priced estimate: {price['estimate_usd']:.2f} USD at {price['usd_per_host_hour']:g} USD a host-hour ({price['source']} rate)",
        "",
        "batches:",
    ]
    for batch in planned["batches"]:
        cells = batch["cells"]
        lines += [
            (
                f"  batch {batch['batch']}: cells {cells[0]} to {cells[-1]} ({len(cells)}), "
                f"minimum window {_minutes(batch['minimum_window_ms'])} min"
            ),
            f"    {batch['command']}",
        ]
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="scripts/evaluate.sh", description="The evaluation of agent sessions.")
    commands = top.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="preview the work, the time and the host-hours of a profile, offline")
    plan.add_argument("--suite", default=planning.DEFAULT_SUITE)
    plan.add_argument("--profile", choices=[profile.value for profile in Profile], required=True)
    plan.add_argument("--students", type=int, required=True, help="the class size")
    plan.add_argument("--instances", type=int, required=True, help="the hosts to book")
    plan.add_argument(
        "--for", "--window", dest="window", type=_duration_ms, help="the longest booking, such as 3h or 90m"
    )
    plan.add_argument("--rate", type=_rate, help="dollars a host-hour")
    plan.add_argument("--input-seed", type=int)
    plan.add_argument("--schedule-seed", type=int)
    plan.add_argument("--json", action="store_true", help="print one JSON document")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    example_window, example_rate = _example()
    sources = {
        "window": Source.EXAMPLE if args.window is None else Source.SUPPLIED,
        "rate": Source.EXAMPLE if args.rate is None else Source.SUPPLIED,
        "rate_value": example_rate if args.rate is None else args.rate,
    }
    try:
        if not 1 <= args.instances <= 1024:
            raise PlanError(Refusal.INVALID_REQUEST)
        plan = planning.expand(
            planning.load_suite(args.suite),
            Profile(args.profile),
            args.students,
            input_seed=args.input_seed,
            schedule_seed=args.schedule_seed,
        )
        bookings = planning.schedule(plan, example_window if args.window is None else args.window)
    except PlanError as refused:
        print(f"refused: {refused.reason.value}", file=sys.stderr)
        return 2
    planned = document(args, plan, bookings, sources)
    print(json.dumps(planned, sort_keys=True) if args.json else render(planned))
    return 0


if __name__ == "__main__":
    sys.exit(main())
