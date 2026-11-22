"""The admin CLI: the roster, direct keys, revocation, the canary, the spikes' students and the
pools' operating state and the session switch (system design, sections 5.6, 6.4 and 11.3).

There is no public admin API: the admin runs this with their own AWS credentials, from the
repository, and it reaches the tables directly.

    env PYTHONPATH=admin:services/common:services/issuer uv run --locked --no-dev python -m rhi_admin.main <command>

    roster add <github-id> <alias> [--budget N] [--limit N]   a student, 100 million tokens and 4 unless given
    roster list                                                 every entry
    roster set <github-id> [--budget N] [--limit N]             a new budget or limit, on the entry and the keys
    roster remove <github-id>                                   the entry, and every key of the student revoked
    key issue <github-id>                                       a key without a login, printed once
    key revoke <github-id>                                      every key of the student revoked
    canary                                                      the canary's entry, and a new key in rhi/canary-key
    spike-keys <students>                                       the spikes' students, and new keys in rhi/spike-keys
    pool status                                                 every pool's mode, who set it and when, its launches
    pool disable <pool>                                         the mode disabled: the admin's kill switch
    pool enable <pool>                                          the pool's configured mode again
    pool clear-suspension <pool>                                the mark launch_suspended cleared
    pool run                                                    one run of the scaler, now
    session on                                                  the session on: the gateway serves students
    session off                                                 the session off: each pool falls to zero when idle
    session status                                              whether a session is on, who switched it and when
    privacy export <github-id>                                  everything kept of a student, as one JSON document
    privacy erase <github-id>                                   a student's entry, keys and usage rows deleted

The account and the region are the caller's, from the AWS SDK's own configuration. Each table is
found in the deployed access stack by its logical ID, which `tests/policy/stateful_logical_ids.json`
pins, so this file names no account, region or table. The course end, which a key and an entry
expire by, is read from the configuration's SSM parameter when a command needs it.

The commands `pool` reach the pools stack the same way, once one of them runs: the pool-state
table by its logical ID, and the scaler's function by the logical ID of the scaler's construct
(`tests/synth/test_scaler.py` holds it to the synthesized one), so this file names no function
either. Each change they make carries who made it, the caller's identity from STS without its
account, and when (`rhi_admin.pools`).

The commands `session` (`rhi_admin.session`) reach, once one of them runs, the same pool-state
table, where the session switch is one item, and record who switched it the same way.

The commands `privacy` (`rhi_admin.privacy`) reach, once one of them runs, the usage table by its
logical ID, the gateway's log group by its logical ID in the gateway stack, and the Prometheus
workspace by its logical ID in the observability stack, whose query address Managed Prometheus
gives. `tests/policy/stateful_logical_ids.json` pins the table's and the workspace's logical IDs,
and `tests/synth/test_gateway_service.py` holds the log group's to the synthesized one.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

import boto3

from rhi_admin import keys as admin_keys
from rhi_admin import pools as admin_pools
from rhi_admin import privacy as admin_privacy
from rhi_admin import roster as admin_roster
from rhi_admin import session as admin_session
from rhi_admin.canary import CANARY_SECRET, run_canary
from rhi_admin.roster import AdminError
from rhi_admin.spikes import run_spike_keys
from rhi_common.spike_keys import SPIKE_KEYS_SECRET

# The deployed stack that holds the students' tables, and their logical IDs in it.
STACK_NAME = "Rhi-Access"
KEYS_LOGICAL_ID = "TablesKeys6B5A3ABF"
ROSTER_LOGICAL_ID = "TablesRosterDAE02BAB"
USAGE_LOGICAL_ID = "TablesUsageA60E9525"
# The gateway's log group, in the gateway stack, and the metrics workspace, in the observability stack.
GATEWAY_STACK_NAME = "Rhi-Gateway"
GATEWAY_LOG_GROUP_LOGICAL_ID = "ServiceLogs205D48C6"
OBSERVABILITY_STACK_NAME = "Rhi-Observability"
WORKSPACE_LOGICAL_ID = "PrometheusWorkspaceF3098DB6"
# The deployed stack that holds the pools, the logical ID of the pool-state table in it, and that
# of the scaler's function.
POOLS_STACK_NAME = "Rhi-Pools"
POOL_STATE_LOGICAL_ID = "TablesPoolState68A872F7"
SCALER_FUNCTION_LOGICAL_ID = "ScalerFunction8891241E"
# The configuration's key `course_end`, under the prefix `SSM_PREFIX` of src/rhi/config.py.
COURSE_END_PARAMETER = "/rhi/config/course_end"


@dataclass(frozen=True)
class Admin:
    """What the commands act on: the two tables, a Secrets Manager client, the course end, and
    what the commands `pool`, `privacy` and `session` act on, each made when one of them runs."""

    roster: Any
    keys: Any
    secrets: Any
    course_end: Callable[[], datetime.date]
    pools: Callable[[], admin_pools.Pools]
    privacy: Callable[[], admin_privacy.Stores]
    switch: Callable[[], admin_session.Switch]


def physical_id(cloudformation: Any, logical_id: str, stack: str = STACK_NAME) -> str:
    """The name of the deployed resource with that logical ID in the stack, the access stack unless given."""
    answer = cloudformation.describe_stack_resource(StackName=stack, LogicalResourceId=logical_id)
    return answer["StackResourceDetail"]["PhysicalResourceId"]


def connect(session: Any = None, *, urlopen: Callable[..., Any] = urllib.request.urlopen) -> Admin:
    """The deployed tables and secrets, reached with the caller's own credentials and region.

    `urlopen` sends the workspace's signed queries."""
    if session is None:
        session = boto3.session.Session()
    cloudformation = session.client("cloudformation")
    dynamodb = session.resource("dynamodb")
    ssm = session.client("ssm")

    def course_end() -> datetime.date:
        value = ssm.get_parameter(Name=COURSE_END_PARAMETER)["Parameter"]["Value"]
        return datetime.date.fromisoformat(value)

    def pools() -> admin_pools.Pools:
        sts = session.client("sts")
        return admin_pools.Pools(
            table=dynamodb.Table(physical_id(cloudformation, POOL_STATE_LOGICAL_ID, POOLS_STACK_NAME)),
            functions=session.client("lambda"),
            scaler=physical_id(cloudformation, SCALER_FUNCTION_LOGICAL_ID, POOLS_STACK_NAME),
            caller=lambda: admin_pools.identity_name(sts.get_caller_identity()["Arn"]),
        )

    def switch() -> admin_session.Switch:
        sts = session.client("sts")
        return admin_session.Switch(
            table=dynamodb.Table(physical_id(cloudformation, POOL_STATE_LOGICAL_ID, POOLS_STACK_NAME)),
            caller=lambda: admin_pools.identity_name(sts.get_caller_identity()["Arn"]),
        )

    def privacy() -> admin_privacy.Stores:
        # A workspace's physical ID is its ARN, which ends with its ID.
        workspace = physical_id(cloudformation, WORKSPACE_LOGICAL_ID, OBSERVABILITY_STACK_NAME).rsplit("/", 1)[-1]
        endpoint = session.client("amp").describe_workspace(workspaceId=workspace)["workspace"]["prometheusEndpoint"]
        return admin_privacy.Stores(
            usage=dynamodb.Table(physical_id(cloudformation, USAGE_LOGICAL_ID)),
            logs=session.client("logs"),
            log_group=physical_id(cloudformation, GATEWAY_LOG_GROUP_LOGICAL_ID, GATEWAY_STACK_NAME),
            query=admin_privacy.workspace_query(session, endpoint, urlopen=urlopen),
        )

    return Admin(
        roster=dynamodb.Table(physical_id(cloudformation, ROSTER_LOGICAL_ID)),
        keys=dynamodb.Table(physical_id(cloudformation, KEYS_LOGICAL_ID)),
        secrets=session.client("secretsmanager"),
        course_end=course_end,
        pools=pools,
        privacy=privacy,
        switch=switch,
    )


def parser() -> argparse.ArgumentParser:
    """The admin CLI's command line, under its program name `rhi_admin`."""
    cli = argparse.ArgumentParser(prog="rhi_admin", description="The admin CLI of the inference endpoint.")
    commands = cli.add_subparsers(dest="group", required=True)

    roster = commands.add_parser("roster", help="the students who may log in").add_subparsers(
        dest="command", required=True
    )
    add = roster.add_parser("add", help="add a student")
    add.add_argument("github_id", type=int)
    add.add_argument("alias")
    change = roster.add_parser("set", help="change a student's budget or limit")
    change.add_argument("github_id", type=int)
    for command in (add, change):
        command.add_argument("--budget", type=int, help="daily token budget (default 100000000)")
        command.add_argument("--limit", type=int, help="concurrent requests (default 4)")
    roster.add_parser("list", help="list the roster")
    roster.add_parser("remove", help="remove a student and revoke their keys").add_argument("github_id", type=int)

    key = commands.add_parser("key", help="keys issued without a login").add_subparsers(dest="command", required=True)
    key.add_parser("issue", help="issue a key, printed once").add_argument("github_id", type=int)
    key.add_parser("revoke", help="revoke every key of a student").add_argument("github_id", type=int)

    commands.add_parser("canary", help=f"the canary's entry, and a new key in {CANARY_SECRET}")
    commands.add_parser(
        "spike-keys", help=f"the spikes' students, one of them with no budget, and new keys in {SPIKE_KEYS_SECRET}"
    ).add_argument("students", type=int, help="how many students with the default budget")

    pool = commands.add_parser("pool", help="the pools' operating state").add_subparsers(dest="command", required=True)
    pool.add_parser("status", help="every pool's mode, who set it and when, and its suspended launches")
    pool.add_parser("disable", help="disable a pool: the admin's kill switch").add_argument("pool")
    pool.add_parser("enable", help="set a pool back to its configured mode").add_argument("pool")
    pool.add_parser("clear-suspension", help="clear the mark launch_suspended of a pool").add_argument("pool")
    pool.add_parser("run", help="ask the scaler to run now")

    switch = commands.add_parser(
        "session", help="the session switch: whether the endpoint serves students"
    ).add_subparsers(dest="command", required=True)
    switch.add_parser("on", help="switch the session on: the gateway serves students")
    switch.add_parser("off", help="switch the session off: each pool falls to zero by its idle rule")
    switch.add_parser("status", help="whether a session is on, who switched it and when")

    privacy = commands.add_parser("privacy", help="one student's data").add_subparsers(dest="command", required=True)
    privacy.add_parser("export", help="export what is kept of a student, as JSON").add_argument("github_id", type=int)
    privacy.add_parser(
        "erase", help="delete a student's entry, keys and usage rows, and say what only retention removes"
    ).add_argument("github_id", type=int)
    return cli


def _pool_command(command: str, arguments: argparse.Namespace, pools: admin_pools.Pools, now: int, out: TextIO) -> None:
    if command == "status":
        print("pool\tmode\tset_by\tset_at\tlaunch_suspended", file=out)
        for state in admin_pools.status(pools):
            set_by, set_at = ("configured", "-") if state.set_by is None else (state.set_by, str(state.set_at))
            print(f"{state.pool}\t{state.mode.value}\t{set_by}\t{set_at}\t{str(state.launch_suspended).lower()}", file=out)
    elif command in ("disable", "enable"):
        change = admin_pools.disable if command == "disable" else admin_pools.enable
        state = change(arguments.pool, pools=pools, now=now)
        print(f"The pool {state.pool} is {state.mode.value}, set by {state.set_by} at {state.set_at}.", file=out)
    elif command == "clear-suspension":
        who = admin_pools.clear_suspension(arguments.pool, pools=pools, now=now)
        print(f"The launches of the pool {arguments.pool} may resume at the scaler's next run, cleared by {who}.", file=out)
    else:
        for decision in admin_pools.run_scaler(pools=pools):
            print(f"{decision['pool']}\t{decision['desired']}\t{decision['reason']}", file=out)


def _session_command(command: str, switch: admin_session.Switch, now: int, out: TextIO) -> None:
    if command == "status":
        state = admin_session.status(switch)
        print("session\tset_by\tset_at", file=out)
        if state is None:
            print("off\t-\t-", file=out)
        else:
            print(f"{'on' if state.on else 'off'}\t{state.set_by}\t{state.set_at}", file=out)
        return
    state, written = admin_session.turn(command == "on", switch=switch, now=now)
    if state is None:
        print("The session is already off: it has never been switched on.", file=out)
    elif written:
        print(f"The session is {command}, switched by {state.set_by} at {state.set_at}.", file=out)
    else:
        print(f"The session is already {command}, switched by {state.set_by} at {state.set_at}.", file=out)


def _dispatch(arguments: argparse.Namespace, admin: Admin, now: int, out: TextIO) -> None:
    command = (arguments.group, getattr(arguments, "command", None))
    if arguments.group == "pool":
        _pool_command(arguments.command, arguments, admin.pools(), now, out)
    elif arguments.group == "session":
        _session_command(arguments.command, admin.switch(), now, out)
    elif command == ("privacy", "export"):
        document = admin_privacy.export(
            arguments.github_id, roster=admin.roster, keys=admin.keys, stores=admin.privacy(), now=now
        )
        print(json.dumps(document, indent=2, sort_keys=True), file=out)
    elif command == ("privacy", "erase"):
        erased = admin_privacy.erase(
            arguments.github_id,
            roster=admin.roster,
            keys=admin.keys,
            stores=admin.privacy(),
            now=now,
            say=lambda line: print(line, file=out, flush=True),
        )
        for line in admin_privacy.report(erased, arguments.github_id):
            print(line, file=out)
    elif command == ("roster", "add"):
        entry = admin_roster.add(
            arguments.github_id,
            arguments.alias,
            roster=admin.roster,
            course_end=admin.course_end(),
            now=now,
            daily_token_budget=arguments.budget,
            concurrency_limit=arguments.limit,
        )
        print(
            f"Added {entry.github_id} as {entry.alias}: {entry.daily_token_budget} tokens a day, "
            f"{entry.concurrency_limit} concurrent requests.",
            file=out,
        )
    elif command == ("roster", "list"):
        print("github_id\talias\tdaily_token_budget\tconcurrency_limit", file=out)
        for entry in admin_roster.entries(roster=admin.roster):
            print(f"{entry.github_id}\t{entry.alias}\t{entry.daily_token_budget}\t{entry.concurrency_limit}", file=out)
    elif command == ("roster", "set"):
        changed = admin_roster.change(
            arguments.github_id,
            roster=admin.roster,
            keys=admin.keys,
            now=now,
            daily_token_budget=arguments.budget,
            concurrency_limit=arguments.limit,
        )
        print(f"Changed the entry and {changed} active key(s); the gateway applies it within 60 seconds.", file=out)
    elif command == ("roster", "remove"):
        revoked = admin_roster.remove(arguments.github_id, roster=admin.roster, keys=admin.keys, now=now)
        print(f"Removed the entry and revoked {revoked} key(s).", file=out)
    elif command == ("key", "issue"):
        key = admin_keys.issue(
            arguments.github_id, roster=admin.roster, keys=admin.keys, course_end=admin.course_end(), now=now
        )
        # The one time the key is shown: it is stored as its SHA-256 only.
        print(key, file=out)
    elif command == ("key", "revoke"):
        revoked = admin_keys.revoke(arguments.github_id, keys=admin.keys, now=now)
        print(f"Revoked {revoked} key(s).", file=out)
    elif command == ("spike-keys", None):
        run_spike_keys(
            arguments.students,
            roster=admin.roster,
            keys=admin.keys,
            secrets=admin.secrets,
            course_end=admin.course_end(),
            now=now,
        )
        # The count and the secret's name: the keys are in the secret and nowhere else.
        print(
            f"The spikes have new keys for {arguments.students} student(s) and for one with no budget, "
            f"in the secret {SPIKE_KEYS_SECRET}.",
            file=out,
        )
    else:
        run_canary(roster=admin.roster, keys=admin.keys, secrets=admin.secrets, course_end=admin.course_end(), now=now)
        print(f"The canary has a new key, in the secret {CANARY_SECRET}.", file=out)


def run(
    argv: Sequence[str],
    admin: Admin | Callable[[], Admin],
    *,
    now: int | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Runs one command; its exit status. `admin` is what it acts on, or what makes it once the arguments parse."""
    arguments = parser().parse_args(list(argv))
    target = admin if isinstance(admin, Admin) else admin()
    try:
        _dispatch(arguments, target, int(time.time()) if now is None else now, out or sys.stdout)
    except AdminError as error:
        print(f"Refused: {error}.", file=err or sys.stderr)
        return 1
    return 0


def main() -> int:
    return run(sys.argv[1:], connect)


if __name__ == "__main__":
    sys.exit(main())
