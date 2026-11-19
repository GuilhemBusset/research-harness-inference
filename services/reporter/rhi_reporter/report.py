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
- the last lifecycle stage each instance reached, which must be in `lifecycle_stages`.

A fact is published whole or not at all. One whose description would be longer than
`DESCRIPTION_LIMIT` fails a last filter: a shortened name would stand for every name that shares
what is left of it, and no reader could tell which one failed.

Whatever fails a filter is dropped and counted: `Report.dropped` is the only trace of it. The
report holds nothing else of it, not its length and not its kind.

`statuses_of` encodes a report, one status per fact, all in the failure state. A fact's context is
the context of the failed event's own status (`deploy/<stage>`, or `verify/<stage>` for the action
named Verify), a slash and a suffix:

- `action`: the description is the stage, a space and the action;
- `resource/<n>`: the logical ID, a space and the status code;
- `test/<n>`: the smoke test's ID;
- `lifecycle/<n>`: a lifecycle stage that an instance reached last;
- `dropped`: the count, only when something was dropped.

The resources, the tests and the lifecycle stages are numbered from 1 in the report's order, which
is sorted, so the same failure gives the same statuses. A stage is named once, whatever number of
instances stopped at it. The names are in the descriptions and not in the contexts:
GitHub reads a context without regard to case, and two names can differ by case alone.

No description is longer than `DESCRIPTION_LIMIT`, and every name in one is whole: `build_report`
has dropped and counted the facts that do not fit, and `statuses_of` refuses a report that holds
one.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from rhi_reporter.status import DESCRIPTION_LIMIT, CommitStatus, State, status_for

# The keys read of one of CloudFormation's events, under the names its API gives them.
RESOURCE_EVENT_FIELDS = ("LogicalResourceId", "ResourceStatus")

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
    """The facts of one failure. Every name in it passed its filter, and every fact fits in a
    description."""

    commit: str
    # The context of the failed event's own status: every fact's context starts with it.
    context: str
    stage: str
    action: str | None
    # Each failed resource's logical ID with its status code, sorted, each pair once.
    resources: tuple[tuple[str, ResourceStatus], ...]
    # The failed smoke tests' IDs, sorted, each once.
    tests: tuple[str, ...]
    # The last lifecycle stages the instances reached, sorted, each once.
    last_stages: tuple[str, ...]
    # How many values failed their filter.
    dropped: int


def _action_fact(stage: str, action: str) -> str:
    """The description of the failed stage and action."""
    return f"{stage} {action}"


def _resource_fact(logical_id: str, code: str) -> str:
    """The description of a failed resource."""
    return f"{logical_id} {code}"


def _fits(description: str) -> bool:
    """Whether GitHub accepts the description."""
    return len(description) <= DESCRIPTION_LIMIT


def build_report(
    event: Mapping[str, object],
    resource_events: Iterable[Mapping[str, object]] = (),
    failed_tests: Iterable[object] = (),
    last_stages: Iterable[object] = (),
    *,
    logical_ids: Iterable[str],
    smoke_test_ids: Iterable[str],
    lifecycle_stages: Iterable[str],
) -> Report:
    """The report of the failure the pipeline's event `event` tells. `last_stages` holds the last
    lifecycle stage each instance reached, one value per instance. ValueError, with nothing of the
    event in its message, when the event has no status (`status_for`) or is not a failure."""
    status = status_for(event)
    if status.state is not State.FAILURE:
        raise ValueError("the event is not a failure")
    stage = status.context.partition("/")[2]
    dropped = 0
    action = event.get("action")
    if not (isinstance(action, str) and _ACTION.fullmatch(action) and _fits(_action_fact(stage, action))):
        action, dropped = None, dropped + (action is not None)

    known_ids = frozenset(logical_ids)
    resources: set[tuple[str, ResourceStatus]] = set()
    for resource_event in resource_events:
        logical_id, code = (resource_event.get(field) for field in RESOURCE_EVENT_FIELDS)
        if not isinstance(code, str) or code not in ResourceStatus:
            dropped += 1
        elif code in FAILED_STATUSES:
            if isinstance(logical_id, str) and logical_id in known_ids and _fits(_resource_fact(logical_id, code)):
                resources.add((logical_id, ResourceStatus(code)))
            else:
                dropped += 1

    known_tests = frozenset(smoke_test_ids)
    tests: set[str] = set()
    for test_id in failed_tests:
        if isinstance(test_id, str) and test_id in known_tests and _fits(test_id):
            tests.add(test_id)
        else:
            dropped += 1

    known_stages = frozenset(lifecycle_stages)
    reached: set[str] = set()
    for stage_reached in last_stages:
        if isinstance(stage_reached, str) and stage_reached in known_stages and _fits(stage_reached):
            reached.add(stage_reached)
        else:
            dropped += 1

    return Report(
        commit=status.commit,
        context=status.context,
        stage=stage,
        action=action,
        resources=tuple(sorted(resources)),
        tests=tuple(sorted(tests)),
        last_stages=tuple(sorted(reached)),
        dropped=dropped,
    )


def statuses_of(report: Report) -> list[CommitStatus]:
    """The report as commit statuses on its commit: one per fact, each under its own context.
    ValueError, with nothing of the report in its message, when a fact does not fit in a
    description: `build_report` gives no such report."""
    facts: list[tuple[str, str]] = []
    if report.action is not None:
        facts.append(("action", _action_fact(report.stage, report.action)))
    for number, (logical_id, code) in enumerate(report.resources, start=1):
        facts.append((f"resource/{number}", _resource_fact(logical_id, code)))
    for number, test_id in enumerate(report.tests, start=1):
        facts.append((f"test/{number}", test_id))
    for number, stage_reached in enumerate(report.last_stages, start=1):
        facts.append((f"lifecycle/{number}", stage_reached))
    if report.dropped:
        facts.append(("dropped", str(report.dropped)))
    if not all(_fits(text) for _, text in facts):
        raise ValueError("a fact of the report does not fit in a description")
    return [
        CommitStatus(commit=report.commit, state=State.FAILURE, context=f"{report.context}/{suffix}", description=text)
        for suffix, text in facts
    ]
