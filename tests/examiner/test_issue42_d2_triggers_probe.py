"""Independent witnesses for the pipeline's trigger and approval rules."""

import importlib.util
from pathlib import Path

import pytest


def load_policy():
    path = Path(__file__).resolve().parents[1] / "policy" / "test_pipeline.py"
    spec = importlib.util.spec_from_file_location("issue42_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cdk(monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    import aws_cdk

    return aws_cdk


@pytest.mark.parametrize("scheduler", [False, True], ids=["event-rule", "scheduler"])
def test_other_stack_cannot_start_pipeline_on_a_schedule(cdk, tmp_path, scheduler):
    policy = load_policy()

    def change(cdk, pipeline_stack, pipeline):
        other = cdk.Stack(pipeline_stack.node.scope, "ScheduledTrigger")
        boundary = cdk.aws_iam.ManagedPolicy.from_managed_policy_name(other, "Boundary", policy.BOUNDARY)
        cdk.aws_iam.PermissionsBoundary.of(other).apply(boundary)
        if scheduler:
            target = cdk.aws_scheduler_targets.CodePipelineStartPipelineExecution(pipeline)
            cdk.aws_scheduler.Schedule(
                other, "Nightly", schedule=cdk.aws_scheduler.ScheduleExpression.rate(cdk.Duration.days(1)), target=target
            )
        else:
            cdk.aws_events.Rule(
                other,
                "Nightly",
                schedule=cdk.aws_events.Schedule.rate(cdk.Duration.days(1)),
                targets=[cdk.aws_events_targets.CodePipeline(pipeline)],
            )

    templates = policy.built(cdk, tmp_path, change=change)
    assert policy.differences(templates, policy.BUILT) == []
    assert policy.ANOTHER_TRIGGER in policy.refused(templates)


