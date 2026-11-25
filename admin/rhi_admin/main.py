"""The admin CLI: the roster, direct keys, revocation, the canary, the spikes' students, the pools'
suspended launches and the pools' sessions (system design, sections 5.4, 5.6, 6.4 and 11.3).

There is no public admin API: the admin runs this with their own AWS credentials, from the
repository, and it reaches the tables directly.

    env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main <command>

    roster add <github-id> <alias> [--limit N]                a student, 4 concurrent requests unless given
    roster list                                                 every entry
    roster set <github-id> --limit N                            a new limit, on the entry and the keys
    roster remove <github-id>                                   the entry, and every key of the student revoked
    key issue <github-id>                                       a key without a login, printed once
    key revoke <github-id>                                      every key of the student revoked
    canary                                                      the canary's entry, and a new key in rhi/canary-key
    spike-keys <students>                                       the spikes' students, and new keys in rhi/spike-keys
    pool status                                                 each pool, and whether its launches are suspended
    pool clear-suspension <pool>                                the mark launch_suspended cleared
    pool run                                                    one run of the scaler, now
    session start <instances> (--until T | --for D)             a pool booked, from now or from --from T
    session set [--instances N] [--from T] [--until T | --for D]
                                                                the pool's session changed: what is given, the rest kept
    session stop [--all]                                        the pool's session ended now, or every pool's
    session status                                              each pool's session and its instances
    privacy export <github-id>                                  everything kept of a student, as one JSON document
    privacy erase <github-id>                                   a student's entry, keys and usage rows deleted

The account and the region are the caller's, from the AWS SDK's own configuration. Each table is
found in the deployed access stack by its logical ID, which `tests/policy/stateful_logical_ids.json`
pins, so this file names no account, region or table. The course end, which a key and an entry
expire by, is read when a command needs it from the configuration in SSM, with the configuration
loader's source `ssm` (`rhi.config.read_ssm`, hence `src` on the path) and the caller's credentials:
the whole configuration is read and checked, and a configuration that does not load refuses the
command with the loader's own message, which names a key and never a value of AWS.

The commands `pool` reach the pools stack the same way, once one of them runs: the pool-state
table by its logical ID, and the scaler's function by the logical ID of the scaler's construct
(`tests/synth/test_scaler.py` holds it to the synthesized one), so this file names no function
either. The change they make, a suspension cleared, carries who made it, the caller's identity from
STS without its account, and when (`rhi_admin.pools`).

The commands `session` (`rhi_admin.session`) reach, once one of them runs, the same pool-state
table, where each pool's session is one item, and record who wrote it the same way. A session is
the one operating state: there is no switch beside it and no mode per pool, and the kill switch is
`session stop`. `start`, `set` and `stop` take `--pool`, and act on the configuration's default
pool without it. A time `T` is `HH:MM`, the next time the clock of the configuration's `time_zone`
shows it, or `YYYY-MM-DDTHH:MM` in that zone; a length `D` is a whole number followed by `m` or
`h`, as `90m` or `6h`. The zone, the default pool and `session_max_hours` are read from the
configuration in SSM, as the course end is, and a pool's `max_instances` from the scaler's
function, as the commands `pool` read the deployed pools. After a write the command asks the
scaler for a run; when one is under way it says so, and the next run is within a minute.

While the endpoint is parked the pools stack keeps its table and holds no scaler, so no pool is
deployed: `pool status` and `session status` print no pool's line, and a command that needs a pool
or the scaler is refused.

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
from botocore.exceptions import ClientError

from rhi.config import Config, ConfigError, read_ssm
from rhi_admin import keys as admin_keys
from rhi_admin import pools as admin_pools
from rhi_admin import privacy as admin_privacy
from rhi_admin import roster as admin_roster
from rhi_admin import session as admin_session
from rhi_admin.canary import CANARY_SECRET, run_canary
from rhi_admin.roster import AdminError
from rhi_admin.spikes import run_spike_keys
from rhi_common.records import Session
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
    sessions: Callable[[], admin_session.Sessions]


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

    def configuration() -> Config:
        try:
            return read_ssm(ssm)
        except ConfigError as error:
            raise AdminError(str(error)) from None

    def course_end() -> datetime.date:
        return configuration().course_end

    def pools() -> admin_pools.Pools:
        sts = session.client("sts")
        table = dynamodb.Table(physical_id(cloudformation, POOL_STATE_LOGICAL_ID, POOLS_STACK_NAME))
        try:
            scaler = physical_id(cloudformation, SCALER_FUNCTION_LOGICAL_ID, POOLS_STACK_NAME)
        except ClientError as error:
            # The stack is there, since its table was found: it holds no scaler, as a parked stage's.
            if error.response.get("Error", {}).get("Code") != "ValidationError":
                raise
            scaler = None
        return admin_pools.Pools(
            table=table,
            functions=session.client("lambda"),
            scaler=scaler,
            caller=lambda: admin_pools.identity_name(sts.get_caller_identity()["Arn"]),
        )

    def sessions() -> admin_session.Sessions:
        read = configuration()
        return admin_session.Sessions(
            pools=pools(),
            time_zone=read.time_zone,
            session_max_hours=read.session_max_hours,
            default_pool=read.default_pool,
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
        sessions=sessions,
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
    change = roster.add_parser("set", help="change a student's limit")
    change.add_argument("github_id", type=int)
    for command in (add, change):
        command.add_argument("--limit", type=int, help="concurrent requests (default 4)")
    roster.add_parser("list", help="list the roster")
    roster.add_parser("remove", help="remove a student and revoke their keys").add_argument("github_id", type=int)

    key = commands.add_parser("key", help="keys issued without a login").add_subparsers(dest="command", required=True)
    key.add_parser("issue", help="issue a key, printed once").add_argument("github_id", type=int)
    key.add_parser("revoke", help="revoke every key of a student").add_argument("github_id", type=int)

    commands.add_parser("canary", help=f"the canary's entry, and a new key in {CANARY_SECRET}")
    commands.add_parser(
        "spike-keys", help=f"the spikes' students, and new keys in {SPIKE_KEYS_SECRET}"
    ).add_argument("students", type=int, help="how many students")

    pool = commands.add_parser("pool", help="the pools' suspended launches, and the scaler").add_subparsers(
        dest="command", required=True
    )
    pool.add_parser("status", help="each pool, and whether the scaler has suspended its launches")
    pool.add_parser("clear-suspension", help="clear the mark launch_suspended of a pool").add_argument("pool")
    pool.add_parser("run", help="ask the scaler to run now")

    session = commands.add_parser("session", help="the pools' sessions: when each pool serves students").add_subparsers(
        dest="command", required=True
    )
    session.add_parser("status", help="each pool's session and what arrived of its instances")
    a_time = "HH:MM, the next time the configured zone's clock shows it, or YYYY-MM-DDTHH:MM"
    a_length = "a whole number followed by m or h, as 90m or 6h"
    start = session.add_parser("start", help="book a pool: a number of instances, to an end")
    start.add_argument("instances", type=int, help="from 1 to the pool's max_instances")
    change = session.add_parser("set", help="change a pool's session: what is given, the rest kept")
    change.add_argument("--instances", type=int, help="from 1 to the pool's max_instances")
    starts = ((start, f"the start, now unless given: {a_time}"), (change, f"a start that has not passed: {a_time}"))
    for command, start_help in starts:
        command.add_argument("--pool", help="the pool, the configuration's default pool unless given")
        command.add_argument("--from", dest="starts", metavar="T", help=start_help)
        end = command.add_mutually_exclusive_group(required=command is start)
        end.add_argument("--until", metavar="T", help=f"the end: {a_time}")
        end.add_argument("--for", dest="length", metavar="D", help=f"the end, as a length from the start: {a_length}")
    stop = session.add_parser("stop", help="end a pool's session now").add_mutually_exclusive_group()
    stop.add_argument("--pool", help="the pool, the configuration's default pool unless given")
    stop.add_argument("--all", action="store_true", help="every pool's session")

    privacy = commands.add_parser("privacy", help="one student's data").add_subparsers(dest="command", required=True)
    privacy.add_parser("export", help="export what is kept of a student, as JSON").add_argument("github_id", type=int)
    privacy.add_parser(
        "erase", help="delete a student's entry, keys and usage rows, and say what only retention removes"
    ).add_argument("github_id", type=int)
    return cli


def _pool_command(command: str, arguments: argparse.Namespace, pools: admin_pools.Pools, now: int, out: TextIO) -> None:
    if command == "status":
        print("pool\tlaunch_suspended", file=out)
        for state in admin_pools.status(pools):
            print(f"{state.pool}\t{str(state.launch_suspended).lower()}", file=out)
    elif command == "clear-suspension":
        who = admin_pools.clear_suspension(arguments.pool, pools=pools, now=now)
        print(f"The launches of the pool {arguments.pool} may resume at the scaler's next run, cleared by {who}.", file=out)
    else:
        for decision in admin_pools.run_scaler(pools=pools):
            print(f"{decision['pool']}\t{decision['desired']}\t{decision['reason']}", file=out)


def _booking(session: Session, sessions: admin_session.Sessions) -> str:
    """A session as a command says it: its instances, its start and its end in the configured zone, and who wrote it."""
    starts, ends = (admin_session.show_time(at, time_zone=sessions.time_zone) for at in (session.starts_at, session.ends_at))
    return f"{session.instances} instance(s) from {starts} to {ends} ({sessions.time_zone}), set by {session.set_by}"


def _apply(sessions: admin_session.Sessions, out: TextIO) -> None:
    """After a write: asks the scaler for a run, and says whether it ran or one was under way."""
    if admin_session.applied(sessions):
        print("The scaler has run: the change applies now.", file=out)
    else:
        print("The scaler is already running: its next run, within a minute, applies the change.", file=out)


def _pool_session_command(
    command: str, arguments: argparse.Namespace, sessions: admin_session.Sessions, now: int, out: TextIO
) -> None:
    """The commands on the pools' sessions: `status`, `start`, `set` and `stop`."""

    def time(text: str | None) -> int | None:
        return None if text is None else admin_session.read_time(text, time_zone=sessions.time_zone, now=now)

    def shown(at: int | None) -> str:
        return "-" if at is None else admin_session.show_time(at, time_zone=sessions.time_zone)

    if command == "status":
        print(
            "pool\tsession\tinstances\tstarts_at\tends_at\tset_by\tset_at"
            "\trequested\tasked\twaiting\twaiting_since\tstarting\tin_service\tcause",
            file=out,
        )
        for line in admin_session.supply(sessions, now=now):
            booked, note = line.session, line.note
            if booked is None:
                session = ["none", "-", "-", "-", "-", "-"]
            else:
                state = "active" if line.active else "booked"
                written = (booked.set_by, shown(booked.set_at))
                session = [state, str(booked.instances), shown(booked.starts_at), shown(booked.ends_at), *written]
            # The cause is one of the scaler's four words, never text of AWS.
            cause = "-" if note.launch_failure is None else note.launch_failure.value
            waiting = (note.waiting, shown(note.waiting_since))
            arrived = (note.requested, note.capacity_asked, *waiting, note.starting, note.in_service, cause)
            print("\t".join([line.pool, *session, *map(str, arrived)]), file=out)
        return
    if command == "stop":
        if arguments.all:
            stops = admin_session.stop_all(sessions=sessions, now=now)
        else:
            pool = admin_session.pool_of(arguments.pool, sessions)
            stops = iter([(pool, admin_session.stop(pool, sessions=sessions, now=now))])
        written = False
        try:
            for pool, session in stops:
                if session is None:
                    print(f"The pool {pool} has no session booked or active: nothing to stop.", file=out)
                else:
                    written = True
                    print(f"The session of the pool {pool} is stopped, by {session.set_by}.", file=out)
        finally:
            # A stop that was written is applied, also when a later pool's failed: that failure is
            # still the command's.
            if written:
                _apply(sessions, out)
        return
    else:
        pool = admin_session.pool_of(arguments.pool, sessions)
        starts_at, until = time(arguments.starts), time(arguments.until)
        length = None if arguments.length is None else admin_session.read_length(arguments.length)
        if command == "start":
            starts_at = now if starts_at is None else starts_at
            # The command line requires one of --until and --for. A length counts from the start,
            # as the longest a session may last does.
            ends_at = starts_at + (length or 0) if until is None else until
            session = admin_session.start(
                pool, instances=arguments.instances, starts_at=starts_at, ends_at=ends_at, sessions=sessions, now=now
            )
            print(f"The pool {pool} is booked: {_booking(session, sessions)}.", file=out)
        else:
            if arguments.instances is None and starts_at is None and until is None and length is None:
                raise AdminError("nothing to change: give --instances, --from, --until or --for")
            session = admin_session.change(
                pool,
                sessions=sessions,
                now=now,
                instances=arguments.instances,
                starts_at=starts_at,
                ends_at=until,
                length=length,
            )
            print(f"The session of the pool {pool} is changed: {_booking(session, sessions)}.", file=out)
    _apply(sessions, out)


def _dispatch(arguments: argparse.Namespace, admin: Admin, now: int, out: TextIO) -> None:
    command = (arguments.group, getattr(arguments, "command", None))
    if arguments.group == "pool":
        _pool_command(arguments.command, arguments, admin.pools(), now, out)
    elif arguments.group == "session":
        _pool_session_command(arguments.command, arguments, admin.sessions(), now, out)
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
            course_end=admin.course_end(),
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
            concurrency_limit=arguments.limit,
        )
        print(f"Added {entry.github_id} as {entry.alias}: {entry.concurrency_limit} concurrent requests.", file=out)
    elif command == ("roster", "list"):
        print("github_id\talias\tconcurrency_limit", file=out)
        for entry in admin_roster.entries(roster=admin.roster):
            print(f"{entry.github_id}\t{entry.alias}\t{entry.concurrency_limit}", file=out)
    elif command == ("roster", "set"):
        changed = admin_roster.change(
            arguments.github_id,
            roster=admin.roster,
            keys=admin.keys,
            course_end=admin.course_end(),
            now=now,
            concurrency_limit=arguments.limit,
        )
        print(f"Changed the entry and {changed} active key(s); the gateway applies it within 60 seconds.", file=out)
    elif command == ("roster", "remove"):
        revoked = admin_roster.remove(
            arguments.github_id, roster=admin.roster, keys=admin.keys, course_end=admin.course_end(), now=now
        )
        print(f"Removed the entry and revoked {revoked} key(s).", file=out)
    elif command == ("key", "issue"):
        key = admin_keys.issue(
            arguments.github_id, roster=admin.roster, keys=admin.keys, course_end=admin.course_end(), now=now
        )
        # The one time the key is shown: it is stored as its SHA-256 only.
        print(key, file=out)
    elif command == ("key", "revoke"):
        revoked = admin_keys.revoke(arguments.github_id, keys=admin.keys, course_end=admin.course_end(), now=now)
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
            f"The spikes have new keys for {arguments.students} student(s), in the secret {SPIKE_KEYS_SECRET}.",
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
