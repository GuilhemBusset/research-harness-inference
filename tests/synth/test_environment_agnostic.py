"""No stack of the pipeline or of its stage names an account or a region (system design, sections 10
and 12, spike S11): both come from the credentials that deploy.

The check reads the construct tree, where a stack keeps the environment it was given. A stack
given none has an account and a region that are unresolved tokens, which CloudFormation's pseudo
parameters answer at deployment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import aws_cdk as cdk


def explicit_environments(app: cdk.App) -> list[str]:
    """Each stack of the app, those inside its stages included, that names an account or a region,
    as `<path>: account` or `<path>: region`. Empty when none does."""
    import aws_cdk as cdk

    found = []
    for construct in app.node.find_all():
        if isinstance(construct, cdk.Stack):
            for what, value in (("account", construct.account), ("region", construct.region)):
                if not cdk.Token.is_unresolved(value):
                    found.append(f"{construct.node.path}: {what}")
    return found


def test_no_stack_of_the_pipeline_or_of_the_stage_names_an_environment(app: cdk.App) -> None:
    import aws_cdk as cdk

    stacks = [construct for construct in app.node.find_all() if isinstance(construct, cdk.Stack)]
    # Both the pipeline and its stage are checked, so the check cannot pass by finding no stack.
    assert any(cdk.Stage.of(stack) is app for stack in stacks)
    assert any(cdk.Stage.of(stack) is not app for stack in stacks)
    assert explicit_environments(app) == []


def test_a_stack_given_an_account_or_a_region_is_reported(app_module: ModuleType) -> None:
    # `app_module` has started the CDK as the app does.
    del app_module
    import aws_cdk as cdk

    app = cdk.App()
    cdk.Stack(app, "Pipeline", env=cdk.Environment(region="eu-west-1"))
    stage = cdk.Stage(app, "Stage", env=cdk.Environment(account="123456789012"))
    cdk.Stack(stage, "Network")
    cdk.Stack(app, "Agnostic")

    assert sorted(explicit_environments(app)) == ["Pipeline: region", "Stage/Network: account"]
