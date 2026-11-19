"""The owner's message of a failure: what a public commit status may not carry (system design,
section 8.4).

A commit status holds a state, a fixed context and allow-listed parts only. The rest of a failure
is what a person needs to diagnose it, and goes to the owner by SNS, on the pipeline stack's topic
(`src/rhi/pipeline/notify.py`). This module is the pure part: a function from the facts of one
event to the message, or to none. It calls nothing.

**Which events give a message.** An action's event whose status ends the action without success:
the failure state (the action failed) or the error state (the action was stopped, abandoned, or
reported in a state the reporter does not know), as `rhi_reporter.status.status_for` gives them.
Any other status, and a stage's own event, gives none: the action's event has said it already.

**The message** holds, as AWS gives them:

- the commit, the pipeline's execution, and the failed stage and action, by their names;
- the action's status and its error details, from the action's latest run in that execution, as
  CodePipeline lists it (`ListActionExecutions`): the error's code and message, and the summary
  the action's provider wrote. The provider's link is left out: it is an address of the console;
- each failed resource of the stack the action deploys (a status code that ends in `_FAILED`),
  from CloudFormation's events of that stack since the action started: its logical ID, its status
  code and its status reason, oldest first.

Its subject is text of this repository, with the stage's and the action's names, which the
pipeline gives; none of it comes from AWS. The message goes to the owner only: neither it nor
anything in it is ever posted as a status or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rhi_reporter.status import State, status_for

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# SNS refuses a subject longer than this, and a message longer than `MESSAGE_LIMIT` bytes.
SUBJECT_LIMIT = 100
MESSAGE_LIMIT = 256 * 1024
# What ends a message that was cut to fit.
CUT = "\n[The message was cut to the size SNS accepts.]\n"
# What the message says when the pipeline's records could not be read.
UNREAD = "(could not be read)"

# The states of a status that end an action without success.
UNSUCCESSFUL = frozenset({State.FAILURE, State.ERROR})


@dataclass(frozen=True)
class Message:
    """One message to the owner's topic."""

    subject: str
    body: str


def message_for(
    facts: Mapping[str, object],
    *,
    execution: object = None,
    action_execution: Mapping[str, object] | None = None,
    resource_events: Iterable[Mapping[str, object]] = (),
    read: bool = True,
) -> Message | None:
    """The message of the action's event that `facts` tell, read as `status_for` reads it, with
    the stage the action belongs to. None unless it is an action's event whose status is a
    failure or an error. `action_execution` is
    the action's latest run as CodePipeline lists it, `resource_events` CloudFormation's events of
    the stack it deploys, and `read` False when the pipeline's records could not be read."""
    action = facts.get("action")
    if not isinstance(action, str):
        return None
    try:
        status = status_for(facts)
    except ValueError:
        return None
    if status.state not in UNSUCCESSFUL:
        return None
    stage = status.context.partition("/")[2]
    lines = [
        f"A pipeline action ended without success: {stage} {action}.",
        "",
        f"Commit: {status.commit}",
        f"Execution: {execution if isinstance(execution, str) else UNREAD}",
        f"Stage: {stage}",
        f"Action: {action}",
        f"Event state: {facts.get('state')}",
        "",
    ]
    lines += _action_lines(action_execution, read=read)
    lines += ["", *_resource_lines(resource_events, read=read)]
    subject = f"rhi pipeline: {status.state.value} of {stage} {action}"[:SUBJECT_LIMIT]
    return Message(subject=subject, body=_fit("\n".join(lines) + "\n"))


def _action_lines(detail: Mapping[str, object] | None, *, read: bool) -> list[str]:
    """The action's status and its error details, as CodePipeline gives them."""
    if detail is None:
        return [f"Action's error details: {UNREAD if not read else '(none recorded)'}"]
    output = detail.get("output")
    result = output.get("executionResult") if isinstance(output, dict) else None
    result = result if isinstance(result, dict) else {}
    error = result.get("errorDetails")
    error = error if isinstance(error, dict) else {}
    return [
        f"Action's status: {detail.get('status')}",
        f"Error code: {error.get('code')}",
        f"Error message: {error.get('message')}",
        f"Summary: {result.get('externalExecutionSummary')}",
    ]


def _resource_lines(events: Iterable[Mapping[str, object]], *, read: bool) -> list[str]:
    """Each failed resource's logical ID, status code and status reason, oldest first."""
    failed = [
        event
        for event in events
        if isinstance(event.get("ResourceStatus"), str) and str(event.get("ResourceStatus")).endswith("_FAILED")
    ]
    if not failed:
        return [f"Failed resources: {UNREAD if not read else '(none)'}"]
    lines = ["Failed resources:"]
    # CloudFormation lists a stack's events newest first.
    for event in reversed(failed):
        lines += [
            f"- {event.get('LogicalResourceId')} {event.get('ResourceStatus')}",
            f"  {event.get('ResourceStatusReason')}",
        ]
    return lines


def _fit(body: str) -> str:
    """The body, cut to the size SNS accepts when it is longer."""
    encoded = body.encode("utf-8")
    if len(encoded) <= MESSAGE_LIMIT:
        return body
    room = MESSAGE_LIMIT - len(CUT.encode("utf-8"))
    return encoded[:room].decode("utf-8", errors="ignore") + CUT
