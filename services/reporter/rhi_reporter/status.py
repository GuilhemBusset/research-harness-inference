"""The commit status a pipeline event becomes (system design, section 8.4).

Agents hold no AWS credentials, so the commit status is their only view of a deployment, and it is
public. `status_for` is a pure function from an event to a status: it calls nothing. A status
carries the commit it sits on, a state, a fixed context and a description, and no link: nothing
here can hold a console address. The description is one member of `Description`, alone, and never
text from AWS, whose messages hold bucket names, addresses and resource IDs.

An event is a mapping, and five of its keys are read, `EVENT_FIELDS`, whatever else it holds:

- `stage`: the stage the status is about, under the name the pipeline gives it. The steps that run
  before a stage's deployment (source, synthesis, self-update, assets) have no context of their
  own: whoever builds the event of such a failure names the stage whose deployment it prevented,
  and it reports under `deploy/<stage>` like every other event that is not the verification's;
- `action`: the action's name, or nothing for the stage's own event;
- `category`: the action's category, as CodePipeline names it. It is read for one thing, to tell a
  manual approval;
- `state`: the state CodePipeline gives the stage or the action, one of `PIPELINE_STATES`. Any
  other value gives the error state with one fixed description;
- `commit`: the full SHA of the commit the execution deploys.

The context is `verify/<stage>` for an event of the action named `Verify`, the stage's
post-deployment step, and `deploy/<stage>` for every other event. The rule is on the name, not on
the category or the position, so it holds whatever other steps a stage gains.

Only the stage's own success is the deployment's success: an action that succeeds leaves
`deploy/<stage>` pending, since other actions follow it. A reader that waits for a verdict
therefore never sees one before the stage has ended.

One description is more than a member: `superseded_description` gives the superseded member, a
space and the full SHA of the commit whose execution covers the superseded one. A SHA is a name
from this repository, not text from AWS, and the function checks that it is one. No event gives
that description: the execution-level state it answers has no stage, and names no covering commit.

**A verification that ran no test is not a verification that passed.** The verification step ends
in success without running a test while there is nothing to test, and leaves its reason
(`scripts/pipeline/verify.py`). No event carries that reason, so `status_for` cannot tell: whoever
holds it passes the status through `as_it_ran`, which gives a verification's success the
description of `not_run_description` in place of `VERIFIED_DESCRIPTION`: one member per known
reason, `NOT_RUN_REASONS`, and one for any other. The reason is only compared, never written.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

EVENT_FIELDS = ("stage", "action", "category", "state", "commit")

# The post-deployment step of each stage the pipeline deploys: the smoke tests name their step so.
VERIFY_ACTION = "Verify"
# CodePipeline's category for a manual approval.
APPROVAL_CATEGORY = "Approval"

DEPLOY_CONTEXT = "deploy/"
VERIFY_CONTEXT = "verify/"

# GitHub refuses a longer description.
DESCRIPTION_LIMIT = 140

_SHA = re.compile(r"[0-9a-fA-F]{40}")
# What CodePipeline accepts as a stage's name.
_STAGE = re.compile(r"[A-Za-z0-9.@_-]{1,100}")


class State(StrEnum):
    """The states GitHub accepts for a commit status."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


class Description(StrEnum):
    """Every description a status can carry. None is longer than `DESCRIPTION_LIMIT`, and the
    superseded member leaves room for the space and the SHA that follow it."""

    DEPLOYING = "The deployment is running."
    DEPLOYED = "The deployment succeeded."
    DEPLOYMENT_FAILED = "The deployment failed."
    APPROVAL_WAITING = "The deployment waits for the owner's approval in the pipeline."
    APPROVAL_REFUSED = "The approval was rejected, or it expired: the deployment did not continue."
    VERIFYING = "The verification is running."
    VERIFIED = "The verification's tests ran and passed."
    VERIFICATION_FAILED = "The verification failed."
    NOT_RUN_CANARY_MISSING = "No test ran: the canary's key is missing. The deployment is not verified."
    NOT_RUN_LISTENER_OFF = "No test ran: the endpoint has no HTTPS listener. The deployment is not verified."
    NOT_RUN_OTHER = "No test ran, for a reason the reporter does not know. The deployment is not verified."
    STOPPING = "The pipeline's execution is being stopped."
    STOPPED = "The pipeline's execution was stopped before this step finished."
    CANCELED = "The pipeline's execution was canceled before this step finished."
    UNKNOWN_STATE = "The pipeline reported a state the reporter does not know."
    SUPERSEDED = "Superseded: this commit is covered by the execution of commit"


# The two descriptions the implement loop reads by value, from its own configuration.
# A verification whose tests ran and passed: only a success of the action named Verify gives it.
VERIFIED_DESCRIPTION = Description.VERIFIED
# A manual approval that waits for a person: no other event gives it.
APPROVAL_DESCRIPTION = Description.APPROVAL_WAITING

# The reasons the verification step leaves when it runs no test (`scripts/pipeline/verify.py`),
# each with the description of a verification that ended in success so.
NOT_RUN_REASONS = {
    "canary-missing": Description.NOT_RUN_CANARY_MISSING,
    "listener-off": Description.NOT_RUN_LISTENER_OFF,
}


@dataclass(frozen=True)
class CommitStatus:
    """What is posted to GitHub, and all of it: there is no field for a link."""

    commit: str
    state: State
    context: str
    description: str


# An action of the stage that is neither its verification nor an approval. Its success is not the
# stage's. ABANDONED is what an action becomes when its execution is stopped and abandoned.
_ACTION: dict[str, tuple[State, Description]] = {
    "STARTED": (State.PENDING, Description.DEPLOYING),
    "RESUMED": (State.PENDING, Description.DEPLOYING),
    "SUCCEEDED": (State.PENDING, Description.DEPLOYING),
    "STOPPING": (State.PENDING, Description.STOPPING),
    "FAILED": (State.FAILURE, Description.DEPLOYMENT_FAILED),
    "CANCELED": (State.ERROR, Description.CANCELED),
    "STOPPED": (State.ERROR, Description.STOPPED),
    "ABANDONED": (State.ERROR, Description.STOPPED),
}
_STAGE_ITSELF = {**_ACTION, "SUCCEEDED": (State.SUCCESS, Description.DEPLOYED)}
# CodePipeline ends an approval that is rejected, or that nobody answers in seven days, as FAILED.
_APPROVAL = {
    **_ACTION,
    "STARTED": (State.PENDING, Description.APPROVAL_WAITING),
    "FAILED": (State.FAILURE, Description.APPROVAL_REFUSED),
}
_VERIFICATION = {
    **_ACTION,
    "STARTED": (State.PENDING, Description.VERIFYING),
    "RESUMED": (State.PENDING, Description.VERIFYING),
    "SUCCEEDED": (State.SUCCESS, Description.VERIFIED),
    "FAILED": (State.FAILURE, Description.VERIFICATION_FAILED),
}

# The states CodePipeline gives a stage or an action.
PIPELINE_STATES = frozenset(_ACTION)


def status_for(event: Mapping[str, object]) -> CommitStatus:
    """The one status of a pipeline event. ValueError, with nothing of the event in its message,
    when the event names no stage or no full commit SHA: such a status could be posted nowhere."""
    stage, action, category, state, commit = (event.get(field) for field in EVENT_FIELDS)
    if not isinstance(stage, str) or not _STAGE.fullmatch(stage):
        raise ValueError("the event's stage is not a stage name")
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise ValueError("the event's commit is not a full commit SHA")
    if action == VERIFY_ACTION:
        context, statuses = VERIFY_CONTEXT + stage, _VERIFICATION
    elif category == APPROVAL_CATEGORY:
        context, statuses = DEPLOY_CONTEXT + stage, _APPROVAL
    elif action is None:
        context, statuses = DEPLOY_CONTEXT + stage, _STAGE_ITSELF
    else:
        context, statuses = DEPLOY_CONTEXT + stage, _ACTION
    unknown = (State.ERROR, Description.UNKNOWN_STATE)
    status, description = statuses.get(state, unknown) if isinstance(state, str) else unknown
    return CommitStatus(commit=commit, state=status, context=context, description=description)


def not_run_description(reason: object) -> Description:
    """The description of a verification that ended in success without running a test, for the
    reason its step left: the member of `NOT_RUN_REASONS` for a reason it lists, and one member for
    anything else. None of them is `VERIFIED_DESCRIPTION`."""
    known = NOT_RUN_REASONS.get(reason) if isinstance(reason, str) else None
    return Description.NOT_RUN_OTHER if known is None else known


def as_it_ran(status: CommitStatus, reason: object) -> CommitStatus:
    """`status` as its verification ran: when it says the verification's tests ran and passed,
    while the step left `reason` for running none, the same status with the description of that
    reason. Any other status, and any status while `reason` is None, is returned as it is."""
    if reason is None or status.description != VERIFIED_DESCRIPTION:
        return status
    return replace(status, description=not_run_description(reason))


def superseded_description(covering_commit: str) -> str:
    """The description of a superseded commit: the superseded member, a space, and the full SHA of
    the commit whose execution covers it. ValueError unless that is 40 hexadecimal characters."""
    if not isinstance(covering_commit, str) or not _SHA.fullmatch(covering_commit):
        raise ValueError("the covering commit is not a full commit SHA")
    return f"{Description.SUPERSEDED} {covering_commit}"
