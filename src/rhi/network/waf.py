"""The web ACL of the public load balancer (system design, section 6.5).

Four rules, in this order: the AWS IP reputation list; a rate limit on the issuer, whose routes all
live under `/auth/` (section 6.4); one on Grafana, under `/grafana/`; and a generous one on every
request, since a campus shares one address. Each rate limit counts the requests of one source
address over 5 minutes, and its value is the configuration's `rate_limits` (section 17).

Two things are left out on purpose. No rule group inspects a request's body: the managed
cross-site-scripting rule blocks real coding prompts (evidence E18). And no rule samples requests:
a sample shows the request's headers, `Authorization` and `x-api-key` with the student's key in
cleartext among them, and a logging redaction does not reach samples (evidence E26). The metrics
stay on. The ACL has no logging configuration; one added later must redact both key headers.

The load balancer exists whether or not its HTTPS listener is on, so the ACL always does too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

if TYPE_CHECKING:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    from rhi.config import RateLimits

# A rate-based rule's window: the limits are per 5 minutes.
WINDOW_SECONDS = 300
REPUTATION_LIST = "AWSManagedRulesAmazonIpReputationList"
ISSUER_PREFIX = "/auth/"
GRAFANA_PREFIX = "/grafana/"


def _visibility(metric_name: str) -> wafv2.CfnWebACL.VisibilityConfigProperty:
    # Metrics on, samples off: a sample would show the key headers.
    return wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True, metric_name=metric_name, sampled_requests_enabled=False
    )


def _rate_rule(name: str, priority: int, limit: int, path_prefix: str | None) -> wafv2.CfnWebACL.RuleProperty:
    scope_down = None
    if path_prefix is not None:
        scope_down = wafv2.CfnWebACL.StatementProperty(
            byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(uri_path={}),
                positional_constraint="STARTS_WITH",
                search_string=path_prefix,
                text_transformations=[wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="NONE")],
            )
        )
    return wafv2.CfnWebACL.RuleProperty(
        name=name,
        priority=priority,
        action=wafv2.CfnWebACL.RuleActionProperty(block={}),
        statement=wafv2.CfnWebACL.StatementProperty(
            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                aggregate_key_type="IP",
                limit=limit,
                evaluation_window_sec=WINDOW_SECONDS,
                scope_down_statement=scope_down,
            )
        ),
        visibility_config=_visibility(f"rhi-{name}"),
    )


class WebAcl(Construct):
    """The web ACL, as `web_acl`, attached to `load_balancer`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        load_balancer: elbv2.IApplicationLoadBalancer,
        rate_limits: RateLimits,
    ) -> None:
        super().__init__(scope, construct_id)
        reputation = wafv2.CfnWebACL.RuleProperty(
            name="ip-reputation",
            priority=0,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS", name=REPUTATION_LIST
                )
            ),
            visibility_config=_visibility("rhi-ip-reputation"),
        )
        self.web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=_visibility("rhi-web-acl"),
            rules=[
                reputation,
                _rate_rule("issuer-rate", 1, rate_limits.issuer, ISSUER_PREFIX),
                _rate_rule("grafana-rate", 2, rate_limits.grafana, GRAFANA_PREFIX),
                _rate_rule("per-address-rate", 3, rate_limits.per_address, None),
            ],
        )
        wafv2.CfnWebACLAssociation(
            self, "Association", resource_arn=load_balancer.load_balancer_arn, web_acl_arn=self.web_acl.attr_arn
        )
