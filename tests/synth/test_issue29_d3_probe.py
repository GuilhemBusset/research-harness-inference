"""Synthesize the stage directly, independently of the app fixture.

The CDK is imported inside the test, after `app_module` has started jsii (see conftest.py).
"""

from rhi.config import load


def test_stage_stacks_are_environment_agnostic_and_nested(app_module, tmp_path):
    import aws_cdk as cdk

    from rhi.stage import EndpointStage

    app = cdk.App(outdir=str(tmp_path), analytics_reporting=False)
    stage = EndpointStage(app, "Probe", config=load({"RHI_CONFIG": "example"}))
    stacks = [child for child in stage.node.children if isinstance(child, cdk.Stack)]
    assert stacks
    for stack in stacks:
        assert cdk.Token.is_unresolved(stack.account)
        assert cdk.Token.is_unresolved(stack.region)
    app.synth()
    templates = list(tmp_path.rglob("*.template.json"))
    assert len(templates) == len(stacks)
    assert all(template.parent != tmp_path for template in templates)
