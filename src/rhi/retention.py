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
- every function whose logging configuration names no log group gets one of its stack,
  `FunctionLogs`, beside it, and names it there.

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


def _names_a_log_group(function: cdk.CfnResource) -> bool:
    """Whether the function's logging configuration names a log group."""
    properties = cdk.Stack.of(function).resolve(function._cfn_properties)
    logging = _property(properties or {}, "LoggingConfig")
    return isinstance(logging, dict) and _property(logging, "LogGroup") is not None


@jsii.implements(cdk.IAspect)
class Retention:
    """Gives every log group of the scope it is added to a retention of `RETENTION_DAYS`, and every
    function that names no log group one of its stack, `FunctionLogs`, named in its logging
    configuration."""

    def visit(self, node: IConstruct) -> None:
        if not isinstance(node, cdk.CfnResource):
            return
        if node.cfn_resource_type == LOG_GROUP:
            if isinstance(node, logs.CfnLogGroup):
                node.retention_in_days = RETENTION_DAYS
            else:
                node.add_property_override("RetentionInDays", RETENTION_DAYS)
        elif node.cfn_resource_type == FUNCTION and not _names_a_log_group(node):
            scope = node.node.scope
            if not isinstance(scope, Construct):
                msg = f"the function {node.node.path} has no scope to hold its log group"
                raise TypeError(msg)
            existing = scope.node.try_find_child(FUNCTION_LOGS_ID)
            function_logs = existing if isinstance(existing, FunctionLogs) else FunctionLogs(scope, FUNCTION_LOGS_ID)
            node.add_property_override("LoggingConfig.LogGroup", function_logs.log_group.ref)


def apply(stage: cdk.Stage) -> None:
    """Adds `Retention` to the stage, before every aspect that reads what it changes: cdk-nag and
    the acknowledgements (`rhi.nag`) see the log groups it adds."""
    cdk.Aspects.of(stage).add(Retention(), priority=cdk.AspectPriority.MUTATING)
