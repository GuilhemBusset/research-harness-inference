"""The document that runs the engine contract suite on an instance (`rhi.pools.contract_check`;
system design, sections 4.1 and 4.3): one Command document, `rhi-engine-contract`, with no
parameter, whose one step runs the release's script with bash.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import aws_cdk as cdk


@pytest.fixture(scope="module")
def pools(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[missing-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[missing-attribute]
    return templates[path]


def test_one_command_document_runs_the_release_s_contract_script(pools: dict[str, Any]) -> None:
    from rhi.pools.contract_check import TIMEOUT_SECONDS

    documents = [r["Properties"] for r in pools["Resources"].values() if r["Type"] == "AWS::SSM::Document"]
    (document,) = documents
    assert document["Name"] == "rhi-engine-contract"
    assert document["DocumentType"] == "Command"
    # A change to the content is a new version of the same document.
    assert document["UpdateMethod"] == "NewVersion"

    content = document["Content"]
    assert "parameters" not in content
    (step,) = content["mainSteps"]
    assert step["action"] == "aws:runShellScript"
    assert step["inputs"]["runCommand"] == ["bash /opt/rhi/release/contract/run.sh"]
    # The plugin's input `timeoutSeconds` is a string of decimal digits, not a number.
    timeout = step["inputs"]["timeoutSeconds"]
    assert isinstance(timeout, str)
    assert timeout.isdigit()
    assert int(timeout) == TIMEOUT_SECONDS > 0
