"""Synthesize the stage directly, independently of the app fixture."""

import json

import aws_cdk as cdk

from rhi.config import load
from rhi.stage import EndpointStage


def test_stage_contains_one_empty_stack(tmp_path):
    app = cdk.App(outdir=str(tmp_path), analytics_reporting=False)
    stage = EndpointStage(app, "Probe", config=load({"RHI_CONFIG": "example"}))
    stacks = [child for child in stage.node.children if isinstance(child, cdk.Stack)]
    assert len(stacks) == 1
    assert cdk.Token.is_unresolved(stacks[0].account)
    assert cdk.Token.is_unresolved(stacks[0].region)
    app.synth()
    templates = list(tmp_path.rglob("*.template.json"))
    assert len(templates) == 1
    assert templates[0].parent != tmp_path
    assert not json.loads(templates[0].read_text()).get("Resources")
