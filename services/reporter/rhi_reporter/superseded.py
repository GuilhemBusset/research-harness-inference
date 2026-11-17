"""Which commits a self-update of the pipeline left without an execution, and which execution
covers each (system design, section 8.4; evidence E20).

The pipeline runs queued, one execution per commit, and updates itself first. An update of the
pipeline cancels the executions still queued, and CodePipeline restarts on the latest commit of
`main`: the commits of the cancelled executions are never deployed on their own, and their
stages send no event. This module is the pure part of what the reporter does about it: functions
from the pipeline's executions, as `ListPipelineExecutions` gives them, to commit statuses. It
calls nothing.

**The decision.** An execution that ended `Cancelled` or `Superseded` (`SUPERSEDED_STATUSES`)
did not deploy its commit. Its commit is covered by the earliest execution that started after it,
names a commit, and did not end so itself: on `main`, which takes changes in order, that
execution deploys a commit that holds this one.

**The statuses.** Under `deploy/<stage>` and `verify/<stage>` of each stage, a superseded commit
gets:

- while the covering execution runs, a pending status whose description is the superseded member
  of the vocabulary, a space and the covering commit's full SHA (`superseded_description`);
- once it has ended (`FINAL_STATES`), that execution's final state, as `status_for` gives it.
  Under `deploy/<stage>` it is the execution's own: the stage's state when the execution ended
  so. Under `verify/<stage>` it is the state of the covering execution's verification, the
  action named `Verify`, when it ran to an end; otherwise the execution's state again, with the
  deployment's description, so that no status claims a verification that did not run.

No pending status is given once the covering execution has ended: it would hide the verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rhi_reporter.status import (
    DEPLOY_CONTEXT,
    VERIFY_ACTION,
    VERIFY_CONTEXT,
    CommitStatus,
    State,
    status_for,
    superseded_description,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# The statuses CodePipeline gives an execution that ended without deploying its commit: cancelled
# by an update of the pipeline in queued mode, or replaced by a later one.
SUPERSEDED_STATUSES = frozenset({"Cancelled", "Superseded"})
# The statuses of an execution that has ended with a verdict, in its API's spelling, each as the
# state a stage's own event would give it.
FINAL_STATES = {"Succeeded": "SUCCEEDED", "Failed": "FAILED", "Stopped": "STOPPED"}
# The statuses of an action that has ended, in its API's spelling, each as its event's state.
ACTION_FINAL_STATES = {"Succeeded": "SUCCEEDED", "Failed": "FAILED", "Abandoned": "ABANDONED"}

_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Execution:
    """One of the pipeline's executions: what the decision reads of it."""

    execution_id: str
    # As CodePipeline spells it: InProgress, Stopping, Succeeded, Failed, Stopped, Cancelled, ...
    status: str
    # When it started: anything that orders, a time as the SDK gives it.
    started: Any
    # The full SHA of the commit it deploys, None when it names none.
    commit: str | None


@dataclass(frozen=True)
class Cover:
    """A superseded commit, the execution that did not deploy it, and the one that covers it."""

    commit: str
    superseded: Execution
    covering: Execution
    # The full SHA of the commit the covering execution deploys.
    covering_commit: str

    @property
    def ended(self) -> bool:
        """Whether the covering execution has ended with a verdict."""
        return self.covering.status in FINAL_STATES


def execution_of(summary: Mapping[str, Any]) -> Execution | None:
    """An execution, from one summary of `ListPipelineExecutions`. None for a summary that lacks
    an identifier, a status or a start time."""
    execution_id, status, started = (summary.get(key) for key in ("pipelineExecutionId", "status", "startTime"))
    if not isinstance(execution_id, str) or not isinstance(status, str) or started is None:
        return None
    commits = [revision.get("revisionId") for revision in summary.get("sourceRevisions", [])]
    commit = next((sha for sha in commits if isinstance(sha, str) and _SHA.fullmatch(sha)), None)
    return Execution(execution_id=execution_id, status=status, started=started, commit=commit)


def covers(executions: Iterable[Execution]) -> list[Cover]:
    """Every superseded commit with the execution that covers it, oldest first. A superseded
    commit that no execution covers yet is left out: a later call finds its cover."""
    ordered = sorted(executions, key=lambda execution: execution.started)
    found = []
    for position, execution in enumerate(ordered):
        if execution.status not in SUPERSEDED_STATUSES or execution.commit is None:
            continue
        for later in ordered[position + 1 :]:
            if later.status not in SUPERSEDED_STATUSES and later.commit is not None:
                found.append(Cover(execution.commit, execution, later, later.commit))
                break
    return found


def statuses_for(cover: Cover, stage: str, verification: str | None = None) -> list[CommitStatus]:
    """The statuses of a superseded commit under the deployment and the verification contexts of
    `stage`. `verification` is the status of the covering execution's action named `Verify` in
    that stage, as `ListActionExecutions` spells it, or None when it did not run."""
    if not cover.ended:
        description = superseded_description(cover.covering_commit)
        return [
            CommitStatus(cover.commit, State.PENDING, context, description)
            for context in (DEPLOY_CONTEXT + stage, VERIFY_CONTEXT + stage)
        ]
    state = FINAL_STATES[cover.covering.status]
    deployed = _status(cover.commit, stage, None, state)
    verified = ACTION_FINAL_STATES.get(verification) if verification is not None else None
    if verified is not None:
        return [deployed, _status(cover.commit, stage, VERIFY_ACTION, verified)]
    return [deployed, CommitStatus(cover.commit, deployed.state, VERIFY_CONTEXT + stage, deployed.description)]


def _status(commit: str, stage: str, action: str | None, state: str) -> CommitStatus:
    return status_for({"stage": stage, "action": action, "state": state, "commit": commit})
