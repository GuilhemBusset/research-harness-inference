"""The weekly GPU canary of `src/rhi/pipeline/canary.py` (system design, sections 8.2, step 6, and
14): a job of the pipeline's stack that runs the coursework pool's smoke tests against the deployed
endpoint, a schedule on Sundays at 04:00 UTC whose only target is that job, and a role that reads
the canary's secret, queries the workspace, reads the pools' release parameters, queries the
heartbeats table the verification queries, and sends the measurement event, and nothing else.

The tests read the app's own pipeline, built from the example configuration.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from rhi import config

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
CANARY = "rhi-gpu-canary"
WEEKLY = "cron(0 4 ? * SUN *)"
# Every action the job's role is allowed, and nothing else.
JOB_ACTIONS = {
    "secretsmanager:GetSecretValue",
    "aps:QueryMetrics",
    "ssm:GetParameter",
    "dynamodb:Query",
    "events:PutEvents",
    "codeconnections:GetConnection",
    "codeconnections:GetConnectionToken",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """A value of the template as text, `<>` in place of each pseudo parameter and `<Name>` or
    `<Name.Attribute>` in place of each resource."""
    if isinstance(value, str):
        return value
    ((function, argument),) = value.items()
    if function == "Ref":
        return "<>" if argument.startswith("AWS::") else f"<{argument}>"
    if function == "Fn::GetAtt":
        return f"<{'.'.join(argument)}>"
    assert function == "Fn::Join", f"a value built with {function}"
    return argument[0].join(text(part) for part in argument[1])


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource["Properties"]
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == kind
    }


def statements(template: dict[str, Any], role: str) -> list[dict[str, Any]]:
    """Every statement of the policies attached to a role, which holds none of its own."""
    assert "Policies" not in template["Resources"][role]["Properties"]
    assert "ManagedPolicyArns" not in template["Resources"][role]["Properties"]
    return [
        statement
        for policy in of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Roles"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]


@pytest.fixture(scope="module")
def pipeline(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return templates[PIPELINE_TEMPLATE]


@pytest.fixture(scope="module")
def project(pipeline: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The canary's project, by its name: there is one."""
    (found,) = [item for item in of_type(pipeline, "AWS::CodeBuild::Project").items() if item[1].get("Name") == CANARY]
    return found


def test_the_job_runs_the_canary_script_on_main_with_no_webhook(project: tuple[str, dict[str, Any]]) -> None:
    _, properties = project
    assert properties["Triggers"] == {"Webhook": False}
    assert properties["Source"]["Type"] == "GITHUB"
    assert properties["SourceVersion"] == "main"
    assert properties["Environment"]["PrivilegedMode"] is False
    build_spec = json.loads(properties["Source"]["BuildSpec"])
    commands = [command for phase in build_spec["phases"].values() for command in phase["commands"]]
    assert "uv run --locked python scripts/pipeline/canary_result.py" in commands
    assert not any("codepipeline" in command for command in commands)


def test_the_job_is_told_the_endpoint_and_the_release_parameters(
    project: tuple[str, dict[str, Any]],
) -> None:
    example = config.load({"RHI_CONFIG": "example"})
    _, properties = project
    told = {variable["Name"]: variable["Value"] for variable in properties["Environment"]["EnvironmentVariables"]}
    assert told["RHI_SMOKE_ENDPOINT"] == (example.domain if example.https_listener else "none")
    pools = {pool.name: f"/rhi/pools/{pool.name}/release" for pool in example.pools}
    assert json.loads(told["RHI_SMOKE_RELEASE_PARAMETERS"]) == pools
    assert told["RHI_MEASUREMENT_BUS"] == "rhi-measurements"


def test_one_weekly_schedule_whose_only_target_is_the_job(
    pipeline: dict[str, Any], project: tuple[str, dict[str, Any]]
) -> None:
    logical_id, _ = project
    targeting = [
        properties
        for properties in of_type(pipeline, "AWS::Events::Rule").values()
        if any(target["Arn"] == {"Fn::GetAtt": [logical_id, "Arn"]} for target in properties.get("Targets", []))
    ]
    (schedule,) = targeting
    assert schedule["Name"] == CANARY
    assert schedule["ScheduleExpression"] == WEEKLY
    assert schedule.get("State", "ENABLED") == "ENABLED"
    assert "EventPattern" not in schedule
    (target,) = schedule["Targets"]
    # The schedule's role may start a build of the job, and nothing else.
    role = target["RoleArn"]["Fn::GetAtt"][0]
    assert [(as_list(s["Action"]), text(s["Resource"])) for s in statements(pipeline, role)] == [
        (["codebuild:StartBuild"], f"<{logical_id}.Arn>")
    ]


def test_the_job_s_role_reads_the_canary_s_inputs_and_sends_the_measurement_only(
    pipeline: dict[str, Any], project: tuple[str, dict[str, Any]]
) -> None:
    _, properties = project
    role = properties["ServiceRole"]["Fn::GetAtt"][0]
    allowed = statements(pipeline, role)
    assert {action for statement in allowed for action in as_list(statement["Action"])} == JOB_ACTIONS
    assert all(statement["Effect"] == "Allow" and "NotAction" not in statement for statement in allowed)

    def resources_of(action: str) -> set[str]:
        return {
            text(resource)
            for statement in allowed
            if action in as_list(statement["Action"])
            for resource in as_list(statement["Resource"])
        }

    assert resources_of("secretsmanager:GetSecretValue") == {"arn:<>:secretsmanager:<>:<>:secret:rhi/canary-key-??????"}
    example = config.load({"RHI_CONFIG": "example"})
    assert resources_of("ssm:GetParameter") == {
        f"arn:<>:ssm:<>:<>:parameter/rhi/pools/{pool.name}/release" for pool in example.pools
    }
    assert resources_of("events:PutEvents") == {"arn:<>:events:<>:<>:event-bus/rhi-measurements"}
    # The workspace is queried, by the stage's tag, never listed or described: its address is in
    # the coursework pool's release parameter.
    (query,) = [statement for statement in allowed if "aps:QueryMetrics" in as_list(statement["Action"])]
    assert as_list(query["Action"]) == ["aps:QueryMetrics"]
    assert text(query["Resource"]) == "arn:<>:aps:<>:<>:workspace/*"
    assert query["Condition"] == {"StringEquals": {"aws:ResourceTag/rhi-stage": "Rhi"}}
    # The one table the verification's role queries too, the stage's heartbeats table, by the pools
    # stack's name and the table's logical ID, and in it the coursework pool's rows only.
    (heartbeats,) = [statement for statement in allowed if "dynamodb:Query" in as_list(statement["Action"])]
    assert as_list(heartbeats["Action"]) == ["dynamodb:Query"]
    (table,) = resources_of("dynamodb:Query")
    assert re.fullmatch(r"arn:<>:dynamodb:<>:<>:table/Rhi-Pools-TablesHeartbeats[0-9A-F]{8}-\*", table)
    assert table in queried_by_others(pipeline, role)
    assert heartbeats["Condition"] == {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["coursework"]}}


def queried_by_others(pipeline: dict[str, Any], role: str) -> set[str]:
    """Every table a policy of the pipeline's stack not attached to `role` may query."""
    return {
        text(resource)
        for policy in of_type(pipeline, "AWS::IAM::Policy").values()
        if {"Ref": role} not in policy["Roles"]
        for statement in policy["PolicyDocument"]["Statement"]
        if "dynamodb:Query" in as_list(statement["Action"])
        for resource in as_list(statement["Resource"])
    }
