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
out, it is `session_max_hours` of `config.example.toml`. The price is the booked time at `--rate`,
in dollars a host-hour, rounded once, to the cent. Left out, the rate is the example
configuration's price of its default pool. Each is labelled `supplied` or `example`: no real rate or session limit is in the
repository, and the deployed ones are read by `run`, not here.

**The commands.** The batch commands name `run`, and leave the pool and the experiment issue to
the operator's shell (`$pool`, `$experiment_issue`): neither is known offline.

It exits 0 with a plan and 2 with a refusal, whose reason is one of `plan.Refusal`.

    scripts/evaluate.sh report .cache/eval/<run>
    scripts/evaluate.sh compare .cache/eval/reference .cache/eval/candidate
    scripts/evaluate.sh export .cache/eval/<run> --to docs/evaluations/<name>

The three read saved bundles offline (`evaluate.report`), with no credentials and no network.
`report` regenerates a bundle's `summary.json`, `report.md` and `summary.sha256` in the bundle and
prints the report. `compare` prints the treatment diff of two bundles first, then their comparison
scorecard; it writes nothing. `export` writes a validated, scanned copy of a bundle as a new
directory under `docs/evaluations/`, for review in a pull request. The host price of the cost
estimates is `--rate`; without it `report` and `export` keep the price of the bundle's saved
summary, and otherwise use the example configuration's, labelled as `plan` labels it. `--json`
prints the summary or the comparison as one JSON document, the comparison's diff first. Each exits
0 when it read, compared or wrote, and 2 with a refusal, whose reason is one of `report.Refusal`.

    scripts/evaluate.sh run --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new
    scripts/evaluate.sh run --profile screen --pool coursework --students 8 --instances 1 \
        --session existing --issue "$experiment_issue"
    scripts/evaluate.sh run --resume [<run-id>] <the same request> --for 90m --issue "$experiment_issue"
    scripts/evaluate.sh status <run-id>
    scripts/evaluate.sh cancel <run-id>
    scripts/evaluate.sh publish --resume <run-id>
    scripts/evaluate.sh compare --issue "$experiment_issue" <reference-run-id> <candidate-run-id>

These go live, through the operator controller (`evaluate.controller`): `run` is one bounded
invocation (preflight, the experiment's issue, the booking, the cells, the cleanup, the local report
and the terminal comment, in that order), scoped to the selected, already-deployed pool and to the
experiment's repository (`--repository`, or `RHI_EVALUATE_REPOSITORY`). `--for` bounds a managed
booking; `--session existing` uses the operator's own booking and leaves its lifetime to them.
`--resume` continues a run, named by its ID or found by its issue and the same request: its
measured cells are kept, its interrupted ones run again. The run journals are under `--runs`
(`.cache/eval/runs` by default, outside the repository's history).

`run` prints its progress and the time left in the booking, the issue's URL, the capacity and
quality verdicts with their limiting rows, the report's paths, the cleanup, and whether the terminal
comment was delivered or is pending. **Its exit code is the verdict's**: 0 for a completed profile
that meets its applicable floors, 1 for a measured miss, 2 for invalid, incomplete or inconclusive
evidence, or a refusal. A comment that GitHub did not take changes no exit code: it stays pending,
`status` shows it, and `publish --resume` delivers it, which needs no booking and no GPU and exits
0 once nothing is pending, 2 otherwise. `status` reads a run without taking it, even while a driver
runs it, and `cancel` asks that driver to stop: it stops admissions and releases the booking it made.
`compare --issue` compares two runs of the issue from their local bundles and posts the comparison
once on it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evaluate import plan as planning
from evaluate.contracts import Profile
from evaluate.plan import Plan, PlanError, Refusal, Schedule

if TYPE_CHECKING:
    from evaluate.controller import Connect

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config.example.toml"
# Where the run journals are kept: the scratch directory the repository ignores.
RUNS = Path(__file__).resolve().parents[2] / ".cache" / "eval" / "runs"
# The experiment's repository, `owner/name`, when `--repository` does not give it.
REPOSITORY_VARIABLE = "RHI_EVALUATE_REPOSITORY"
_ISSUE = re.compile(r"new|[1-9][0-9]{0,8}")
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


def _issue(text: str) -> int | None:
    if not _ISSUE.fullmatch(text):
        raise argparse.ArgumentTypeError("needs new or an issue number")
    return None if text == "new" else int(text)


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
            "estimate_usd": bookings.price_usd(args.instances, sources["rate_value"]),
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
    read = commands.add_parser("report", help="regenerate the scorecard of a saved bundle, offline")
    read.add_argument("bundle", type=Path)
    compare = commands.add_parser(
        "compare", help="compare a candidate bundle with a reference, offline, or two runs of an issue on it"
    )
    compare.add_argument("reference", help="a bundle, or with --issue the reference's run ID")
    compare.add_argument("candidate", help="a bundle, or with --issue the candidate's run ID")
    compare.add_argument("--issue", type=int, help="post the comparison of two runs of this experiment issue")
    export = commands.add_parser("export", help="write a validated public copy of a bundle under docs/evaluations/")
    export.add_argument("bundle", type=Path)
    export.add_argument("--to", dest="destination", type=Path, required=True)
    for one in (read, compare, export):
        one.add_argument("--rate", type=_rate, help="dollars a host-hour, for the cost estimates")
    for one in (read, compare):
        one.add_argument("--json", action="store_true", help="print one JSON document")
    run = commands.add_parser("run", help="run a profile on the selected deployed pool and publish it on its issue")
    run.add_argument("--suite", default=planning.DEFAULT_SUITE)
    run.add_argument("--profile", choices=[profile.value for profile in Profile], required=True)
    run.add_argument("--pool", required=True, help="the already-deployed pool to measure")
    run.add_argument("--students", type=int, required=True, help="the class size")
    run.add_argument("--instances", type=int, required=True, help="the hosts to book")
    run.add_argument("--for", "--window", dest="window", type=_duration_ms, help="the longest booking, such as 3h")
    run.add_argument("--session", choices=["existing"], help="measure in the session the operator booked")
    run.add_argument("--issue", type=_issue, required=True, help="new, or the experiment issue's number")
    run.add_argument(
        "--resume", nargs="?", const=True, default=False, metavar="RUN_ID", help="continue a run of the issue"
    )
    run.add_argument("--question", help="the experiment's public question, for a new issue")
    run.add_argument("--target", default="reference", help="the label of this configuration in a new issue")
    run.add_argument("--input-seed", type=int)
    run.add_argument("--schedule-seed", type=int)
    run.add_argument("--rate", type=_rate, help="dollars a host-hour, for the cost estimates")
    status = commands.add_parser("status", help="show a run: its progress, verdicts, cleanup and publication")
    status.add_argument("run_id")
    cancel = commands.add_parser("cancel", help="ask the driver of a run to stop and release its booking")
    cancel.add_argument("run_id")
    publish = commands.add_parser("publish", help="deliver a run's pending issue and comments, and nothing else")
    publish.add_argument("--resume", dest="run_id", metavar="RUN_ID", required=True)
    for one in (run, status, cancel, publish, compare):
        one.add_argument("--runs", type=Path, default=RUNS, help="where the run journals are")
    for one in (run, publish, compare):
        one.add_argument("--repository", help="the experiment's repository, owner/name")
    return top


def _live(args: argparse.Namespace, example_rate: float, connect: Connect | None) -> int:
    """`run`, `status`, `cancel`, `publish --resume` and `compare --issue`: the operator
    controller, imported here, since it needs the pool's and the phases' modules."""
    from evaluate import controller

    chosen = controller.connect_live if connect is None else connect
    repository = getattr(args, "repository", None) or os.environ.get(REPOSITORY_VARIABLE)
    rate = (example_rate, Source.EXAMPLE) if getattr(args, "rate", None) is None else (args.rate, Source.SUPPLIED)
    if args.command == "status":
        return controller.status(args.runs, args.run_id, sys.stdout)
    if args.command == "cancel":
        return controller.cancel(args.runs, args.run_id, sys.stdout)
    if args.command == "publish":
        return controller.publish_resume(args.runs, args.run_id, connect=chosen, out=sys.stdout, repository=repository)
    if args.command == "compare":
        return controller.compare_issue(
            args.runs,
            args.issue,
            args.reference,
            args.candidate,
            connect=chosen,
            out=sys.stdout,
            rate=rate,
            repository=repository,
        )
    if not repository:
        print(f"refused: {controller.Refusal.INVALID_REQUEST.value}: no --repository", file=sys.stderr)
        return 2
    request = controller.Request(
        suite=args.suite,
        profile=Profile(args.profile),
        pool=args.pool,
        students=args.students,
        instances=args.instances,
        repository=repository,
        window_ms=args.window,
        existing=args.session == "existing",
        issue=args.issue,
        resume=args.resume,
        question=args.question,
        target=args.target,
        input_seed=args.input_seed,
        schedule_seed=args.schedule_seed,
        rate=rate,
    )
    return controller.execute(request, connect=chosen, root=args.runs, out=sys.stdout)


def _evidence(args: argparse.Namespace, example_rate: float) -> int:
    """`report`, `compare` and `export`: read saved bundles, offline. The reader is imported here:
    it needs `spikes` and the services `spikes/common.py` imports, which `plan` does not.

    The host price is `--rate` when given. Otherwise `report` and `export` keep the price the
    bundle's saved summary was made at, so that regenerating a bundle reproduces its digest, and a
    bundle with no summary yet, or a comparison, is priced at the example configuration's rate."""
    from evaluate import report

    supplied = None if args.rate is None else (args.rate, Source.SUPPLIED)
    example = (example_rate, Source.EXAMPLE)
    diff = None
    try:
        if args.command == "compare":
            manifests = report.read_manifest(Path(args.reference)), report.read_manifest(Path(args.candidate))
            # The diff comes first, whether or not the two compare, and before the rest is read.
            diff = report.treatment_diff(*manifests)
            if not args.json:
                print(report.render_diff(diff), flush=True)
            report.check_workloads(*manifests)
            reference, candidate = report.load(Path(args.reference)), report.load(Path(args.candidate))
            rate, source = supplied or example
            compared = report.compare(reference, candidate, usd_per_host_hour=rate, rate_source=source)
            print(report.dump_first(compared, "diff") if args.json else report.render_comparison(compared), end="")
        elif args.command == "report":
            bundle = report.load(args.bundle)
            rate, source = supplied or report.saved_price(bundle) or example
            derived = report.regenerate(bundle, usd_per_host_hour=rate, rate_source=source)
            report.write_derived(args.bundle, derived)
            print(derived.summary if args.json else derived.report, end="")
        else:
            rate, source = supplied or report.saved_price(report.load(args.bundle)) or example
            derived = report.export(args.bundle, args.destination, usd_per_host_hour=rate, rate_source=source)
            print(f"exported: {derived.digest}")
    except report.ReportError as refused:
        if args.command == "compare" and args.json and diff is not None:
            print(report.dump_first({"diff": diff, "refused": str(refused)}, "diff"), end="")
        # What was printed, the diff first, reaches a log both streams share before the refusal.
        sys.stdout.flush()
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None, *, connect: Connect | None = None) -> int:
    """The command line. `connect` opens the runtime of a live command for its scope; left out,
    it is the driver's live one (`controller.connect_live`)."""
    args = parser().parse_args(argv)
    example_window, example_rate = _example()
    if args.command in ("run", "status", "cancel", "publish") or (args.command == "compare" and args.issue):
        return _live(args, example_rate, connect)
    if args.command != "plan":
        return _evidence(args, example_rate)
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
