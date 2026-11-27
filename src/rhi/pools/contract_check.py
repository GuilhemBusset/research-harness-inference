"""The engine contract check on an instance: the Systems Manager document `rhi-engine-contract`
(system design, sections 4.1 and 4.3).

The contract suite of `tests/contract/` is an HTTP client of an engine, and nothing deployed
reaches a deployed engine but the gateway, through the internal load balancer, which answers 404
to the health route and to `/metrics`, served on localhost only. So the suite runs where its own
configuration places it: on the node, against `http://127.0.0.1:8000`. The release's archive
carries it there (`contract/`, `rhi.pools.release`), and its script,
`/opt/rhi/release/contract/run.sh` (`services/engine/contract/run.sh`), runs it.

The document is of type Command and takes no parameter: its one step runs that script with bash,
since the archive gives no file the executable mode, and nothing else. Whoever may send it can run
the contract check on an instance, and no other command. Run Command reaches the instance's agent
through `ssmmessages`, the channel of Session Manager, whose actions the instance role holds
already (`rhi.pools.role.SESSION_ACTIONS`): this module grants nothing, to the instances or to
anyone who would send it. A change to its content is a new version of the same document.
"""

from __future__ import annotations

from aws_cdk import aws_ssm as ssm
from constructs import Construct

DOCUMENT_NAME = "rhi-engine-contract"
SCRIPT = "/opt/rhi/release/contract/run.sh"
# How long the step may run: the four tests, each request waiting up to two minutes for the
# engine's next bytes (tests/contract/conftest.py, READ_SECONDS), with room to spare. The plugin
# `aws:runShellScript` takes it as a string of decimal digits, so the content carries it so.
TIMEOUT_SECONDS = 900


def content() -> dict[str, object]:
    """The document's content, in the schema of a Command document."""
    return {
        "schemaVersion": "2.2",
        "description": "Runs the engine contract suite of the release against the instance's own engine.",
        "mainSteps": [
            {
                "action": "aws:runShellScript",
                "name": "contract",
                "inputs": {"runCommand": [f"bash {SCRIPT}"], "timeoutSeconds": str(TIMEOUT_SECONDS)},
            }
        ],
    }


class ContractCheck(Construct):
    """The document `rhi-engine-contract`, as `document`."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.document = ssm.CfnDocument(
            self,
            "Document",
            name=DOCUMENT_NAME,
            document_type="Command",
            content=content(),
            update_method="NewVersion",
        )
