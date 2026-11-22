"""The key issuer in the access stack (system design, sections 2 and 6.4): an arm64 function with its
reserved concurrency, its log group /rhi/issuer, its environment, a role limited to the roster and
keys tables, and, only while the public load balancer has its listener, one rule for /auth/.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})
HANDLER = "rhi_issuer.handler.handler"
READ = {"dynamodb:BatchGetItem", "dynamodb:ConditionCheckItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"}


def access_template(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    (template,) = [template for path, template in templates.items() if Path(path).name.startswith("RhiAccess")]
    return template


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


def issuer_function(template: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (found,) = [item for item in of_type(template, "AWS::Lambda::Function").items() if item[1]["Handler"] == HANDLER]
    return found


def table_id(template: dict[str, Any], prefix: str) -> str:
    (found,) = [name for name in of_type(template, "AWS::DynamoDB::Table") if name.startswith(prefix)]
    return found


def references(value: object) -> set[str]:
    """The logical IDs a fragment of a template refers to, by Ref or Fn::GetAtt."""
    if isinstance(value, dict):
        found = {value["Ref"]} if isinstance(value.get("Ref"), str) else set()
        if "Fn::GetAtt" in value:
            found.add(value["Fn::GetAtt"][0])
        return found.union(*(references(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(references(item) for item in value))
    return set()


@pytest.fixture(scope="module")
def without_listener(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The access stack's template when the configuration leaves the HTTPS listener off."""
    outdir = tmp_path_factory.mktemp("off")
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(dataclasses.replace(EXAMPLE, https_listener=False), outdir=str(outdir), context=context)
    app.synth()
    return access_template(
        {path.as_posix(): json.loads(path.read_text(encoding="utf-8")) for path in outdir.rglob("*.template.json")}
    )


def test_the_function_runs_on_arm64_with_its_reserved_concurrency_and_log_group(
    templates: dict[str, dict[str, Any]],
) -> None:
    template = access_template(templates)
    _, function = issuer_function(template)
    assert function["Architectures"] == ["arm64"]
    assert EXAMPLE.issuer_reserved_concurrency == 5
    assert function["ReservedConcurrentExecutions"] == 5
    (log_id,) = references(function["LoggingConfig"]["LogGroup"])
    assert of_type(template, "AWS::Logs::LogGroup")[log_id]["LogGroupName"] == "/rhi/issuer"


def test_the_admin_s_erase_outwaits_a_login_in_flight(templates: dict[str, dict[str, Any]]) -> None:
    from rhi_admin import privacy

    _, function = issuer_function(access_template(templates))
    assert privacy.ISSUER_SECONDS > function["Timeout"]


def test_the_environment_comes_from_the_configuration_and_the_tables(templates: dict[str, dict[str, Any]]) -> None:
    template = access_template(templates)
    _, function = issuer_function(template)
    assert function["Environment"]["Variables"] == {
        # The example's invented client ID: no template synthesized from the example holds a real one.
        "RHI_CLIENT_ID": EXAMPLE.login_client_id,
        "RHI_COURSE_END": EXAMPLE.course_end.isoformat(),
        "RHI_ISSUANCES_PER_DAY": str(EXAMPLE.key_issuances_per_day),
        "RHI_ROSTER_TABLE": {"Ref": table_id(template, "TablesRoster")},
        "RHI_KEYS_TABLE": {"Ref": table_id(template, "TablesKeys")},
    }
    assert "example" in EXAMPLE.login_client_id


def test_one_rule_at_priority_20_forwards_auth_to_the_function(templates: dict[str, dict[str, Any]]) -> None:
    template = access_template(templates)
    function_id, _ = issuer_function(template)
    (group_id, group) = of_type(template, "AWS::ElasticLoadBalancingV2::TargetGroup").popitem()
    assert group["TargetType"] == "lambda"
    assert references(group["Targets"]) == {function_id}
    rules = [
        rule
        for template in templates.values()
        for rule in of_type(template, "AWS::ElasticLoadBalancingV2::ListenerRule").values()
        if group_id in references(rule["Actions"])
    ]
    (rule,) = rules
    assert rule["Priority"] == 20
    assert rule["Conditions"] == [{"Field": "path-pattern", "PathPatternConfig": {"Values": ["/auth/*"]}}]
    assert rule["Actions"] == [{"Type": "forward", "TargetGroupArn": {"Ref": group_id}}]


def test_only_a_load_balancer_of_the_account_may_invoke_the_function(templates: dict[str, dict[str, Any]]) -> None:
    template = access_template(templates)
    function_id, _ = issuer_function(template)
    (permission,) = [p for p in of_type(template, "AWS::Lambda::Permission").values() if function_id in references(p)]
    assert permission["Principal"] == "elasticloadbalancing.amazonaws.com"
    assert permission["Action"] == "lambda:InvokeFunction"
    assert permission["SourceAccount"] == {"Ref": "AWS::AccountId"}


def test_the_role_is_limited_to_the_roster_and_keys_tables(templates: dict[str, dict[str, Any]]) -> None:
    template = access_template(templates)
    _, function = issuer_function(template)
    (role_id,) = references(function["Role"])
    role = of_type(template, "AWS::IAM::Role")[role_id]
    assert "ManagedPolicyArns" not in role and "Policies" not in role
    statements = [
        statement
        for policy in of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in policy["Roles"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    roster, keys = table_id(template, "TablesRoster"), table_id(template, "TablesKeys")
    (log_id,) = references(function["LoggingConfig"]["LogGroup"])
    granted: set[str] = set()
    for statement in statements:
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        granted.update(actions)
        assert statement["Effect"] == "Allow"
        assert all(action.split(":")[0] in {"dynamodb", "logs"} for action in actions), actions
        if any(action.startswith("dynamodb:") for action in actions):
            assert references(statement["Resource"]) in ({roster}, {keys})
            if references(statement["Resource"]) == {roster}:
                assert set(actions) <= READ
        else:
            assert references(statement["Resource"]) == {log_id}
    tables = set().union(*(references(statement["Resource"]) for statement in statements)) - {log_id}
    assert tables == {roster, keys}
    assert {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"} <= granted


def test_without_the_listener_the_function_has_no_target_group_and_no_rule(without_listener: dict[str, Any]) -> None:
    issuer_function(without_listener)
    for kind in ("TargetGroup", "ListenerRule"):
        assert of_type(without_listener, f"AWS::ElasticLoadBalancingV2::{kind}") == {}
    assert of_type(without_listener, "AWS::Lambda::Permission") == {}
    assert of_type(without_listener, "AWS::Lambda::Url") == {}
