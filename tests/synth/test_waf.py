"""The web ACL of the public load balancer (system design, section 6.5): three rate limits and the
AWS IP reputation list, no rule that reads a request's body (evidence E18), and no request sampling,
which would show the key headers in cleartext (evidence E26).

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})
REPUTATION = "AWSManagedRulesAmazonIpReputationList"
# The only managed rule group the ACL may hold: it reads the source address and nothing else.
ALLOWED_GROUPS = {REPUTATION}
KEY_HEADERS = {"authorization", "x-api-key"}  # section 6.4


def resources(templates: dict[str, dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r for t in templates.values() for r in t.get("Resources", {}).values() if r["Type"] == kind]


def body_inspection(acl: dict[str, Any]) -> list[str]:
    """Every part of the ACL's rules that reads a request's body, or could."""
    found: list[str] = []

    def walk(node: object, rule: str) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, rule)
        if not isinstance(node, dict):
            return
        group = node.get("ManagedRuleGroupStatement")
        if group is not None and group.get("Name") not in ALLOWED_GROUPS:
            found.append(f"{rule}: managed rule group {group.get('Name')}")
        for kind in ("RuleGroupReferenceStatement", "XssMatchStatement", "SqliMatchStatement"):
            if kind in node:
                found.append(f"{rule}: {kind}")
        field = node.get("FieldToMatch")
        if isinstance(field, dict) and {"Body", "JsonBody"} & field.keys():
            found.append(f"{rule}: a match on the body")
        for value in node.values():
            walk(value, rule)

    for rule in acl["Properties"].get("Rules", []):
        walk(rule["Statement"], rule["Name"])
    return found


def sampling_on(acl: dict[str, Any]) -> list[str]:
    """The ACL, or each of its rules, whose requests are sampled."""
    properties = acl["Properties"]
    parts = [("the ACL", properties["VisibilityConfig"])]
    parts += [(rule["Name"], rule["VisibilityConfig"]) for rule in properties.get("Rules", [])]
    return [name for name, visibility in parts if visibility["SampledRequestsEnabled"] is not False]


def unredacted(logging: dict[str, Any]) -> set[str]:
    """The key headers a logging configuration leaves in its logs."""
    redacted = logging["Properties"].get("RedactedFields", [])
    return KEY_HEADERS - {field["SingleHeader"]["Name"].lower() for field in redacted if "SingleHeader" in field}


def acl_of(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    (acl,) = resources(templates, "AWS::WAFv2::WebACL")
    return acl


def rate_rules(acl: dict[str, Any]) -> dict[str | None, int]:
    """The limit of each rate rule, by the path prefix it is scoped to (None: every request)."""
    limits: dict[str | None, int] = {}
    for rule in acl["Properties"]["Rules"]:
        rate = rule["Statement"].get("RateBasedStatement")
        if rate is None:
            continue
        assert (rate["AggregateKeyType"], rate["EvaluationWindowSec"]) == ("IP", 300)  # per address, 5 minutes
        assert rule["Action"] == {"Block": {}}
        prefix = None
        if "ScopeDownStatement" in rate:
            match = rate["ScopeDownStatement"]["ByteMatchStatement"]
            assert (match["FieldToMatch"], match["PositionalConstraint"]) == ({"UriPath": {}}, "STARTS_WITH")
            prefix = match["SearchString"]
        limits[prefix] = rate["Limit"]
    return limits


def built_acl(
    app_module: ModuleType, extra_rule: dict[str, Any] | None = None, *, logging: bool = False
) -> dict[str, Any]:
    """The template of a stack, built here, that holds the app's ACL, one more rule if given, and an
    unredacted logging configuration if asked."""
    del app_module  # started jsii
    import aws_cdk as cdk
    from aws_cdk import assertions
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from aws_cdk import aws_wafv2 as wafv2

    from rhi.network.waf import WebAcl

    stack = cdk.Stack(cdk.App(), "Built")
    balancer = elbv2.ApplicationLoadBalancer.from_application_load_balancer_attributes(
        stack,
        "Balancer",
        # The stack's own account and region, as tokens: no ARN is written out.
        load_balancer_arn=stack.format_arn(service="elasticloadbalancing", resource="loadbalancer", resource_name="app/b/0"),
        security_group_id="security-group",
    )
    acl = WebAcl(stack, "Waf", load_balancer=balancer, rate_limits=EXAMPLE.rate_limits).web_acl
    if extra_rule is not None:
        acl.add_property_override("Rules.4", extra_rule)  # after the four rules of the construct
    if logging:
        wafv2.CfnLoggingConfiguration(stack, "Logs", resource_arn=acl.attr_arn, log_destination_configs=["logs"])
    return dict(assertions.Template.from_stack(stack).to_json())


def test_three_rate_rules_and_the_reputation_list_on_the_public_load_balancer(templates: dict[str, Any]) -> None:
    acl = acl_of(templates)
    assert acl["Properties"]["Scope"] == "REGIONAL"
    assert acl["Properties"]["DefaultAction"] == {"Allow": {}}
    assert rate_rules(acl) == {None: 2000, "/auth/": 100, "/grafana/": 300}
    groups = [r["Statement"].get("ManagedRuleGroupStatement") for r in acl["Properties"]["Rules"]]
    assert {"VendorName": "AWS", "Name": REPUTATION} in groups

    (association,) = resources(templates, "AWS::WAFv2::WebACLAssociation")
    (network,) = [t for t in templates.values() if acl in t["Resources"].values()]
    (acl_id,) = [name for name, r in network["Resources"].items() if r is acl]
    assert association["Properties"]["WebACLArn"] == {"Fn::GetAtt": [acl_id, "Arn"]}
    balancer = network["Resources"][association["Properties"]["ResourceArn"]["Ref"]]
    assert balancer["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer"
    assert balancer["Properties"]["Scheme"] == "internet-facing"


def test_no_body_inspection(templates: dict[str, Any], app_module: ModuleType) -> None:
    assert body_inspection(acl_of(templates)) == []
    assert body_inspection(acl_of({"built": built_acl(app_module)})) == []

    common = {
        "Name": "common",
        "Priority": 4,
        "OverrideAction": {"None": {}},
        "Statement": {"ManagedRuleGroupStatement": {"VendorName": "AWS", "Name": "AWSManagedRulesCommonRuleSet"}},
        "VisibilityConfig": {"CloudWatchMetricsEnabled": True, "MetricName": "common", "SampledRequestsEnabled": False},
    }
    xss = common | {
        "Name": "xss",
        "Action": {"Block": {}},
        "Statement": {
            "XssMatchStatement": {"FieldToMatch": {"Body": {"OversizeHandling": "CONTINUE"}}, "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}
        },
    }
    del xss["OverrideAction"]
    assert body_inspection(acl_of({"built": built_acl(app_module, common)})) == [
        "common: managed rule group AWSManagedRulesCommonRuleSet"
    ]
    assert body_inspection(acl_of({"built": built_acl(app_module, xss)})) == ["xss: XssMatchStatement", "xss: a match on the body"]


def test_no_request_is_sampled_and_any_logging_redacts_the_key_headers(
    templates: dict[str, Any], app_module: ModuleType
) -> None:
    acl = acl_of(templates)
    assert sampling_on(acl) == []
    assert all(r["VisibilityConfig"]["CloudWatchMetricsEnabled"] for r in acl["Properties"]["Rules"])
    sampled = json.loads(json.dumps(acl))
    sampled["Properties"]["Rules"][0]["VisibilityConfig"]["SampledRequestsEnabled"] = True
    assert sampling_on(sampled) == [acl["Properties"]["Rules"][0]["Name"]]

    # The app logs nothing from WAF today; a logging configuration added later must redact both.
    assert [unredacted(logging) for logging in resources(templates, "AWS::WAFv2::LoggingConfiguration")
            if unredacted(logging)] == []
    (built,) = resources({"built": built_acl(app_module, logging=True)}, "AWS::WAFv2::LoggingConfiguration")
    assert unredacted(built) == KEY_HEADERS


def test_the_acl_takes_its_limits_from_the_configuration_with_the_listener_off(
    app_module: ModuleType, tmp_path: Path
) -> None:
    settings = dataclasses.replace(
        EXAMPLE, https_listener=False, rate_limits=config.RateLimits(per_address=5000, issuer=20, grafana=150)
    )
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(settings, outdir=str(tmp_path), context=context)
    app.synth()
    templates = {str(path): json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.rglob("*.template.json")}
    # The public load balancer has no listener; the internal one, which routes to the pools, keeps its own.
    (public,) = [
        name
        for template in templates.values()
        for name, r in template["Resources"].items()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer" and r["Properties"]["Scheme"] == "internet-facing"
    ]
    listeners = resources(templates, "AWS::ElasticLoadBalancingV2::Listener")
    assert [listener for listener in listeners if listener["Properties"]["LoadBalancerArn"] == {"Ref": public}] == []
    assert rate_rules(acl_of(templates)) == {None: 5000, "/auth/": 20, "/grafana/": 150}
    assert len(resources(templates, "AWS::WAFv2::WebACLAssociation")) == 1
