"""Independent probes for issue 27's pure failure report."""

import ast
import dataclasses
import inspect

import pytest

from rhi_reporter import report as module
from rhi_reporter.report import build_report, statuses_of
from rhi_reporter.status import CommitStatus, State

SHA = "ab" * 20


def event(action="Deploy", stage="Prod"):
    return dict(stage=stage, action=action, category="Deploy", state="FAILED", commit=SHA)


def resource(name, code="CREATE_FAILED", **extra):
    return dict(LogicalResourceId=name, ResourceStatus=code, **extra)


def build(*, pipeline=None, resources=(), tests=(), lifecycle=None,
          logical_ids=("Api", "Table"), smoke_ids=("chat",), stages=("booted",)):
    return build_report(
        event() if pipeline is None else pipeline,
        resources, tests, lifecycle,
        logical_ids=logical_ids, smoke_test_ids=smoke_ids, lifecycle_stages=stages,
    )


def test_recorded_failure_and_lifecycle_encoding():
    report = build(resources=[resource("Api"), resource("Table", "UPDATE_FAILED")], tests=["chat"])
    statuses = statuses_of(report)
    assert len(statuses) == 4
    assert {s.description for s in statuses} == {"Prod Deploy", "Api CREATE_FAILED", "Table UPDATE_FAILED", "chat"}
    assert {(s.commit, s.state) for s in statuses} == {(SHA, State.FAILURE)}
    assert len({s.context.casefold() for s in statuses}) == 4
    assert all(s.context.startswith("deploy/Prod/") and len(s.description) <= 140 for s in statuses)
    verification = statuses_of(build(pipeline=event("Verify"), tests=["chat"], lifecycle="booted"))
    assert {s.description for s in verification} == {"Prod Verify", "chat", "booted"}
    assert all(s.context.startswith("verify/Prod/") for s in verification)


def test_all_three_name_lists_and_status_enum_filter_inputs():
    report = build(
        resources=[resource("Api"), resource("Outside"), resource("Table", "UNRECOGNIZED_FAILED")],
        tests=["chat", "outside-test"], lifecycle="outside-stage",
    )
    assert report.resources == (("Api", "CREATE_FAILED"),)
    assert report.tests == ("chat",)
    assert report.lifecycle_stage is None
    assert report.dropped == 4
    encoded = repr(dataclasses.asdict(report)) + repr(statuses_of(report))
    assert all(value not in encoded for value in ("Outside", "UNRECOGNIZED_FAILED", "outside-test", "outside-stage"))
    counts = [s for s in statuses_of(report) if s.context.endswith("/dropped")]
    assert len(counts) == 1 and counts[0].description == "4"


def test_unknown_resource_and_smoke_names_are_only_counted():
    report = build(resources=[resource("UnlistedResource")], tests=["unlisted-test"])
    assert report.dropped == 2
    assert report.resources == () and report.tests == ()
    statuses = statuses_of(report)
    assert len(statuses) == 2
    assert any(s.context.endswith("/dropped") and s.description == "2" for s in statuses)
    assert "UnlistedResource" not in repr(statuses) and "unlisted-test" not in repr(statuses)


def test_reason_text_never_enters_the_report_or_statuses():
    account = "".join(str(n % 10) for n in range(1, 13))
    arn = ":".join(("arn", "aws", "ecs", "eu-west-1", account, "service/private"))
    address = ".".join(str(n) for n in (10, 20, 30, 40))
    reason = " ".join((account, arn, address))
    pipeline = event() | {"reason": reason, "execution-result": {"external-execution-summary": reason}}
    report = build(
        pipeline=pipeline,
        resources=[resource("Api", ResourceStatusReason=reason, PhysicalResourceId=arn)],
        tests=["chat"], lifecycle="booted",
    )
    encoded = repr(dataclasses.asdict(report)) + repr(statuses_of(report))
    assert all(value not in encoded for value in (account, arn, address))


def test_module_produces_only_status_values_and_has_no_comment_call():
    statuses = statuses_of(build(resources=[resource("Api")]))
    assert statuses and all(isinstance(s, CommitStatus) for s in statuses)
    calls = [node.func for node in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(node, ast.Call)]
    names = [getattr(call, "attr", getattr(call, "id", "")) for call in calls]
    assert not any("comment" in name.lower() for name in names)


@pytest.mark.parametrize("kind", ["resource", "test", "lifecycle", "action"])
def test_retained_names_are_identifiable_in_the_published_facts(kind):
    # These names are in the caller's lists. Distinct prefixes have the same long
    # suffix, so displaying only the suffix cannot identify which fact failed.
    names = [prefix + "Shared" * 30 for prefix in ("Alpha", "Bravo")]
    if kind == "action":
        names = [prefix + "a" * 90 for prefix in ("Alpha", "Bravo")]
    for name in names:
        if kind == "resource":
            report = build(resources=[resource(name)], logical_ids=names)
            retained = bool(report.resources)
        elif kind == "test":
            report = build(tests=[name], smoke_ids=names)
            retained = bool(report.tests)
        elif kind == "lifecycle":
            report = build(lifecycle=name, stages=names)
            retained = report.lifecycle_stage is not None
        else:
            report = build(pipeline=event(name, "S" * 100))
            retained = report.action is not None
        statuses = statuses_of(report)
        assert all(len(s.description) <= 140 for s in statuses)
        if retained:
            assert any(name in s.context or name in s.description for s in statuses), (
                "A retained fact lost its allow-listed name in the public encoding", kind,
            )
        else:
            assert report.dropped == 1
            assert any(s.context.endswith("/dropped") and s.description == "1" for s in statuses)
