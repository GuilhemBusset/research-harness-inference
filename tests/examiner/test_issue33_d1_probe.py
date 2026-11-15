"""Synthesize the real bootstrap template and check its fixed managed policies."""
from pathlib import Path


def test_two_fixed_managed_policies(tmp_path, monkeypatch):
    # The CDK is imported here, not at the top of the file: the import starts jsii, which refuses a
    # package cache below a directory that another user owns, and that is what the implement loop's
    # sandbox gives it. At the top of the file it would fail when pytest collects this folder.
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    from aws_cdk import App, Stack
    from aws_cdk.assertions import Template
    from aws_cdk.cloudformation_include import CfnInclude

    root = Path(__file__).resolve().parents[2]
    app = App(outdir=str(tmp_path), analytics_reporting=False)
    stack = Stack(app, "BootstrapPolicies")
    CfnInclude(stack, "Policies", template_file=str(root / "setup/bootstrap/policies.yaml"))
    resources = Template.from_stack(stack).to_json()["Resources"]
    assert len(resources) == 2
    assert {resource["Type"] for resource in resources.values()} == {"AWS::IAM::ManagedPolicy"}
    assert {resource["Properties"]["ManagedPolicyName"] for resource in resources.values()} == {
        "rhi-cfn-execution", "rhi-permissions-boundary",
    }
