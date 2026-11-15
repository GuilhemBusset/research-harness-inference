"""Nothing is acknowledged to cdk-nag that the acknowledgement files do not list (system design, section 8.4, control 2).

cdk-nag's AWS Solutions pack runs inside synthesis and fails it on a finding. An acknowledged
finding no longer fails it, and any code of the app can acknowledge one. So the app has cdk-nag
write every acknowledgement into the template, in the metadata of each resource it covers
(`app.py`), and this test holds what is written there to the protected files: an acknowledgement
on a resource is in the `[nag]` table of the file of the module that created the resource
(`acknowledged/README.md`), under the same finding id and with the same reason.

An acknowledgement made on a construct covers every resource below it, and cdk-nag writes it on
each of them: each is held to the file of its own module.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import ModuleType

    Templates = dict[str, dict[str, Any]]
    CreatedBy = dict[tuple[str, str], str]
    Acknowledged = dict[str, dict[str, Any]]

# Where cdk-nag writes the acknowledgements of a resource, in its metadata: a list of findings,
# each as its `id` and its `reason`.
KEY = "cdk_nag"
RULES = "rules_to_suppress"

# Reported in place of a finding id, for metadata under `KEY` that is not such a list.
UNREADABLE = "not a list of finding ids and reasons"


class Finding(NamedTuple):
    """An acknowledgement on a resource that the file of its module does not list."""

    template: str
    resource: str
    acknowledgement: str


def unlisted(resource: Mapping[str, Any], listed: Mapping[str, Any]) -> list[str]:
    """The acknowledgements in a resource's metadata that `listed`, the nag table of the file of
    its module, does not hold with the same reason."""
    metadata = resource.get("Metadata")
    if not isinstance(metadata, dict) or KEY not in metadata:
        return []
    rules = metadata[KEY].get(RULES) if isinstance(metadata[KEY], dict) else None
    if not isinstance(rules, list):
        return [UNREADABLE]
    found = []
    for rule in rules:
        finding = rule.get("id") if isinstance(rule, dict) else None
        if not isinstance(finding, str):
            found.append(UNREADABLE)
        elif finding not in listed or listed[finding] != rule.get("reason"):
            found.append(finding)
    return found


def violations(templates: Templates, created_by: CreatedBy, acknowledged: Acknowledged) -> list[Finding]:
    """Every acknowledgement in the templates that the file of the resource's module does not list."""
    return [
        Finding(path, logical_id, finding)
        for path, template in templates.items()
        for logical_id, resource in template.get("Resources", {}).items()
        for finding in unlisted(resource, acknowledged.get(created_by[path, logical_id], {}).get("nag", {}))
    ]


def test_every_acknowledgement_of_the_app_is_listed(
    templates: Templates, created_by: CreatedBy, acknowledged: Acknowledged
) -> None:
    assert violations(templates, created_by, acknowledged) == []


# What the pack finds on a topic that nothing configures: its publishers need not use TLS.
FINDING = "AwsSolutions-SNS3"
REASON = "The topic of this test carries no message."

PIPELINE = "import aws_cdk as cdk\n\n\nclass Pipeline(cdk.Stack):\n    pass\n"


def test_detects_an_unlisted_acknowledgement(
    cdk: ModuleType,
    read_app: Callable[[Any, Path], tuple[Templates, CreatedBy]],
    read_acknowledged: Callable[[Path], Acknowledged],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cdk_nag

    # A source tree with one module, imported from there: a class names its module, and the
    # module names its file.
    module = tmp_path / "src" / "rhi" / "pipeline" / "stack.py"
    module.parent.mkdir(parents=True)
    module.write_text(PIPELINE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("nag_acknowledgements_test_pipeline_stack", module)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)

    # A stack of that module, under the pack as the app has it, whose code acknowledges the
    # finding of its topic. No file is asked: synthesis succeeds.
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    topic = cdk.aws_sns.Topic(loaded.Pipeline(app, "Pipeline"), "Topic")
    cdk.Validations.of(topic).acknowledge(cdk.Acknowledgment(id=FINDING, reason=REASON))
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app, write_suppressions_to_cloud_formation=True))
    templates, created_by = read_app(app, module.parents[1])
    assert list(created_by.values()) == ["pipeline_stack"]
    unlisted_on_the_topic = [Finding(*resource, FINDING) for resource in created_by]

    def found(case: str, files: dict[str, dict[str, str]]) -> list[Finding]:
        folder = tmp_path / case
        folder.mkdir()
        for name, nag in files.items():
            entries = "".join(f"{json.dumps(finding)} = {json.dumps(reason)}\n" for finding, reason in nag.items())
            (folder / f"{name}.toml").write_text(f"[nag]\n{entries}", encoding="utf-8")
        return violations(templates, created_by, read_acknowledged(folder))

    assert found("no file", {}) == unlisted_on_the_topic
    assert found("another finding", {"pipeline_stack": {"AwsSolutions-S1": REASON}}) == unlisted_on_the_topic
    assert found("another module", {"stage": {FINDING: REASON}}) == unlisted_on_the_topic
    another_reason = {"pipeline_stack": {FINDING: "The file gives another reason."}}
    assert found("another reason", another_reason) == unlisted_on_the_topic
    assert found("listed", {"pipeline_stack": {FINDING: REASON}}) == []


@pytest.mark.parametrize(
    "written",
    [
        "AwsSolutions-SNS3",
        {},
        {RULES: {FINDING: REASON}},
        {RULES: [FINDING]},
        {RULES: [{"reason": REASON}]},
    ],
)
def test_metadata_that_is_not_a_list_of_findings_is_not_admitted(written: Any) -> None:
    templates = {"a.template.json": {"Resources": {"Topic": {"Type": "AWS::SNS::Topic", "Metadata": {KEY: written}}}}}
    created_by = {("a.template.json", "Topic"): "pipeline_stack"}
    listed = {"pipeline_stack": {"nag": {FINDING: REASON}}}
    assert violations(templates, created_by, listed) == [Finding("a.template.json", "Topic", UNREADABLE)]
