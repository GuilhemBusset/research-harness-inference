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

`run` prints its progress and the time left in the invocation, the issue's URL, the capacity and
quality verdicts with their limiting rows, the report's paths, the cleanup, and the publication:
`delivered` once GitHub confirmed every write, `pending` while the issue's creation or a comment
waits, `refused` for a comment the hygiene scan or a field check refused, `none` before anything
was written. **Its exit code is the verdict's**: 0 for a completed profile that meets its applicable
floors, at every load it measured; 1 for a measured miss of a completed profile; 2 for invalid,
incomplete or inconclusive evidence, an invocation that failed (a fault of the driver among
them) or was interrupted (whatever it measured: its verdict rows still show a miss), or a refusal. The publication changes no exit code:
`status` shows it, and `publish --resume` delivers what waits, which needs no booking and no GPU
and exits 0 once nothing is pending, 2 otherwise. `--for` bounds the whole invocation from its
start, the preflight, the issue and the wait for capacity included; the cells chosen keep the planner's allowance for that wait
out of the window, as `plan` prints the batches. The report prices the deployed pool's instance
type unless `--rate` is given. `status` reads a run without taking it, even while a driver runs it,
and `cancel` asks that driver to stop: it stops admissions and releases the booking it made.
`compare --issue` compares two runs of the same experiment issue, in the same repository, from their
local bundles and posts the comparison once on it. A malformed `--repository`, or an issue number
that is not one, is refused with 2 before anything is connected to.

    scripts/evaluate.sh driver
    scripts/evaluate.sh submit --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new
    scripts/evaluate.sh inspect --submission <submission-id>
    scripts/evaluate.sh cancel --socket --submission <submission-id>

The same runs, from a coding agent (`evaluate.driver`): the operator starts `driver`, which holds
the operator's AWS session and GitHub token and listens on an owner-only socket (`--socket`, or
`RHI_EVALUATE_SOCKET`, or one in the user's runtime directory). `submit` takes `run`'s fields,
saves them with a submission ID in its private `--outbox` and prints the ID before it sends them;
after a lost answer, the same command with `--submission <id>` gets the same run. The driver
admits a run only once the selected pool's limits and the experiment's destination are checked,
and answers its run ID and status; `inspect` shows it again, with the issue's URL once GitHub
confirmed it, and `cancel --socket` asks the driver to stop it. Each exits 0 with an answer and 2
with a refusal or no answer. The run goes on in the driver whatever the client does.
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
# Where the client keeps each submission before it sends it, beside the run journals.
OUTBOX = RUNS.parent / "submitted"
_ISSUE = re.compile(r"new|[1-9][0-9]{0,8}")
MINUTE_MS = 60_000
_DURATION = re.compile(r"([1-9][0-9]{0,4})([hm])")


class Source:
    """Where a value of the estimate comes from."""

    EXAMPLE = "example"
    SUPPLIED = "supplied"
    # The selected pool's deployed price of the instance type that served: what a live run reads.
    DEPLOYED = "deployed"


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


def _issue_number(text: str) -> int:
    found = _issue(text)
    if found is None:
        raise argparse.ArgumentTypeError("needs an issue number")
    return found


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
    compare.add_argument(
        "--issue", type=_issue_number, help="post the comparison of two runs of this experiment issue"
    )
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
    cancel.add_argument("run_id", nargs="?")
    cancel.add_argument(
        "--socket", nargs="?", const="", help="ask the operator's driver, through its socket (its default one if none is named)"
    )
    driver = commands.add_parser("driver", help="start the operator's driver: it runs the runs submitted to its socket")
    submit = commands.add_parser("submit", help="submit a run to the operator's driver")
    submit.add_argument("--suite", default=planning.DEFAULT_SUITE)
    submit.add_argument("--profile", choices=[profile.value for profile in Profile], required=True)
    submit.add_argument("--pool", required=True, help="the already-deployed pool to measure")
    submit.add_argument("--students", type=int, required=True, help="the class size")
    submit.add_argument("--instances", type=int, required=True, help="the hosts to book")
    submit.add_argument("--for", "--window", dest="window", type=_duration_ms, help="the longest booking, such as 3h")
    submit.add_argument("--session", choices=["existing"], help="measure in the session the operator booked")
    submit.add_argument("--issue", type=_issue, required=True, help="new, or the experiment issue's number")
    submit.add_argument("--question", help="the experiment's public question, for a new issue")
    submit.add_argument("--target", default="reference", help="the label of this configuration in a new issue")
    submit.add_argument("--input-seed", type=int)
    submit.add_argument("--schedule-seed", type=int)
    submit.add_argument("--rate", type=_rate, help="dollars a host-hour, for the cost estimates")
    submit.add_argument("--repository", help="the experiment's repository, owner/name")
    submit.add_argument("--submission", help="the submission's ID, to submit it again after a lost answer")
    submit.add_argument("--outbox", type=Path, default=OUTBOX, help="where the client keeps its submissions")
    inspect = commands.add_parser("inspect", help="show a run of the operator's driver")
    inspect.add_argument("run_id", nargs="?")
    for one in (cancel, inspect):
        one.add_argument("--submission", help="the run's submission ID")
    for one in (driver, submit, inspect):
        one.add_argument("--socket", help="the driver's socket (RHI_EVALUATE_SOCKET, or one in the user's runtime directory)")
    driver.add_argument("--runs", type=Path, default=RUNS, help="where the run journals are")
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
    from evaluate import controller, publisher

    chosen = controller.connect_live if connect is None else connect
    repository = getattr(args, "repository", None) or os.environ.get(REPOSITORY_VARIABLE)
    if args.command in ("run", "publish", "compare") and repository and not publisher.valid_repository(repository):
        # Refused before anything is connected to: no lookup, no network.
        print(f"refused: {controller.Refusal.INVALID_REQUEST.value}: the repository is not owner/name", file=sys.stderr)
        return 2
    supplied = None if getattr(args, "rate", None) is None else (args.rate, Source.SUPPLIED)
    example = (example_rate, Source.EXAMPLE)
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
            rate=supplied or example,
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
        rate=supplied,
        fallback_rate=example,
    )
    return controller.execute(request, connect=chosen, root=args.runs, out=sys.stdout)


def _shown(answer: dict[str, Any]) -> int:
    """Prints the driver's answer, one field a line: 0 for a run it shows, 2 for a refusal."""
    if not answer.get("ok"):
        detail = answer.get("detail")
        print(f"refused: {answer.get('refused')}" + ("" if detail is None else f": {detail}"), file=sys.stderr)
        return 2
    for key in ("submission", "run_id", "step", "issue", "ending", "exit", "publication", "cancel_requested", "running"):
        if answer.get(key) is not None:
            print(f"{key}: {json.dumps(answer[key])}")
    return 0


def _client(args: argparse.Namespace, example_rate: float, connect: Connect | None) -> int:
    """`driver`, `submit`, `inspect` and `cancel --socket`: the operator's driver, and the typed
    client of its socket, which holds no credential (`evaluate.driver`)."""
    from evaluate import controller, driver

    socket_path = Path(args.socket) if args.socket else driver.default_socket()
    if args.command == "driver":
        chosen = controller.connect_live if connect is None else connect
        served = driver.Driver(args.runs, socket_path, connect=chosen, fallback_rate=(example_rate, Source.EXAMPLE))
        print("driver: listening, stop it with Ctrl-C", flush=True)
        served.serve_forever()
        print("driver: stopped", flush=True)
        return 0
    if args.command == "submit":
        run = {
            "suite": args.suite,
            "profile": args.profile,
            "pool": args.pool,
            "students": args.students,
            "instances": args.instances,
            "window_ms": args.window,
            "session": args.session,
            "issue": "new" if args.issue is None else args.issue,
            "repository": args.repository or os.environ.get(REPOSITORY_VARIABLE),
            "question": args.question,
            "target": args.target,
            "input_seed": args.input_seed,
            "schedule_seed": args.schedule_seed,
            "rate": args.rate,
        }
        try:
            submission = driver.persist_submission(args.outbox, args.submission, run)
        except driver.DriverError as refused:
            print(f"refused: {refused}", file=sys.stderr)
            return 2
        # Printed before anything is sent: an answer that is lost leaves the ID to submit again.
        print(f"submission: {submission}", flush=True)
        message: dict[str, Any] = {"op": "submit", "submission": submission, "run": run}
    else:
        if (args.run_id is None) == (args.submission is None):
            print("refused: name the run or its --submission", file=sys.stderr)
            return 2
        named = {"run_id": args.run_id} if args.submission is None else {"submission": args.submission}
        message = {"op": args.command, **named}
    try:
        return _shown(driver.call(socket_path, message))
    except driver.Unanswered as lost:
        again = f", submit it again with --submission {message['submission']}" if args.command == "submit" else ""
        print(f"unanswered: the driver did not answer ({lost}){again}", file=sys.stderr)
        return 2


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
    if args.command in ("driver", "submit", "inspect") or (args.command == "cancel" and args.socket is not None):
        return _client(args, example_rate, connect)
    if args.command == "cancel" and args.run_id is None:
        print("refused: name the run", file=sys.stderr)
        return 2
    if args.command in ("run", "status", "cancel", "publish") or (args.command == "compare" and args.issue is not None):
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
