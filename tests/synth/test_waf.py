"""The web ACL of the public load balancer (system design, section 6.5): three rate limits and the
AWS IP reputation list, no rule that reads a request's body (evidence E18), no request sampling,
which would show the key headers in cleartext (evidence E26), and logs to a log group of its stack
that hold neither key header (section 6.6).

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


def unprotected(acl: dict[str, Any]) -> set[str]:
    """The key headers the ACL's data protection does not replace by `REDACTED` everywhere WAF writes
    a request, a rule's match details and a rate rule's keys included."""
    protections = acl["Properties"].get("DataProtectionConfig", {}).get("DataProtections", [])
    protected: set[str] = set()
    for protection in protections:
        field = protection["Field"]
        everywhere = not protection.get("ExcludeRuleMatchDetails") and not protection.get("ExcludeRateBasedDetails")
        if field["FieldType"] == "SINGLE_HEADER" and protection["Action"] == "SUBSTITUTION" and everywhere:
            # Only the headers it names count: each key header is named, not left to a default.
            protected |= {key.lower() for key in field.get("FieldKeys", [])}
    return KEY_HEADERS - protected


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
    app_module: ModuleType, extra_rule: dict[str, Any] | None = None, *, unprotected: bool = False
) -> dict[str, Any]:
    """The template of a stack, built here, that holds the app's ACL, one more rule if given, and,
    if asked, neither its logging redaction nor its data protection."""
    del app_module  # started jsii
    import aws_cdk as cdk
    from aws_cdk import assertions
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    from rhi.network.waf import WebAcl

    stack = cdk.Stack(cdk.App(), "Built")
    balancer = elbv2.ApplicationLoadBalancer.from_application_load_balancer_attributes(
        stack,
        "Balancer",
        # The stack's own account and region, as tokens: no ARN is written out.
        load_balancer_arn=stack.format_arn(service="elasticloadbalancing", resource="loadbalancer", resource_name="app/b/0"),
        security_group_id="security-group",
    )
    waf = WebAcl(stack, "Waf", load_balancer=balancer, rate_limits=EXAMPLE.rate_limits)
    if extra_rule is not None:
        waf.web_acl.add_property_override("Rules.4", extra_rule)  # after the four rules of the construct
    if unprotected:
        waf.logging.add_property_deletion_override("RedactedFields")
        waf.web_acl.add_property_deletion_override("DataProtectionConfig")
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


def test_no_request_is_sampled_and_the_logs_hold_neither_key_header(
    templates: dict[str, Any], app_module: ModuleType
) -> None:
    acl = acl_of(templates)
    assert sampling_on(acl) == []
    assert all(r["VisibilityConfig"]["CloudWatchMetricsEnabled"] for r in acl["Properties"]["Rules"])
    sampled = json.loads(json.dumps(acl))
    sampled["Properties"]["Rules"][0]["VisibilityConfig"]["SampledRequestsEnabled"] = True
    assert sampling_on(sampled) == [acl["Properties"]["Rules"][0]["Name"]]

    # Both key headers are redacted from the logs, and replaced wherever WAF writes a request,
    # whichever rule matches.
    (logging,) = resources(templates, "AWS::WAFv2::LoggingConfiguration")
    assert unredacted(logging) == set()
    assert unprotected(acl) == set()
    built = {"built": built_acl(app_module, unprotected=True)}
    (built_logging,) = resources(built, "AWS::WAFv2::LoggingConfiguration")
    assert unredacted(built_logging) == KEY_HEADERS
    assert unprotected(acl_of(built)) == KEY_HEADERS
    # An exception for a rule's match details leaves the headers there in cleartext.
    excepted = json.loads(json.dumps(acl))
    excepted["Properties"]["DataProtectionConfig"]["DataProtections"][0]["ExcludeRuleMatchDetails"] = True
    assert unprotected(excepted) == KEY_HEADERS


def test_the_acl_logs_to_a_waf_log_group_of_its_stack(templates: dict[str, Any]) -> None:
    acl = acl_of(templates)
    (network,) = [t for t in templates.values() if acl in t["Resources"].values()]
    (acl_id,) = [name for name, r in network["Resources"].items() if r is acl]
    (logging,) = resources({"network": network}, "AWS::WAFv2::LoggingConfiguration")
    assert logging["Properties"]["ResourceArn"] == {"Fn::GetAtt": [acl_id, "Arn"]}
    # The destination is the ARN of a log group of the stack, as WAF takes it: ending in its name.
    (destination,) = logging["Properties"]["LogDestinationConfigs"]
    *_, name = destination["Fn::Join"][1]
    group = network["Resources"][name["Ref"]]
    assert group["Type"] == "AWS::Logs::LogGroup"
    assert group["Properties"]["LogGroupName"].startswith("aws-waf-logs-")


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
