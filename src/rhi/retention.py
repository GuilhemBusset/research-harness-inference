"""Retention on every log group of the stage, and a log group of its stack for every function
(system design, section 6.6).

What the stage logs holds personal data: usage metadata per student, and source addresses. A log
group's retention is a period counted from when each record is written, so 90 days is what makes
"course end plus 90 days" hold: whatever was written before course end is gone 90 days after it.
The rows of the tables carry their own expiry date, and the metrics workspace its own retention
(`rhi.observability.prometheus`).

**A log group that no template holds is kept forever.** A Lambda function that names no log group
in its logging configuration writes to one that Lambda creates at run time, with no retention and
outside every template. So `Retention`, an aspect of the stage (`rhi.stage`), visits every
resource of every stack of the stage, those the CDK adds for its own custom resources included, and
a function added later all the same:

- every log group gets a retention of 90 days, whatever its construct asked for;
- every function whose logging configuration names no log group of its own stack (none, or one
  imported from elsewhere) gets one of its stack, `FunctionLogs`, beside it, and names it there.

The CDK's own function for AWS SDK custom resources comes with a log group of the default name,
`/aws/lambda/<function>`, which is retained. It cannot be named in the function's logging
configuration, since its name is made of the function's: the function gets a `FunctionLogs` all
the same, and the default group is kept, with the same 90 days, so that what was written there
before still expires.

`tests/policy/test_retention.py` holds the stage's templates to all of this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aws_cdk as cdk
import jsii
from aws_cdk import aws_logs as logs
from constructs import Construct

if TYPE_CHECKING:
    from constructs import IConstruct

# Section 6.6: course end plus 90 days, as a period from when each record is written.
RETENTION_DAYS = 90
LOG_GROUP = "AWS::Logs::LogGroup"
FUNCTION = "AWS::Lambda::Function"
# The ID of the `FunctionLogs` the aspect adds beside a function.
FUNCTION_LOGS_ID = "FunctionLogs"


class FunctionLogs(Construct):
    """The log group, as `log_group`, of a function that named none."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.log_group = logs.CfnLogGroup(self, "Logs", retention_in_days=RETENTION_DAYS)
        # The logs go with the stack: nothing of them is kept past it.
        self.log_group.apply_removal_policy(cdk.RemovalPolicy.DESTROY)


def _property(properties: dict[str, Any], name: str) -> Any:
    """A property of a resource, whether the construct keeps its key as CloudFormation writes it
    (`LoggingConfig`) or in camel case (`loggingConfig`)."""
    return next((value for key, value in properties.items() if key.lower() == name.lower()), None)


def _stack_log_group(function: cdk.CfnResource) -> Any:
    """A reference to the log group the function's logging configuration names, if that is a log
    group of the function's own stack: by a reference to it, or by the name its construct gives it.
    None for no log group, or one of another stack or of no template.

    The reference, not the name: it stays the group's name in the template even if an override
    renames the group after its construct named it."""
    stack = cdk.Stack.of(function)
    logging = _property(stack.resolve(function._cfn_properties) or {}, "LoggingConfig")
    resolved = _property(logging, "LogGroup") if isinstance(logging, dict) else None
    if resolved is None:
        return None
    for group in stack.node.find_all():
        if not (isinstance(group, cdk.CfnResource) and group.cfn_resource_type == LOG_GROUP):
            continue
        if cdk.Stack.of(group) is not stack:
            continue
        name = _property(stack.resolve(group._cfn_properties) or {}, "LogGroupName")
        if resolved == stack.resolve(group.ref) or (isinstance(name, str) and resolved == name):
            return group.ref
    return None


@jsii.implements(cdk.IAspect)
class Retention:
    """Gives every log group of the scope it is added to a retention of `RETENTION_DAYS`, and every
    function a log group of its stack named in its logging configuration: the one it names, if it
    is of its stack, or else one added beside it, `FunctionLogs`.

    Both are written as property overrides, which CloudFormation's template takes over the typed
    properties and over any override a construct wrote before: what the aspect sets is what the
    template holds."""

    def visit(self, node: IConstruct) -> None:
        if not isinstance(node, cdk.CfnResource):
            return
        if node.cfn_resource_type == LOG_GROUP:
            if isinstance(node, logs.CfnLogGroup):
                node.retention_in_days = RETENTION_DAYS
            node.add_property_override("RetentionInDays", RETENTION_DAYS)
        elif node.cfn_resource_type == FUNCTION:
            named = _stack_log_group(node)
            if named is None:
                scope = node.node.scope
                if not isinstance(scope, Construct):
                    msg = f"the function {node.node.path} has no scope to hold its log group"
                    raise TypeError(msg)
                existing = scope.node.try_find_child(FUNCTION_LOGS_ID)
                function_logs = existing if isinstance(existing, FunctionLogs) else FunctionLogs(scope, FUNCTION_LOGS_ID)
                named = function_logs.log_group.ref
            node.add_property_override("LoggingConfig.LogGroup", named)


def apply(stage: cdk.Stage) -> None:
    """Adds `Retention` to the stage, before every aspect that reads what it changes: cdk-nag and
    the acknowledgements (`rhi.nag`) see the log groups it adds."""
    cdk.Aspects.of(stage).add(Retention(), priority=cdk.AspectPriority.MUTATING)
