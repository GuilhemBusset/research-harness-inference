"""Compare fixture templates against the assembly's own nested manifests."""

import json

from rhi.config import load


def test_fixture_exposes_all_nested_stack_templates(app, cloud_assembly, templates, tmp_path_factory):
    found = {}

    def visit(directory):
        manifest = json.loads((directory / "manifest.json").read_text())
        for artifact in manifest["artifacts"].values():
            properties = artifact.get("properties", {})
            if artifact["type"] == "cdk:cloud-assembly":
                visit(directory / properties["directoryName"])
            elif artifact["type"] == "aws:cloudformation:stack":
                path = directory / properties["templateFile"]
                found[path.relative_to(cloud_assembly).as_posix()] = json.loads(path.read_text())

    visit(cloud_assembly)
    assert found and found == templates
    assert any("/" in name for name in found)
    assert cloud_assembly.is_relative_to(tmp_path_factory.getbasetemp())
    assert app.node.try_get_context("aws:cdk:bundling-stacks") == []
    assert app.node.children[0].config == load({"RHI_CONFIG": "example"})
