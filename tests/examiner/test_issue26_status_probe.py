"""Independent examination of issue 26's pure public-status mapping."""

import runpy
from collections.abc import Mapping
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from rhi_reporter.status import (
    APPROVAL_DESCRIPTION,
    PIPELINE_STATES,
    VERIFIED_DESCRIPTION,
    CommitStatus,
    Description,
    status_for,
    superseded_description,
)

SHA = "abcDEF01" * 5
KINDS = ((None, None), ("Apply", "Deploy"), ("Confirm", "Approval"), ("Verify", "Build"))
EXPECTED = {
    "STARTED": ("pending", "pending", "pending", "pending"),
    "RESUMED": ("pending", "pending", "pending", "pending"),
    "SUCCEEDED": ("success", "pending", "pending", "success"),
    "FAILED": ("failure", "failure", "failure", "failure"),
    "STOPPING": ("pending", "pending", "pending", "pending"),
    "STOPPED": ("error", "error", "error", "error"),
    "CANCELED": ("error", "error", "error", "error"),
    "ABANDONED": ("error", "error", "error", "error"),
}


def event(action, category, state, stage="Canary-1"):
    return dict(stage=stage, action=action, category=category, state=state, commit=SHA)


@pytest.mark.parametrize("state", EXPECTED)
def test_states_have_independently_expected_results(state):
    assert set(EXPECTED) == set(PIPELINE_STATES)
    for index, (action, category) in enumerate(KINDS):
        value = event(action, category, state)
        result = status_for(value)
        assert result == status_for(value)
        assert result.state == EXPECTED[state][index]
        assert result.commit == SHA
        assert result.context == ("verify/" if action == "Verify" else "deploy/") + "Canary-1"
        assert result.description in set(Description)


@pytest.mark.parametrize("state", ["unrecognized", "succeeded", None, [], {}, 12])
def test_unknown_states_have_one_fixed_error(state):
    for action, category in KINDS:
        result = status_for(event(action, category, state))
        assert result.state == "error"
        assert result.description == Description.UNKNOWN_STATE


def test_action_name_alone_selects_context_and_verified_success():
    for category in ("Build", "Test", "Deploy", None):
        result = status_for(event("Verify", category, "SUCCEEDED", "Prod.eu_1"))
        assert result.state == "success"
        assert result.context == "verify/Prod.eu_1"
        assert result.description == VERIFIED_DESCRIPTION
    for action in ("Source", "Synth", "SelfMutate", "Assets", "verify", "VerifyMore", None):
        result = status_for(event(action, "Build", "FAILED", "Prod.eu_1"))
        assert (result.state, result.context) == ("failure", "deploy/Prod.eu_1")


def test_manual_approval_has_exclusive_wait_and_failure_descriptions():
    waiting = status_for(event("Review", "Approval", "STARTED"))
    refused = status_for(event("Review", "Approval", "FAILED"))
    assert (waiting.state, waiting.context, waiting.description) == (
        "pending", "deploy/Canary-1", APPROVAL_DESCRIPTION
    )
    assert refused.state == "failure"
    assert refused.context == "deploy/Canary-1"
    assert refused.description != waiting.description
    for state in EXPECTED:
        for action, category in KINDS:
            result = status_for(event(action, category, state))
            assert (result.description == APPROVAL_DESCRIPTION) == (
                category == "Approval" and state == "STARTED"
            )
            assert (result.description == refused.description) == (
                category == "Approval" and state == "FAILED"
            )
            assert (result.description == VERIFIED_DESCRIPTION) == (
                action == "Verify" and state == "SUCCEEDED"
            )


def test_vocabulary_and_only_superseded_sha_suffix():
    assert all(len(member.value) <= 140 for member in Description)
    for sha in ("0" * 40, "aBcDeF01" * 5):
        text = superseded_description(sha)
        assert text == Description.SUPERSEDED + " " + sha
        assert len(text) <= 140
    for invalid in ("", "a" * 39, "a" * 41, "G" * 40, "a" * 40 + "\n", None, 40):
        with pytest.raises(ValueError):
            superseded_description(invalid)  # ty: ignore[invalid-argument-type] - invalid caller input on purpose


class FiveFieldsOnly(Mapping):
    def __init__(self, data):
        self.data = data
        self.read = set()

    def __getitem__(self, key):
        assert key in {"stage", "action", "category", "state", "commit"}
        self.read.add(key)
        return self.data[key]

    def __iter__(self):
        raise AssertionError("the mapper must not enumerate event fields")

    def __len__(self):
        raise AssertionError("the mapper must not inspect unrelated event fields")


def test_unrelated_aws_fields_are_never_read_or_published():
    account = "".join(map(str, range(1, 10))) + "012"
    arn = ":".join(("arn", "aws", "s3", "", account, "example-bucket"))
    bucket = "example-artifacts-" + account
    assert {field.name for field in fields(CommitStatus)} == {"commit", "state", "context", "description"}
    for state in (*EXPECTED, "unexpected"):
        for action, category in KINDS:
            data = event(action, category, state)
            data.update(account=account, resource=arn, error=bucket, target_url="https://example.com")
            restricted = FiveFieldsOnly(data)
            result = status_for(restricted)
            assert restricted.read == {"stage", "action", "category", "state", "commit"}
            serialized = str(asdict(result))
            assert all(secret not in serialized for secret in (account, arn, bucket, "https://"))


def test_required_unit_tests_exercise_the_real_mapping():
    unit = Path("tests/unit/reporter/test_status.py")
    namespace = runpy.run_path(str(unit))
    for name in (
        "test_every_accepted_state_maps_to_one_status",
        "test_the_action_named_verify_reports_under_verify",
        "test_nothing_from_aws_is_copied",
        "test_an_approval_has_descriptions_of_its_own",
    ):
        namespace[name]()
