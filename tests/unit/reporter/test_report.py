"""The structured report of a failure, and its encoding as commit statuses (system design, 8.4).

The report is public like every status: these tests hold that a fact is made of allow-listed names
and enumerations only, that whatever else comes in leaves a count and nothing more, and that each
fact has a status of its own that GitHub accepts.
"""

import ast
import dataclasses
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import pytest

from rhi_reporter import report as module
from rhi_reporter.report import (
    FAILED_STATUSES,
    RESOURCE_EVENT_FIELDS,
    Report,
    ResourceStatus,
    build_report,
    statuses_of,
)
from rhi_reporter.status import DESCRIPTION_LIMIT, CommitStatus, State

SHA = "1a2b3c4d" * 5
# What the caller supplies: the template's logical IDs, the smoke tests' IDs, the lifecycle stages.
LOGICAL_IDS = ["GatewayService9F2C61B7", "KeysTable4D1E0A55", "FleetGroup77AB03CE"]
SMOKE_TEST_IDS = ["chat-completion", "key-refused"]
LIFECYCLE_STAGES = ["booted", "engine-started", "model-loaded"]


def build(
    event: Mapping[str, object],
    resource_events: Iterable[Mapping[str, object]] = (),
    failed_tests: Iterable[object] = (),
    lifecycle_stage: object = None,
) -> Report:
    """The report of a failure, with the lists above."""
    return build_report(
        event,
        resource_events,
        failed_tests,
        lifecycle_stage,
        logical_ids=LOGICAL_IDS,
        smoke_test_ids=SMOKE_TEST_IDS,
        lifecycle_stages=LIFECYCLE_STAGES,
    )


def failed(action: str | None = "Deploy", **more: object) -> dict[str, object]:
    """A failed action of the stage Prod, as the pipeline's event tells it."""
    return {"stage": "Prod", "action": action, "category": "Deploy", "state": "FAILED", "commit": SHA, **more}


def resource(logical_id: object, code: object, **more: object) -> dict[str, object]:
    """One of CloudFormation's events, under the names its API gives the fields."""
    return {"LogicalResourceId": logical_id, "ResourceStatus": code, "ResourceType": "AWS::ECS::Service", **more}


def written(report: Report) -> str:
    """Everything the report and its statuses hold, as one text."""
    return repr((dataclasses.astuple(report), [dataclasses.astuple(status) for status in statuses_of(report)]))


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


def test_a_recorded_failure_gives_one_status_per_fact() -> None:
    events = [
        resource("FleetGroup77AB03CE", "CREATE_COMPLETE"),
        resource("KeysTable4D1E0A55", "UPDATE_IN_PROGRESS"),
        resource("KeysTable4D1E0A55", "UPDATE_FAILED"),
        resource("GatewayService9F2C61B7", "CREATE_IN_PROGRESS"),
        resource("GatewayService9F2C61B7", "CREATE_FAILED"),
        resource("GatewayService9F2C61B7", "CREATE_FAILED"),
    ]
    report = build(failed(), events, ["key-refused"])
    assert report.dropped == 0
    assert statuses_of(report) == [
        CommitStatus(SHA, State.FAILURE, "deploy/Prod/action", "Prod Deploy"),
        CommitStatus(SHA, State.FAILURE, "deploy/Prod/resource/1", "GatewayService9F2C61B7 CREATE_FAILED"),
        CommitStatus(SHA, State.FAILURE, "deploy/Prod/resource/2", "KeysTable4D1E0A55 UPDATE_FAILED"),
        CommitStatus(SHA, State.FAILURE, "deploy/Prod/test/1", "key-refused"),
    ]
    # The verification's facts are under the verification's context, and the last lifecycle stage
    # an instance reached is one more fact.
    verification = build(failed("Verify"), [], ["chat-completion", "key-refused"], "engine-started")
    assert [(status.context, status.description) for status in statuses_of(verification)] == [
        ("verify/Prod/action", "Prod Verify"),
        ("verify/Prod/test/1", "chat-completion"),
        ("verify/Prod/test/2", "key-refused"),
        ("verify/Prod/lifecycle", "engine-started"),
    ]


def test_free_text_never_leaves() -> None:
    account = "".join(str(digit % 10) for digit in range(1, 13))
    arn = ":".join(("arn", "aws", "ecs", "eu-west-1", account, "service/gateway"))
    address = ".".join(str(part) for part in (10, 0, 12, 34))
    free_text = (account, arn, address)
    reason = f"Resource handler returned: {arn} is not reachable from {address} in account {account}"
    summary = {"external-execution-summary": reason, "error-code": address}
    event = failed() | {"execution-result": summary, "additionalInformation": reason, "account": account, arn: address}
    events = [
        resource("GatewayService9F2C61B7", "CREATE_FAILED", ResourceStatusReason=reason, PhysicalResourceId=arn),
        resource("KeysTable4D1E0A55", "UPDATE_COMPLETE", ResourceStatusReason=reason, StackId=arn),
        # The same text where a name or a code is expected.
        *(resource(text, "CREATE_FAILED", ResourceStatusReason=reason) for text in free_text),
        *(resource("KeysTable4D1E0A55", text, ResourceStatusReason=reason) for text in (*free_text, reason)),
    ]
    report = build(event, events, [*free_text, reason, "key-refused"], reason)
    assert report.resources == (("GatewayService9F2C61B7", ResourceStatus.CREATE_FAILED),)
    assert report.tests == ("key-refused",)
    assert report.dropped == 3 + 4 + 4 + 1
    assert not [text for text in free_text if text in written(report)]
    # A lifecycle stage that is such text leaves no more, nor does an action or a stage that is no name.
    for text in (*free_text, reason):
        assert not [value for value in free_text if value in written(build(failed(), [], [], text))]
    for text in (arn, reason):
        assert not [value for value in free_text if value in written(build(failed(action=text)))]
        with pytest.raises(ValueError, match="^the event's stage is not a stage name$"):
            build(failed(stage=text))


def test_a_name_outside_the_allow_lists_is_dropped() -> None:
    events = [resource("GatewayService9F2C61B7", "CREATE_FAILED"), resource("AuditBucket0C55D1F2", "CREATE_FAILED")]
    report = build(failed(), events, ["key-refused", "streaming-cut"])
    assert report.dropped == 2
    statuses = statuses_of(report)
    assert [status.context for status in statuses] == [
        "deploy/Prod/action",
        "deploy/Prod/resource/1",
        "deploy/Prod/test/1",
        "deploy/Prod/dropped",
    ]
    assert statuses[-1].description == "2"
    assert "AuditBucket" not in written(report)
    assert "streaming" not in written(report)
    # Nothing dropped, no such status.
    kept = build(failed(), events[:1], ["key-refused"])
    assert not [status for status in statuses_of(kept) if status.context.endswith("/dropped")]


def test_each_part_has_its_own_filter() -> None:
    # CloudFormation's codes: a failed one is reported, another member is passed over, anything
    # else is dropped and counted.
    assert {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "UPDATE_ROLLBACK_FAILED"} <= FAILED_STATUSES
    assert FAILED_STATUSES < set(ResourceStatus)
    for code in ResourceStatus:
        report = build(failed(), [resource("KeysTable4D1E0A55", str(code))])
        assert report.resources == ((("KeysTable4D1E0A55", code),) if code in FAILED_STATUSES else ())
        assert report.dropped == 0
    for code in ("create_failed", "CREATE_FAILED ", "FAILED", "", None, 7, ["CREATE_FAILED"]):
        report = build(failed(), [resource("KeysTable4D1E0A55", code)])
        assert (report.resources, report.dropped) == ((), 1), code
    # A name is kept as it is in its list, and only so.
    for name in ("keystable4d1e0a55", "KeysTable", "", None, 7, ["KeysTable4D1E0A55"]):
        report = build(failed(), [resource(name, "CREATE_FAILED")], [name], name)
        assert (report.resources, report.tests, report.lifecycle_stage) == ((), (), None), name
        assert report.dropped == (2 if name is None else 3), name
    # An action that is no action's name is dropped; the stage's own event has none.
    assert build(failed("Deploy to prod")).dropped == 1
    own = build(failed(None, category=None), [], [], "booted")
    assert (own.action, own.lifecycle_stage, own.dropped) == (None, "booted", 0)
    assert [status.context for status in statuses_of(own)] == ["deploy/Prod/lifecycle"]
    # Of one of CloudFormation's events, the logical ID and the code are all that is read.
    recorded = Recorded(resource("KeysTable4D1E0A55", "UPDATE_FAILED", ResourceStatusReason="a reason"))
    assert build(failed(), [recorded]).resources == (("KeysTable4D1E0A55", "UPDATE_FAILED"),)
    assert recorded.read == set(RESOURCE_EVENT_FIELDS) == {"LogicalResourceId", "ResourceStatus"}
    # A report is of a failure.
    for state in ("SUCCEEDED", "STARTED", "CANCELED", "failed", None):
        with pytest.raises(ValueError, match="^the event is not a failure$"):
            build(failed(state=state))


def test_every_fact_has_a_context_and_a_description_github_accepts() -> None:
    # Names that fill a description to its last character, and two that differ by case alone.
    code = ResourceStatus.UPDATE_ROLLBACK_FAILED
    room = DESCRIPTION_LIMIT - len(code) - 1
    whole_ids = ["Gateway" + "N" * (room - 7), "Queue1A", "QUEUE1A"]
    whole_test = "tests/smoke/test_chat.py::" + "t" * (DESCRIPTION_LIMIT - 26)
    stage, action = "Stage." + "s" * 94, "Action." + "a" * 32
    report = build_report(
        failed(action, stage=stage),
        [resource(name, str(code)) for name in whole_ids],
        [whole_test],
        "l" * DESCRIPTION_LIMIT,
        logical_ids=whole_ids,
        smoke_test_ids=[whole_test],
        lifecycle_stages=["l" * DESCRIPTION_LIMIT],
    )
    statuses = statuses_of(report)
    assert (report.dropped, len(statuses)) == (0, 6)
    assert {(status.commit, status.state) for status in statuses} == {(SHA, State.FAILURE)}
    # GitHub reads a context without regard to case.
    contexts = [status.context.lower() for status in statuses]
    assert len(set(contexts)) == len(contexts)
    assert all(context.startswith(f"deploy/{stage}/".lower()) for context in contexts)
    # Every name is whole in its description.
    descriptions = [status.description for status in statuses]
    assert sorted(descriptions) == sorted(
        [f"{stage} {action}", *(f"{name} {code}" for name in whole_ids), whole_test, "l" * DESCRIPTION_LIMIT]
    )
    assert max(len(description) for description in descriptions) == DESCRIPTION_LIMIT == 140


def test_a_fact_too_long_for_a_description_is_dropped_and_never_shortened() -> None:
    # Two names of each kind, in the caller's lists, that differ only at their start: a shortened
    # form would be the same for both. Each is a character or more past what its description holds.
    ids = [start + "Shared" * 30 for start in ("Alpha", "Bravo")]
    stage = "Stage." + "s" * 94
    for name in ids:
        lists = {"logical_ids": ids, "smoke_test_ids": ids, "lifecycle_stages": ids}
        report = build_report(failed(stage=stage), [resource(name, "CREATE_FAILED")], [name], name, **lists)
        assert (report.resources, report.tests, report.lifecycle_stage, report.dropped) == ((), (), None, 3)
        assert [(status.context, status.description) for status in statuses_of(report)] == [
            (f"deploy/{stage}/action", f"{stage} Deploy"),
            (f"deploy/{stage}/dropped", "3"),
        ]
        assert "Shared" not in written(report)
        # The stage and the action share one description.
        long_action = build(failed(name[:40], stage=stage))
        assert (long_action.action, long_action.dropped, len(statuses_of(long_action))) == (None, 1, 1)
        assert build(failed(name[:39], stage=stage)).action == name[:39]
    # The same name fits with one code and not with a longer one.
    name = "N" * (DESCRIPTION_LIMIT - len("CREATE_FAILED") - 1)
    events = [resource(name, "CREATE_FAILED"), resource(name, "UPDATE_ROLLBACK_FAILED")]
    report = build_report(failed(), events, logical_ids=[name], smoke_test_ids=[], lifecycle_stages=[])
    assert (report.resources, report.dropped) == (((name, ResourceStatus.CREATE_FAILED),), 1)
    # A report made by hand with a fact that does not fit is refused, not shortened.
    by_hand = dataclasses.replace(report, tests=("t" * (DESCRIPTION_LIMIT + 1),))
    with pytest.raises(ValueError, match="^a fact of the report does not fit in a description$"):
        statuses_of(by_hand)


def test_the_module_writes_nothing_but_statuses() -> None:
    tree = ast.parse(Path(module.__file__).read_text())
    imported = {
        alias.name if isinstance(node, ast.Import) else node.module
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    # No HTTP client, no AWS SDK and not the reporter's GitHub client: the module can call nothing.
    assert imported == {"re", "collections.abc", "dataclasses", "enum", "rhi_reporter.status"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not [name for name in names if "comment" in name.lower()]
    # What it returns is the status the reporter posts, which has no field for a body or a link.
    statuses = statuses_of(build(failed(), [resource("KeysTable4D1E0A55", "UPDATE_FAILED")], ["?"]))
    assert len(statuses) == 3
    assert all(type(status) is CommitStatus for status in statuses)
