"""Nothing reaches Grafana that has not signed in (system design, sections 7.4 and 8.4).

Grafana has no login of its own: it trusts the identity header that the public load balancer sets
after its sign-in against the admin's user pool. A rule that forwarded to Grafana without
authenticating first would let anyone name themselves. So, in the templates of the app:

- a rule of a public load balancer's listener that takes paths under `/grafana/` and forwards
  authenticates first: its first action signs in against a user pool of the app, and sends away
  whoever has not signed in;
- a rule that forwards to a target group of Grafana is such a rule: it takes paths under
  `/grafana/` and authenticates first. No other rule forwards to Grafana, whatever its listener,
  and no listener's default action does, which takes every path that no rule took.

A target group is Grafana's when the module of Grafana created it (`created_by`), and when a rule
that takes paths under `/grafana/` forwards to it. A rule takes those paths unless a path
condition shows that it cannot. A rule whose listener, or whose listener's load balancer, the
templates do not show is read as one of a public load balancer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    Templates = dict[str, dict[str, Any]]
    CreatedBy = dict[tuple[str, str], str]
    # A resource of the templates: the path of its template and its logical ID.
    Held = tuple[str, str]

PREFIX = "/grafana/"
# The name the module of Grafana gives its acknowledgement file (`acknowledged/README.md`).
GRAFANA_MODULE = "observability_grafana"

ELB = "AWS::ElasticLoadBalancingV2::"
RULE = ELB + "ListenerRule"
LISTENER = ELB + "Listener"
LOAD_BALANCER = ELB + "LoadBalancer"
TARGET_GROUP = ELB + "TargetGroup"
USER_POOL = "AWS::Cognito::UserPool"

FORWARD = "forward"
SIGN_IN = "authenticate-cognito"
# What a sign-in action does with a request that has not signed in, other than letting it through.
TURNED_AWAY = ("authenticate", "deny")

OPEN = f"takes paths under {PREFIX} and forwards before it authenticates"
SIDE_DOOR = "forwards to Grafana and is no rule that authenticates first"


class Finding(NamedTuple):
    template: str
    logical_id: str
    problem: str


def of_type(templates: Templates, kind: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Every resource of a type: its template's path, its logical ID and its properties."""
    for path, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            if resource.get("Type") == kind:
                yield path, logical_id, resource.get("Properties", {})


def held(reference: Any, path: str, templates: Templates) -> Held | None:
    """The resource a reference in the template at `path` names, in whichever template holds it,
    or None when the templates do not show it. An import is what its stack exports."""
    if not isinstance(reference, dict) or len(reference) != 1:
        return None
    ((function, argument),) = reference.items()
    resources = templates[path].get("Resources", {})
    if function == "Ref" and argument in resources:
        return path, argument
    if function == "Fn::GetAtt" and isinstance(argument, list) and argument and argument[0] in resources:
        return path, argument[0]
    if function == "Fn::ImportValue":
        exported = [
            (other, output.get("Value"))
            for other, template in templates.items()
            for output in template.get("Outputs", {}).values()
            if isinstance(output, dict) and output.get("Export", {}).get("Name") == argument
        ]
        if len(exported) == 1:
            return held(exported[0][1], exported[0][0], templates)
    return None


def kind_of(resource: Held | None, templates: Templates) -> str | None:
    return None if resource is None else templates[resource[0]]["Resources"][resource[1]].get("Type")


def may_begin(pattern: str, prefix: str) -> bool:
    """Whether a path pattern of a load balancer, in which `*` is any text and `?` any one
    character, takes some path that begins with `prefix`."""
    # The positions of the pattern that the part of the prefix read so far can have reached.
    reached = {0}
    for character in prefix:
        following = set()
        for position in reached:
            # A `*` may also take nothing: the pattern goes on after it.
            while position < len(pattern) and pattern[position] == "*":
                following.add(position)
                position += 1
            if position < len(pattern) and pattern[position] in (character, "?"):
                following.add(position + 1)
        reached = following
    # Whatever is left of the pattern, some rest of a path matches it.
    return bool(reached)


def takes_grafana_paths(rule: Mapping[str, Any]) -> bool:
    """Whether the rule takes some path under `/grafana/`. It does unless one of its conditions is
    on the path and none of that condition's patterns can begin so. A pattern that is not written
    out, and a regular expression, may be anything."""
    conditions = rule.get("Conditions", [])
    if not isinstance(conditions, list):
        return True
    for condition in conditions:
        if not isinstance(condition, dict) or condition.get("Field") != "path-pattern":
            continue
        configured = condition.get("PathPatternConfig", {})
        if not isinstance(configured, dict) or "RegexValues" in configured:
            continue
        patterns = [*(configured.get("Values") or []), *(condition.get("Values") or [])]
        if not any(not isinstance(pattern, str) or may_begin(pattern, PREFIX) for pattern in patterns):
            return False
    return True


def listed(actions: Any) -> list[Any]:
    return actions if isinstance(actions, list) else [actions]


def first(actions: Any) -> Any:
    """The action the load balancer performs first: the only one, or the one of the lowest
    `Order`. None when the template does not show which it is."""
    actions = listed(actions)
    if len(actions) == 1:
        return actions[0]
    given = [action.get("Order") if isinstance(action, dict) else None for action in actions]
    orders = [order for order in given if type(order) is int]
    if not orders or len(orders) != len(actions) or orders.count(min(orders)) != 1:
        return None
    return actions[orders.index(min(orders))]


def is_forward(action: Any) -> bool:
    """A forward, or what the test cannot read as another kind of action."""
    return not isinstance(action, dict) or action.get("Type") == FORWARD or not isinstance(action.get("Type"), str)


def forwards_to(actions: Any, path: str, templates: Templates) -> set[Held]:
    """The target groups of the templates that the actions forward to."""
    found = set()
    for action in listed(actions):
        if not isinstance(action, dict) or not is_forward(action):
            continue
        weighted = (action.get("ForwardConfig") or {}).get("TargetGroups") or []
        named = [action.get("TargetGroupArn"), *(group.get("TargetGroupArn") for group in weighted)]
        found.update(group for name in named if (group := held(name, path, templates)) is not None)
    return found


def signs_in(action: Any, path: str, templates: Templates) -> bool:
    """Whether the action signs in against a user pool of the app, and turns away a request that
    has not signed in."""
    if not isinstance(action, dict) or action.get("Type") != SIGN_IN:
        return False
    configured = action.get("AuthenticateCognitoConfig", {})
    # Left out, the load balancer sends the request to the sign-in.
    if configured.get("OnUnauthenticatedRequest", "authenticate") not in TURNED_AWAY:
        return False
    return kind_of(held(configured.get("UserPoolArn"), path, templates), templates) == USER_POOL


def authenticates_first(actions: Any, path: str, templates: Templates) -> bool:
    return signs_in(first(actions), path, templates)


def is_public(rule: Mapping[str, Any], path: str, templates: Templates) -> bool:
    """Whether the rule is on a listener of a public load balancer. Only a load balancer that the
    templates show to be internal is not public."""
    listener = held(rule.get("ListenerArn"), path, templates)
    if kind_of(listener, templates) != LISTENER:
        return True
    assert listener is not None
    properties = templates[listener[0]]["Resources"][listener[1]].get("Properties", {})
    balancer = held(properties.get("LoadBalancerArn"), listener[0], templates)
    if kind_of(balancer, templates) != LOAD_BALANCER:
        return True
    assert balancer is not None
    return templates[balancer[0]]["Resources"][balancer[1]].get("Properties", {}).get("Scheme") != "internal"


def grafana_target_groups(templates: Templates, created_by: CreatedBy) -> set[Held]:
    """The target groups the module of Grafana created, and those a rule that takes paths under
    `/grafana/` and signs in forwards to."""
    created = {(path, logical_id) for path, logical_id, _ in of_type(templates, TARGET_GROUP)}
    found = {group for group in created if created_by.get(group) == GRAFANA_MODULE}
    for path, _, rule in of_type(templates, RULE):
        if takes_grafana_paths(rule) and authenticates_first(rule.get("Actions"), path, templates):
            found |= forwards_to(rule.get("Actions"), path, templates)
    return found


def violations(templates: Templates, created_by: CreatedBy) -> list[Finding]:
    """Every rule that opens Grafana: one that forwards paths under `/grafana/` from the internet
    before it authenticates, and one that forwards to Grafana and is not a rule for those paths
    that authenticates first. A listener's default action is never such a rule."""
    grafana = grafana_target_groups(templates, created_by)
    found = []
    for path, logical_id, rule in of_type(templates, RULE):
        actions = rule.get("Actions")
        takes = takes_grafana_paths(rule)
        authenticated = authenticates_first(actions, path, templates)
        forwards = any(is_forward(action) for action in listed(actions))
        if forwards and takes and not authenticated and is_public(rule, path, templates):
            found.append(Finding(path, logical_id, OPEN))
        elif forwards_to(actions, path, templates) & grafana and not (takes and authenticated):
            found.append(Finding(path, logical_id, SIDE_DOOR))
    found.extend(
        Finding(path, logical_id, SIDE_DOOR)
        for path, logical_id, listener in of_type(templates, LISTENER)
        if forwards_to(listener.get("DefaultActions", []), path, templates) & grafana
    )
    return found


def test_grafana_is_reached_only_through_the_sign_in(templates: Templates, created_by: CreatedBy) -> None:
    assert violations(templates, created_by) == []
    # The invariant holds of something: the app has Grafana's target group, which its module
    # created, and one rule forwards to it, on the public load balancer, for the paths of Grafana.
    (group,) = grafana_target_groups(templates, created_by)
    assert created_by[group] == GRAFANA_MODULE
    ((path, rule),) = [
        (path, rule)
        for path, _, rule in of_type(templates, RULE)
        if group in forwards_to(rule["Actions"], path, templates)
    ]
    assert is_public(rule, path, templates) and takes_grafana_paths(rule)
    assert authenticates_first(rule["Actions"], path, templates)


# The templates of the tests below: a public and an internal load balancer with a listener each
# and the admin's user pool, and, in a second template, Grafana's target group, another one and
# the rules of each test.
EDGE = "edge.template.json"
BEHIND = "behind.template.json"
POOL_ARN = {"Fn::ImportValue": "Pool"}
SIGNED_IN = {"Type": SIGN_IN, "Order": 1, "AuthenticateCognitoConfig": {"UserPoolArn": POOL_ARN}}
TO_GRAFANA = {"Type": FORWARD, "Order": 2, "TargetGroupArn": {"Ref": "Grafana"}}
TO_ANOTHER = {"Type": FORWARD, "TargetGroupArn": {"Ref": "Another"}}
NOT_FOUND = {"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "404"}}


def paths(*patterns: str) -> list[dict[str, Any]]:
    return [{"Field": "path-pattern", "PathPatternConfig": {"Values": list(patterns)}}]


def rule(actions: list[dict[str, Any]], conditions: list[dict[str, Any]], listener: str = "Public") -> dict[str, Any]:
    properties = {"ListenerArn": {"Fn::ImportValue": listener}, "Priority": 30, "Actions": actions}
    return {"Type": RULE, "Properties": properties | {"Conditions": conditions}}


def listener(balancer: Any, default: dict[str, Any]) -> dict[str, Any]:
    return {"Type": LISTENER, "Properties": {"LoadBalancerArn": balancer, "DefaultActions": [default]}}


def found_in(behind: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """What `violations` reports when the second template holds these rules: each finding as its
    resource and its problem."""
    templates = {
        EDGE: {
            "Resources": {
                "PublicBalancer": {"Type": LOAD_BALANCER, "Properties": {"Scheme": "internet-facing"}},
                "InternalBalancer": {"Type": LOAD_BALANCER, "Properties": {"Scheme": "internal"}},
                "PublicListener": listener({"Ref": "PublicBalancer"}, NOT_FOUND),
                "InternalListener": listener({"Ref": "InternalBalancer"}, NOT_FOUND),
                "UserPool": {"Type": USER_POOL},
            },
            "Outputs": {
                "Balancer": {"Value": {"Ref": "PublicBalancer"}, "Export": {"Name": "Balancer"}},
                "Public": {"Value": {"Ref": "PublicListener"}, "Export": {"Name": "Public"}},
                "Internal": {"Value": {"Ref": "InternalListener"}, "Export": {"Name": "Internal"}},
                "Pool": {"Value": {"Fn::GetAtt": ["UserPool", "Arn"]}, "Export": {"Name": "Pool"}},
            },
        },
        BEHIND: {"Resources": {"Grafana": {"Type": TARGET_GROUP}, "Another": {"Type": TARGET_GROUP}} | behind},
    }
    created_by = {(BEHIND, "Grafana"): GRAFANA_MODULE, (BEHIND, "Another"): "gateway_service"}
    return [(finding.logical_id, finding.problem) for finding in violations(templates, created_by)]


def test_admits_a_rule_that_authenticates_before_it_forwards() -> None:
    assert (
        found_in(
            {
                # Listed in another order than they are performed in.
                "Rule": rule([TO_GRAFANA, SIGNED_IN], paths("/grafana/*", "/oauth2/idpresponse")),
                # Paths that are not Grafana's, forwarded elsewhere; a rule of the internal load
                # balancer that takes every path; and a rule for Grafana's paths that forwards nothing.
                "Gateway": rule([TO_ANOTHER], paths("/v1/*", "/status")),
                "Pool": rule([TO_ANOTHER], [{"Field": "http-header"}], listener="Internal"),
                "Moved": rule([{"Type": "redirect"}], paths("/grafana/old/*")),
            }
        )
        == []
    )


def sign_in(**configured: Any) -> dict[str, Any]:
    return SIGNED_IN | {"AuthenticateCognitoConfig": configured}


@pytest.mark.parametrize(
    "actions",
    [
        pytest.param([TO_GRAFANA], id="forwards and never authenticates"),
        pytest.param([TO_GRAFANA | {"Order": 1}, SIGNED_IN | {"Order": 2}], id="forwards, then authenticates"),
        pytest.param([SIGNED_IN | {"Order": {"Ref": "Parameter"}}, TO_GRAFANA], id="an order chosen at deployment"),
        pytest.param(
            [sign_in(UserPoolArn=POOL_ARN, OnUnauthenticatedRequest="allow"), TO_GRAFANA],
            id="lets through who has not signed in",
        ),
        pytest.param(
            [sign_in(UserPoolArn={"Ref": "Parameter"}), TO_GRAFANA],
            id="signs in against a user pool that is not the app's",
        ),
        pytest.param(
            [{"Type": "authenticate-oidc", "Order": 1, "AuthenticateOidcConfig": {}}, TO_GRAFANA],
            id="signs in against another provider",
        ),
        pytest.param([TO_ANOTHER], id="forwards to another target group"),
    ],
)
def test_detects_an_open_grafana(actions: list[dict[str, Any]]) -> None:
    assert found_in({"Rule": rule(actions, paths("/grafana/*"))}) == [("Rule", OPEN)]


@pytest.mark.parametrize(
    "conditions",
    [
        pytest.param([], id="no condition"),
        pytest.param([{"Field": "host-header", "HostHeaderConfig": {"Values": ["example.com"]}}], id="a host only"),
        pytest.param(paths("/v1/*", "/*"), id="every path"),
        pytest.param(paths("/graf*"), id="a wider prefix"),
        pytest.param(paths("/grafana/api/*"), id="a part of Grafana"),
        pytest.param(paths("/?rafana/*"), id="any one character"),
        pytest.param([{"Field": "path-pattern", "Values": ["/grafana/*"]}], id="the older form of a path condition"),
        pytest.param(
            [{"Field": "path-pattern", "PathPatternConfig": {"Values": [{"Ref": "Parameter"}]}}],
            id="a pattern chosen at deployment",
        ),
    ],
)
def test_a_rule_is_for_grafana_whenever_it_takes_a_path_under_the_prefix(conditions: list[dict[str, Any]]) -> None:
    assert found_in({"Rule": rule([TO_ANOTHER], conditions)}) == [("Rule", OPEN)]


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(rule([TO_GRAFANA], paths("/v1/*")), id="a rule for another path"),
        pytest.param(rule([SIGNED_IN, TO_GRAFANA], paths("/v1/*")), id="a rule for another path that authenticates"),
        pytest.param(rule([TO_GRAFANA], [], listener="Internal"), id="a rule of the internal load balancer"),
        pytest.param(
            rule([{"Type": FORWARD, "ForwardConfig": {"TargetGroups": [TO_ANOTHER, TO_GRAFANA]}}], paths("/v1/*")),
            id="one target group among several",
        ),
        pytest.param(listener({"Fn::ImportValue": "Balancer"}, TO_GRAFANA), id="a listener's default action"),
    ],
)
def test_detects_another_rule_that_forwards_to_grafana(other: dict[str, Any]) -> None:
    signed_in = {"Rule": rule([SIGNED_IN, TO_GRAFANA], paths("/grafana/*"))}
    assert found_in(signed_in | {"Other": other}) == [("Other", SIDE_DOOR)]
