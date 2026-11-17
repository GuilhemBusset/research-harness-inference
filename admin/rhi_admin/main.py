"""The admin CLI: the roster, direct keys, revocation and the canary (system design, section 6.4).

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

The account and the region are the caller's, from the AWS SDK's own configuration. Each table is
found in the deployed access stack by its logical ID, which `tests/policy/stateful_logical_ids.json`
pins, so this file names no account, region or table. The course end, which a key and an entry
expire by, is read from the configuration's SSM parameter when a command needs it.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

import boto3

from rhi_admin import keys as admin_keys
from rhi_admin import roster as admin_roster
from rhi_admin.canary import CANARY_SECRET, run_canary
from rhi_admin.roster import AdminError

# The deployed stack that holds the students' tables, and their logical IDs in it.
STACK_NAME = "Rhi-Access"
KEYS_LOGICAL_ID = "TablesKeys6B5A3ABF"
ROSTER_LOGICAL_ID = "TablesRosterDAE02BAB"
# The configuration's key `course_end`, under the prefix `SSM_PREFIX` of src/rhi/config.py.
COURSE_END_PARAMETER = "/rhi/config/course_end"


@dataclass(frozen=True)
class Admin:
    """What the commands act on: the two tables, a Secrets Manager client and the course end."""

    roster: Any
    keys: Any
    secrets: Any
    course_end: Callable[[], datetime.date]


def table_name(cloudformation: Any, logical_id: str) -> str:
    """The name of the deployed table with that logical ID in the access stack."""
    answer = cloudformation.describe_stack_resource(StackName=STACK_NAME, LogicalResourceId=logical_id)
    return answer["StackResourceDetail"]["PhysicalResourceId"]


def connect(session: Any = None) -> Admin:
    """The deployed tables and secrets, reached with the caller's own credentials and region."""
    if session is None:
        session = boto3.session.Session()
    cloudformation = session.client("cloudformation")
    dynamodb = session.resource("dynamodb")
    ssm = session.client("ssm")

    def course_end() -> datetime.date:
        value = ssm.get_parameter(Name=COURSE_END_PARAMETER)["Parameter"]["Value"]
        return datetime.date.fromisoformat(value)

    return Admin(
        roster=dynamodb.Table(table_name(cloudformation, ROSTER_LOGICAL_ID)),
        keys=dynamodb.Table(table_name(cloudformation, KEYS_LOGICAL_ID)),
        secrets=session.client("secretsmanager"),
        course_end=course_end,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rhi_admin", description="The admin CLI of the inference endpoint.")
    commands = parser.add_subparsers(dest="group", required=True)

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
    return parser


def _dispatch(arguments: argparse.Namespace, admin: Admin, now: int, out: TextIO) -> None:
    command = (arguments.group, getattr(arguments, "command", None))
    if command == ("roster", "add"):
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
    arguments = _parser().parse_args(list(argv))
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
