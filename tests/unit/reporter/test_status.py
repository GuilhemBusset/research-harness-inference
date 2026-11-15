"""A pipeline event becomes one commit status, from a fixed vocabulary (system design, section 8.4).

The status is the factory's only view of a deployment and it is public: these tests hold the
contexts and states the implement loop waits on, the two descriptions it reads by value, and that
nothing of an event but five fields can reach GitHub.
"""

import dataclasses
from collections.abc import Iterator, Mapping

import pytest

from rhi_reporter.status import (
    APPROVAL_DESCRIPTION,
    DESCRIPTION_LIMIT,
    EVENT_FIELDS,
    PIPELINE_STATES,
    VERIFIED_DESCRIPTION,
    CommitStatus,
    Description,
    State,
    status_for,
    superseded_description,
)

SHA = "1a2b3c4d" * 5
# The action and its category, for each kind of event a stage gives.
KINDS: dict[str, dict[str, object]] = {
    "stage": dict(action=None, category=None),
    "action": dict(action="Deploy", category="Deploy"),
    "approval": dict(action="Confirm", category="Approval"),
    "verify": dict(action="Verify", category="Build"),
}
SWEEP = [(kind, state) for kind in KINDS for state in sorted(PIPELINE_STATES)]
UNKNOWN_STATES = ("SUPERSEDED", "succeeded", "", None, 7)


def event(kind: str, state: object, **more: object) -> dict[str, object]:
    return {"stage": "Prod", "state": state, "commit": SHA, **KINDS[kind], **more}


def given_by(description: str) -> list[tuple[str, str]]:
    """The events of the sweep that give a description."""
    return [(kind, state) for kind, state in SWEEP if status_for(event(kind, state)).description == description]


class Recorded(Mapping[str, object]):
    """An event that records which of its keys are read."""

    def __init__(self, fields: dict[str, object]) -> None:
        self.fields = fields
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> object:
        self.read.add(key)
        return self.fields[key]

    def __iter__(self) -> Iterator[str]:
        self.read.add("every key")
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)


def test_every_accepted_state_maps_to_one_status() -> None:
    # What CodePipeline gives a stage or an action.
    documented = {"STARTED", "RESUMED", "SUCCEEDED", "FAILED", "CANCELED", "STOPPING", "STOPPED", "ABANDONED"}
    assert documented <= PIPELINE_STATES
    for kind, state in SWEEP:
        status = status_for(event(kind, state))
        assert status == status_for(event(kind, state))
        assert isinstance(status.state, State), (kind, state)
        assert isinstance(status.description, Description), (kind, state)
        assert status.commit == SHA
        assert status.context == ("verify/Prod" if kind == "verify" else "deploy/Prod")
        assert status.state == {"STARTED": State.PENDING, "FAILED": State.FAILURE}.get(state, status.state)
    # The deployment's success is the stage's own, and the verification's is its action's.
    successes = [(kind, state) for kind, state in SWEEP if status_for(event(kind, state)).state == State.SUCCESS]
    assert successes == [("stage", "SUCCEEDED"), ("verify", "SUCCEEDED")]
    assert status_for(event("stage", "SUCCEEDED")).context == "deploy/Prod"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", UNKNOWN_STATES)
def test_an_unknown_state_is_an_error_with_a_fixed_description(kind: str, state: object) -> None:
    status = status_for(event(kind, state))
    assert (status.state, status.description) == (State.ERROR, Description.UNKNOWN_STATE)
    assert status.context == ("verify/Prod" if kind == "verify" else "deploy/Prod")
    assert given_by(Description.UNKNOWN_STATE) == []


def test_the_action_named_verify_reports_under_verify() -> None:
    verified = CommitStatus(SHA, State.SUCCESS, "verify/Prod", VERIFIED_DESCRIPTION)
    assert status_for(event("verify", "SUCCEEDED")) == verified
    assert given_by(VERIFIED_DESCRIPTION) == [("verify", "SUCCEEDED")]
    # The rule is on the name: the category changes nothing, and no other name is the verification.
    assert status_for(event("verify", "SUCCEEDED", category="Test")) == verified
    for action in ("Deploy", "verify", "Verify2", "Prod.Verify", None):
        status = status_for(event("action", "SUCCEEDED", action=action, category="Build"))
        assert status.context == "deploy/Prod", action
        assert status.description != VERIFIED_DESCRIPTION
    # The stage's name is used as the pipeline gives it.
    assert status_for(event("verify", "STARTED", stage="Mock-eu.1")).context == "verify/Mock-eu.1"


@pytest.mark.parametrize(
    ("action", "category"), [("Source", "Source"), ("Synth", "Build"), ("SelfMutate", "Build"), ("FileAsset1", "Build")]
)
def test_a_failure_before_the_deployment_reports_under_deploy(action: str, category: str) -> None:
    status = status_for(event("action", "FAILED", action=action, category=category))
    assert (status.state, status.context) == (State.FAILURE, "deploy/Prod")


def test_a_description_is_one_member_and_a_status_has_no_link() -> None:
    assert all(len(member) <= DESCRIPTION_LIMIT <= 140 for member in Description)
    assert len(superseded_description(SHA)) <= DESCRIPTION_LIMIT
    assert len(set(Description)) == len(Description.__members__)
    assert [field.name for field in dataclasses.fields(CommitStatus)] == ["commit", "state", "context", "description"]
    assert given_by(Description.SUPERSEDED) == []


def test_nothing_from_aws_is_copied() -> None:
    account = "".join(str(digit % 10) for digit in range(1, 13))
    arn = ":".join(("arn", "aws", "codepipeline", "eu-west-1", account, "Pipeline"))
    bucket = f"pipeline-artifacts-{account}-eu-west-1"
    others = {
        "account": account,
        "resources": [arn],
        "pipeline": arn,
        "execution-result": {"external-execution-summary": f"Upload to {bucket} failed for {arn}"},
        "input-artifacts": [{"s3location": {"bucket": bucket}}],
        "additionalInformation": f"Access denied on {bucket}",
        bucket: arn,
    }
    for kind in KINDS:
        for state in (*sorted(PIPELINE_STATES), *UNKNOWN_STATES, account, arn, bucket):
            recorded = Recorded(event(kind, state, **others))
            status = status_for(recorded)
            assert recorded.read == set(EVENT_FIELDS) == {"stage", "action", "category", "state", "commit"}
            written = " ".join(str(value) for value in dataclasses.astuple(status))
            assert not [value for value in (account, arn, bucket) if value in written], (kind, state)


def test_an_approval_has_descriptions_of_its_own() -> None:
    waiting = status_for(event("approval", "STARTED"))
    assert waiting == CommitStatus(SHA, State.PENDING, "deploy/Prod", APPROVAL_DESCRIPTION)
    assert given_by(APPROVAL_DESCRIPTION) == [("approval", "STARTED")]

    rejected = status_for(event("approval", "FAILED"))
    assert (rejected.state, rejected.context) == (State.FAILURE, "deploy/Prod")
    assert given_by(rejected.description) == [("approval", "FAILED")]

    assert superseded_description(SHA) == f"{Description.SUPERSEDED} {SHA}"
    for after in (SHA[:39], SHA + "0", SHA[:7], "g" * 40, "", SHA + "\n", " " + SHA[1:], f"{SHA} {SHA}", None, 7):
        with pytest.raises(ValueError, match="not a full commit SHA"):
            superseded_description(after)  # ty: ignore[invalid-argument-type] - a caller's mistake, on purpose


@pytest.mark.parametrize(
    "wrong", [{"commit": SHA[:7]}, {"commit": None}, {"stage": None}, {"stage": ""}, {"stage": "a/b"}, {"stage": "a b"}]
)
def test_a_status_needs_a_stage_name_and_a_full_commit(wrong: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="^the event's (stage|commit) is not a "):
        status_for(event("action", "STARTED", **wrong))
