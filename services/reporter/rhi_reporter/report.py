"""The structured report of a failure, as commit statuses (system design, section 8.4).

A red status alone does not tell the factory what to fix. The report adds the facts of the
failure, each as one more commit status on the same commit. This module is the pure part: a
function from events and allow-lists to statuses. It calls nothing and writes nothing: what it
returns are `CommitStatus` values, the one thing the reporter posts (ADR 0001, amendment 4). There
is no commit comment.

A fact is assembled only from allow-listed parts, and each part has one filter:

- the failed stage and action, from the pipeline's event, the one `rhi_reporter.status.status_for`
  reads. The stage is the name that event's own status carries. The action must be an action's
  name as CodePipeline accepts one. An event of the stage itself has no action, and gives no such
  fact;
- each failed resource's logical ID with its status code, from CloudFormation's events. Two keys
  of such an event are read, `RESOURCE_EVENT_FIELDS`, whatever else it holds: never the reason.
  The code must be a member of `ResourceStatus`, and the resource failed when the code is one of
  `FAILED_STATUSES`: an event with any other member is no failure and is passed over. The logical
  ID must be in `logical_ids`, the names of the templates synthesized from the same commit;
- each failed smoke test's ID, which must be in `smoke_test_ids`;
- the last lifecycle stage an instance reached, which must be in `lifecycle_stages`.

Whatever fails its filter is dropped and counted: `Report.dropped` is the only trace of it. The
report holds nothing else of it, not its length and not its kind.

`statuses_of` encodes a report, one status per fact, all in the failure state. A fact's context is
the context of the failed event's own status (`deploy/<stage>`, or `verify/<stage>` for the action
named Verify), a slash and a suffix:

- `action`: the description is the stage, a space and the action;
- `resource/<n>`: the logical ID, a space and the status code;
- `test/<n>`: the smoke test's ID;
- `lifecycle`: the lifecycle stage;
- `dropped`: the count, only when something was dropped.

The resources and the tests are numbered from 1 in the report's order, which is sorted, so the
same failure gives the same statuses. The names are in the descriptions and not in the contexts:
GitHub reads a context without regard to case, and two names can differ by case alone.

No description is longer than `DESCRIPTION_LIMIT`, and no fact is lost to that limit: a name too
long for its description keeps its end, after `SHORTENED`. The end is what tells such names apart:
a generated logical ID ends with the digest of its construct's path.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from rhi_reporter.status import DESCRIPTION_LIMIT, CommitStatus, State, status_for

# The keys read of one of CloudFormation's events, under the names its API gives them.
RESOURCE_EVENT_FIELDS = ("LogicalResourceId", "ResourceStatus")

# What stands for the start of a name that is too long for its description.
SHORTENED = "..."

# What CodePipeline accepts as an action's name.
_ACTION = re.compile(r"[A-Za-z0-9.@_-]{1,100}")


class ResourceStatus(StrEnum):
    """CloudFormation's resource status codes: the values its API documents for the status of a
    resource in a stack event. The list is public and fixed."""

    CREATE_IN_PROGRESS = "CREATE_IN_PROGRESS"
    CREATE_FAILED = "CREATE_FAILED"
    CREATE_COMPLETE = "CREATE_COMPLETE"
    DELETE_IN_PROGRESS = "DELETE_IN_PROGRESS"
    DELETE_FAILED = "DELETE_FAILED"
    DELETE_COMPLETE = "DELETE_COMPLETE"
    DELETE_SKIPPED = "DELETE_SKIPPED"
    UPDATE_IN_PROGRESS = "UPDATE_IN_PROGRESS"
    UPDATE_FAILED = "UPDATE_FAILED"
    UPDATE_COMPLETE = "UPDATE_COMPLETE"
    IMPORT_FAILED = "IMPORT_FAILED"
    IMPORT_COMPLETE = "IMPORT_COMPLETE"
    IMPORT_IN_PROGRESS = "IMPORT_IN_PROGRESS"
    IMPORT_ROLLBACK_IN_PROGRESS = "IMPORT_ROLLBACK_IN_PROGRESS"
    IMPORT_ROLLBACK_FAILED = "IMPORT_ROLLBACK_FAILED"
    IMPORT_ROLLBACK_COMPLETE = "IMPORT_ROLLBACK_COMPLETE"
    EXPORT_FAILED = "EXPORT_FAILED"
    EXPORT_COMPLETE = "EXPORT_COMPLETE"
    EXPORT_IN_PROGRESS = "EXPORT_IN_PROGRESS"
    EXPORT_ROLLBACK_IN_PROGRESS = "EXPORT_ROLLBACK_IN_PROGRESS"
    EXPORT_ROLLBACK_FAILED = "EXPORT_ROLLBACK_FAILED"
    EXPORT_ROLLBACK_COMPLETE = "EXPORT_ROLLBACK_COMPLETE"
    UPDATE_ROLLBACK_IN_PROGRESS = "UPDATE_ROLLBACK_IN_PROGRESS"
    UPDATE_ROLLBACK_COMPLETE = "UPDATE_ROLLBACK_COMPLETE"
    UPDATE_ROLLBACK_FAILED = "UPDATE_ROLLBACK_FAILED"
    ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
    ROLLBACK_COMPLETE = "ROLLBACK_COMPLETE"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


# The codes of a resource that failed.
FAILED_STATUSES = frozenset(code for code in ResourceStatus if code.endswith("_FAILED"))


@dataclass(frozen=True)
class Report:
    """The facts of one failure. Every name in it passed its filter."""

    commit: str
    # The context of the failed event's own status: every fact's context starts with it.
    context: str
    stage: str
    action: str | None
    # Each failed resource's logical ID with its status code, sorted, each pair once.
    resources: tuple[tuple[str, ResourceStatus], ...]
    # The failed smoke tests' IDs, sorted, each once.
    tests: tuple[str, ...]
    lifecycle_stage: str | None
    # How many values failed their filter.
    dropped: int


def build_report(
    event: Mapping[str, object],
    resource_events: Iterable[Mapping[str, object]] = (),
    failed_tests: Iterable[object] = (),
    lifecycle_stage: object = None,
    *,
    logical_ids: Iterable[str],
    smoke_test_ids: Iterable[str],
    lifecycle_stages: Iterable[str],
) -> Report:
    """The report of the failure the pipeline's event `event` tells. ValueError, with nothing of
    the event in its message, when the event has no status (`status_for`) or is not a failure."""
    status = status_for(event)
    if status.state is not State.FAILURE:
        raise ValueError("the event is not a failure")
    dropped = 0
    action = event.get("action")
    if not (isinstance(action, str) and _ACTION.fullmatch(action)):
        action, dropped = None, dropped + (action is not None)

    known_ids = frozenset(logical_ids)
    resources: set[tuple[str, ResourceStatus]] = set()
    for resource_event in resource_events:
        logical_id, code = (resource_event.get(field) for field in RESOURCE_EVENT_FIELDS)
        if not isinstance(code, str) or code not in ResourceStatus:
            dropped += 1
        elif code in FAILED_STATUSES:
            if isinstance(logical_id, str) and logical_id in known_ids:
                resources.add((logical_id, ResourceStatus(code)))
            else:
                dropped += 1

    known_tests = frozenset(smoke_test_ids)
    tests: set[str] = set()
    for test_id in failed_tests:
        if isinstance(test_id, str) and test_id in known_tests:
            tests.add(test_id)
        else:
            dropped += 1

    if not (isinstance(lifecycle_stage, str) and lifecycle_stage in frozenset(lifecycle_stages)):
        lifecycle_stage, dropped = None, dropped + (lifecycle_stage is not None)

    return Report(
        commit=status.commit,
        context=status.context,
        stage=status.context.partition("/")[2],
        action=action,
        resources=tuple(sorted(resources)),
        tests=tuple(sorted(tests)),
        lifecycle_stage=lifecycle_stage,
        dropped=dropped,
    )


def _fitted(name: str, room: int) -> str:
    """The name, or its end after `SHORTENED` when it is longer than `room` characters."""
    return name if len(name) <= room else SHORTENED + name[len(name) - room + len(SHORTENED) :]


def statuses_of(report: Report) -> list[CommitStatus]:
    """The report as commit statuses on its commit: one per fact, each under its own context."""
    facts: list[tuple[str, str]] = []
    if report.action is not None:
        action = _fitted(report.action, DESCRIPTION_LIMIT - len(report.stage) - 1)
        facts.append(("action", f"{report.stage} {action}"))
    for number, (logical_id, code) in enumerate(report.resources, start=1):
        facts.append((f"resource/{number}", f"{_fitted(logical_id, DESCRIPTION_LIMIT - len(code) - 1)} {code}"))
    for number, test_id in enumerate(report.tests, start=1):
        facts.append((f"test/{number}", _fitted(test_id, DESCRIPTION_LIMIT)))
    if report.lifecycle_stage is not None:
        facts.append(("lifecycle", _fitted(report.lifecycle_stage, DESCRIPTION_LIMIT)))
    if report.dropped:
        facts.append(("dropped", str(report.dropped)))
    return [
        CommitStatus(commit=report.commit, state=State.FAILURE, context=f"{report.context}/{suffix}", description=text)
        for suffix, text in facts
    ]
