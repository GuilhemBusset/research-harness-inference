"""The pipeline's own steps, triggers and roles (system design, section 8.4, control 2).

A merge to `main` is deployment rights in the account, and the pipeline is what uses them. A
change to what the pipeline runs therefore shows up as a change to this protected file.

**The reviewed list**, `REVIEWED`, holds the pipeline's stages in order, each with its actions in
the order they run. An action is listed by its name and its type. Two kinds of action are not
listed, because every stack and every asset a later change adds to the stage brings its own:

- an asset-publishing action: a CodeBuild action whose project does nothing but publish assets
  with `cdk-assets`. It is left out in the asset stage, and so is that stage while it holds
  nothing else: CDK Pipelines creates it with the first asset;
- a stack's deployment action: a CloudFormation action that prepares or executes a change set, or
  creates and updates a stack.

Rules hold those two, and every action:

- an asset-publishing action is in the asset stage, and every action of that stage is a CodeBuild
  action. One that does more than publish assets is a step like any other, held by the list;
- a CloudFormation action comes after the approval on widened permissions of its stage: a manual
  approval that follows a CodeBuild action whose project runs `cdk diff --security-only` as a
  command of its build specification, the conditional CDK Pipelines writes
  (`ConfirmPermissionsBroadening`), and not text that only mentions it. A deployment made any
  other way is a step, held by the list, and so is a CloudFormation action that deletes a stack;
- the pipeline starts on a push to `main` and on nothing else: its source is the connection's, on
  that branch; a trigger filters pushes to that branch, and never pull requests or tags; and no
  EventBridge rule or schedule of any template targets a pipeline: not one of the pipeline's
  template by its reference, not one of another template by an import of the pipeline's
  export, and none by a CodePipeline ARN (section 8.2);
- no CodeBuild project has a webhook: on a public repository it would run a stranger's code with
  a role of the account (section 8.3);
- every role of every template carries the permissions boundary of the bounded bootstrap, by its
  fixed name, in the deploying account (`setup/bootstrap/README.md`).

The app has no pipeline yet, so the list is empty. The checks are shown on pipelines built here
with CDK Pipelines, against `BUILT`, the list of the pipeline `built` makes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

    Templates = Mapping[str, Mapping[str, Any]]
    # A stage and its listed actions, each as `(name, type)`.
    Reviewed = list[tuple[str, list[tuple[str, str]]]]
    # What a test does to the pipeline's stack and to the pipeline, given the CDK library.
    Change = Callable[[ModuleType, Any, Any], None]

# The reviewed list. Empty: the app has no pipeline.
REVIEWED: Reviewed = []

# The types of action the rules name, as `Category/Owner/Provider`.
CODEBUILD = "Build/AWS/CodeBuild"
CLOUDFORMATION = "Deploy/AWS/CloudFormation"
APPROVAL = "Approval/AWS/Manual"
CONNECTION = "Source/AWS/CodeStarSourceConnection"

# The name CDK Pipelines gives the stage that publishes assets.
ASSET_STAGE = "Assets"
# What a project that only publishes assets runs, as CDK Pipelines writes it.
PUBLISHES_ASSETS = re.compile(r'cdk-assets --path "[\w./-]+" --verbose publish "[\w:.-]+"')
INSTALLS_THE_PUBLISHER = re.compile(r"npm install -g cdk-assets@[\w.-]+")
# The modes of a CloudFormation action that only deploy a stack: the others delete one, or replace
# one that failed.
DEPLOYS = ("CHANGE_SET_REPLACE", "CHANGE_SET_EXECUTE", "CREATE_UPDATE")
# What the check of the approval on widened permissions runs, as CDK Pipelines writes it: the
# security diff, as a command of its own or as the condition that opens one.
SECURITY_DIFF = "cdk diff -a . --security-only --fail $STAGE_PATH/"
RUNS_THE_SECURITY_DIFF = re.compile(r"(if )?" + re.escape(SECURITY_DIFF) + r"\\\*(; then .*)?", re.DOTALL)
# What stands for a value only known when the stack is deployed, in a command read from a template.
DEPLOY_TIME = "<deploy-time value>"

BRANCH = "main"
# The boundary, as `Fn::Sub` would write its ARN: the fixed name, in the account that deploys.
BOUNDARY = "rhi-permissions-boundary"
BOUNDARY_ARN = re.compile(r"arn:(aws[a-z-]*|\$\{AWS::Partition\}):iam::\$\{AWS::AccountId\}:policy/" + BOUNDARY)

UNLISTED = "the pipeline has it and the reviewed list does not"
MISSING = "the reviewed list has it and the pipeline does not"
ANOTHER_ORDER = "the pipeline's stages and actions are not in the order of the reviewed list"
ASSETS_ELSEWHERE = "an asset-publishing action outside the asset stage"
NOT_CODEBUILD = "an action of the asset stage that is not a CodeBuild action"
NOT_AFTER_THE_APPROVAL = "a CloudFormation action that does not come after the approval on widened permissions"
ANOTHER_TRIGGER = "a trigger other than a push to `main`"
WEBHOOK = "a CodeBuild project with a webhook"
NO_BOUNDARY = "a role without the permissions boundary of the bounded bootstrap"


def resources(templates: Templates, kind: str) -> list[tuple[str, str, dict[str, Any], Mapping[str, Any]]]:
    """Every resource of one type, as `(template path, logical ID, properties, template)`."""
    return [
        (path, logical_id, resource.get("Properties", {}), template)
        for path, template in sorted(templates.items())
        for logical_id, resource in template.get("Resources", {}).items()
        if resource.get("Type") == kind
    ]


def type_of(action: Mapping[str, Any]) -> str:
    kind = action["ActionTypeId"]
    return f"{kind['Category']}/{kind['Owner']}/{kind['Provider']}"


def stages_of(pipeline: Mapping[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """The stages of a pipeline, each with its actions in the order they run."""
    return [
        (stage["Name"], sorted(stage["Actions"], key=lambda action: action.get("RunOrder", 1)))
        for stage in pipeline["Stages"]
    ]


def text_of(value: Any, deploy_time: str | None = None) -> str:
    """The text of a value of a template, without what is only known when the stack is deployed,
    or with `deploy_time` in its place."""
    if isinstance(value, dict) and list(value) == ["Fn::Join"]:
        parts = value["Fn::Join"][1]
        return value["Fn::Join"][0].join(
            part if isinstance(part, str) else deploy_time
            for part in parts
            if isinstance(part, str) or deploy_time is not None
        )
    return value if isinstance(value, str) else ""


def build_spec_of(action: Mapping[str, Any], template: Mapping[str, Any]) -> Any:
    """The build specification of the project a CodeBuild action runs, when the project is one of
    the same template, named by a `Ref`."""
    project = action.get("Configuration", {}).get("ProjectName")
    if type_of(action) != CODEBUILD or not isinstance(project, dict) or list(project) != ["Ref"]:
        return None
    resource = template.get("Resources", {}).get(project["Ref"], {})
    if resource.get("Type") != "AWS::CodeBuild::Project":
        return None
    return resource.get("Properties", {}).get("Source", {}).get("BuildSpec")


def commands_of(build_spec: Any) -> list[str] | None:
    """The commands of a build specification written as text, in every phase. None when it is not
    such a build specification."""
    try:
        phases = json.loads(build_spec)["phases"]
        commands = [
            command for phase in phases.values() for key in ("commands", "finally") for command in phase.get(key, [])
        ]
    except (TypeError, ValueError, KeyError, AttributeError):  # Not such a build specification.
        return None
    return commands if all(isinstance(command, str) for command in commands) else None


def publishes_assets(action: Mapping[str, Any], template: Mapping[str, Any]) -> bool:
    """Whether an action is a CodeBuild action whose project does nothing but publish assets:
    every command of its build specification installs `cdk-assets` or publishes with it."""
    commands = commands_of(build_spec_of(action, template))
    if commands is None:
        return False
    publishing = [command for command in commands if PUBLISHES_ASSETS.fullmatch(command)]
    return bool(publishing) and all(
        INSTALLS_THE_PUBLISHER.fullmatch(command) for command in commands if command not in publishing
    )


def deploys_a_stack(action: Mapping[str, Any]) -> bool:
    return type_of(action) == CLOUDFORMATION and action.get("Configuration", {}).get("ActionMode") in DEPLOYS


def steps(templates: Templates) -> Reviewed:
    """What the reviewed list holds of the synthesized pipeline: its stages in order, each with
    its actions other than asset-publishing and stack-deployment actions, in the order they run.
    A second pipeline would follow the first, and so be unlisted."""
    found = []
    for _, _, pipeline, template in resources(templates, "AWS::CodePipeline::Pipeline"):
        for stage, actions in stages_of(pipeline):
            listed = [
                (action["Name"], type_of(action))
                for action in actions
                if not deploys_a_stack(action) and not (stage == ASSET_STAGE and publishes_assets(action, template))
            ]
            if listed or stage != ASSET_STAGE:
                found.append((stage, listed))
    return found


def lines(reviewed: Reviewed) -> list[str]:
    """A list as one line for each stage and each action, for a difference to name."""
    return [
        line
        for stage, actions in reviewed
        for line in [f"stage {stage}", *(f"{stage}/{name}: {kind}" for name, kind in actions)]
    ]


def differences(templates: Templates, reviewed: Reviewed) -> list[tuple[str, str]]:
    """How the synthesized pipeline differs from a reviewed list, as `(what, the line)`; empty
    when it is that list."""
    found, expected = lines(steps(templates)), lines(reviewed)
    different = [(UNLISTED, line) for line in found if line not in expected]
    different += [(MISSING, line) for line in expected if line not in found]
    if not different and found != expected:
        different.append((ANOTHER_ORDER, ", ".join(found)))
    return different


def checks_permissions(action: Mapping[str, Any], template: Mapping[str, Any]) -> bool:
    """Whether an action is a CodeBuild action whose project runs the security diff: one command
    of its build specification is that diff, or opens with it as its condition."""
    commands = commands_of(text_of(build_spec_of(action, template), DEPLOY_TIME)) or []
    return any(RUNS_THE_SECURITY_DIFF.fullmatch(command) for command in commands)


def approval_on_widened_permissions(actions: list[dict[str, Any]], template: Mapping[str, Any]) -> int | None:
    """The run order of a stage's approval on widened permissions: a manual approval that follows
    a CodeBuild action of the stage whose project runs the security diff. None when the stage has
    none."""
    checked = [action.get("RunOrder", 1) for action in actions if checks_permissions(action, template)]
    approved = [
        action.get("RunOrder", 1)
        for action in actions
        if type_of(action) == APPROVAL and any(check < action.get("RunOrder", 1) for check in checked)
    ]
    return min(approved, default=None)


def is_a_push_to_main(trigger: Mapping[str, Any]) -> bool:
    """Whether a trigger of a pipeline filters pushes to `main` and nothing else."""
    git = trigger.get("GitConfiguration", {})
    return "PullRequest" not in git and all(
        "Tags" not in push and push.get("Branches", {}).get("Includes") == [BRANCH] for push in git.get("Push", [])
    )


def names(value: Any, logical_id: str) -> bool:
    """Whether a value of a template refers to a resource of that template."""
    if isinstance(value, dict):
        return any(names(part, logical_id) for part in value.values())
    if isinstance(value, list):
        return any(names(part, logical_id) for part in value)
    return isinstance(value, str) and (value == logical_id or "${" + logical_id in value)


def imports(value: Any, exports: list[Any]) -> bool:
    """Whether a value of a template imports one of the exports, by its name."""
    if isinstance(value, dict):
        return any(value.get("Fn::ImportValue") == name for name in exports) or any(
            imports(part, exports) for part in value.values()
        )
    return isinstance(value, list) and any(imports(part, exports) for part in value)


def mentions_a_pipeline(value: Any) -> bool:
    """Whether a value of a template holds text of a CodePipeline ARN."""
    if isinstance(value, dict):
        return any(mentions_a_pipeline(part) for part in value.values())
    if isinstance(value, list):
        return any(mentions_a_pipeline(part) for part in value)
    return isinstance(value, str) and ":codepipeline:" in value


def written(value: Any) -> str | None:
    """A value of a template as `Fn::Sub` would write it, with pseudo parameters as `${AWS::...}`.
    None when it is built from anything but text and pseudo parameters."""
    if isinstance(value, str):
        return None if "$" in value else value
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((function, argument),) = value.items()
    if function == "Ref" and isinstance(argument, str) and argument.startswith("AWS::"):
        return "${" + argument + "}"
    if function == "Fn::Sub" and isinstance(argument, str):
        return argument
    if function == "Fn::Join" and isinstance(argument, list) and len(argument) == 2 and isinstance(argument[1], list):
        parts = [written(part) for part in argument[1]]
        if isinstance(argument[0], str) and all(isinstance(part, str) for part in parts):
            return argument[0].join(part for part in parts if part is not None)
    return None


def breaches(templates: Templates) -> list[tuple[str, str, str]]:
    """Everything of the templates that a rule refuses, as `(template path, what it is named,
    what is refused)`: an action by its stage and name, a resource by its logical ID."""
    found = []
    for path, pipeline_id, pipeline, template in resources(templates, "AWS::CodePipeline::Pipeline"):
        for stage, actions in stages_of(pipeline):
            approved = approval_on_widened_permissions(actions, template)
            for action in actions:
                name = f"{stage}/{action['Name']}"
                if stage != ASSET_STAGE and publishes_assets(action, template):
                    found.append((path, name, ASSETS_ELSEWHERE))
                if stage == ASSET_STAGE and type_of(action) != CODEBUILD:
                    found.append((path, name, NOT_CODEBUILD))
                if type_of(action) == CLOUDFORMATION and (approved is None or action.get("RunOrder", 1) <= approved):
                    found.append((path, name, NOT_AFTER_THE_APPROVAL))
                on_main = type_of(action) == CONNECTION and action.get("Configuration", {}).get("BranchName") == BRANCH
                if action["ActionTypeId"]["Category"] == "Source" and not on_main:
                    found.append((path, name, ANOTHER_TRIGGER))
        if not all(is_a_push_to_main(trigger) for trigger in pipeline.get("Triggers", [])):
            found.append((path, pipeline_id, ANOTHER_TRIGGER))
    # What starts a pipeline from outside it, in any template: the targets of a rule, the target of
    # a schedule. A target names the pipeline of its own template by a reference, the pipeline of
    # another by importing what that template exports of it, and any pipeline by its ARN.
    pipelines = resources(templates, "AWS::CodePipeline::Pipeline")
    exports = [
        output["Export"]["Name"]
        for _, pipeline_id, _, template in pipelines
        for output in template.get("Outputs", {}).values()
        if "Name" in output.get("Export", {}) and names(output.get("Value"), pipeline_id)
    ]
    for kind, key in (("AWS::Events::Rule", "Targets"), ("AWS::Scheduler::Schedule", "Target")):
        for path, logical_id, properties, _ in resources(templates, kind):
            targets = properties.get(key)
            own = [pipeline_id for pipeline_path, pipeline_id, _, _ in pipelines if pipeline_path == path]
            if (
                any(names(targets, pipeline_id) for pipeline_id in own)
                or imports(targets, exports)
                or mentions_a_pipeline(targets)
            ):
                found.append((path, logical_id, ANOTHER_TRIGGER))
    for path, logical_id, properties, _ in resources(templates, "AWS::CodeBuild::Project"):
        # Only a project that says it has no webhook, and nothing more, has none.
        if properties.get("Triggers", {"Webhook": False}) != {"Webhook": False}:
            found.append((path, logical_id, WEBHOOK))
    for path, logical_id, properties, _ in resources(templates, "AWS::IAM::Role"):
        boundary = written(properties.get("PermissionsBoundary"))
        if boundary is None or BOUNDARY_ARN.fullmatch(boundary) is None:
            found.append((path, logical_id, NO_BOUNDARY))
    return found


def test_the_pipeline_is_the_reviewed_list(templates: dict[str, dict[str, Any]]) -> None:
    assert differences(templates, REVIEWED) == []


def test_the_pipeline_keeps_the_rules(templates: dict[str, dict[str, Any]]) -> None:
    assert breaches(templates) == []


# The reviewed list of the pipeline `built` makes, written by hand as `REVIEWED` is.
BUILT: Reviewed = [
    ("Source", [("owner_repo", CONNECTION)]),
    ("Build", [("Synth", CODEBUILD)]),
    ("UpdatePipeline", [("SelfMutate", CODEBUILD)]),
    ("Rhi", [("Check.Check", CODEBUILD), ("Check.Confirm", APPROVAL)]),
]
# A command as an asset-publishing project runs it.
PUBLISH = 'cdk-assets --path "assembly-Pipeline-Rhi/Other.assets.json" --verbose publish "0123abcd:current_account"'


def built(
    cdk: ModuleType,
    folder: Path,
    *,
    connection: bool = True,
    branch: str = BRANCH,
    boundary: str = BOUNDARY,
    approval: str = "first",
    step: tuple[str, list[str]] | None = None,
    stack_with_an_asset: str | None = None,
    change: Change | None = None,
) -> dict[str, dict[str, Any]]:
    """The templates of an app, built in the test, whose pipeline CDK Pipelines makes as the
    design describes it (section 8.2): a source on a connection, a synth step, the self-update,
    and one stage with one stack behind the approval on widened permissions. Every role carries
    the boundary.

    A test changes one thing:

    - the source: a CodeCommit repository in place of the connection, or another branch;
    - the boundary's name;
    - the approval: on widened permissions and `first`, the same and `last`, after the deployment,
      or `unchecked`, a manual approval that no check precedes;
    - one more step before the stage's deployment, by its name and its commands;
    - one more stack in the stage, with a file asset and a role under the boundary of the name
      given or, when the name is empty, under none;
    - anything `change` does to the pipeline's stack and to the pipeline once it is built.
    """
    pipelines, iam = cdk.pipelines, cdk.aws_iam
    folder.mkdir(parents=True, exist_ok=True)
    context = json.loads((Path(__file__).resolve().parents[2] / "cdk.json").read_text(encoding="utf-8"))["context"]
    app = cdk.App(outdir=str(folder / "cdk.out"), context=context, analytics_reporting=False)

    def bounded(scope: Any, name: str) -> None:
        policy = iam.ManagedPolicy.from_managed_policy_name(scope, "Boundary", name)
        iam.PermissionsBoundary.of(scope).apply(policy)

    stack = cdk.Stack(app, "Pipeline")
    bounded(stack, boundary)
    if connection:
        arn = cdk.aws_codeconnections.CfnConnection(stack, "Connection", connection_name="github").attr_connection_arn
        source = pipelines.CodePipelineSource.connection("owner/repo", branch, connection_arn=arn)
    else:
        # No rule starts the pipeline from this repository: only the source's kind is another.
        repository = cdk.aws_codecommit.Repository.from_repository_name(stack, "Repository", "repo")
        unwatched = cdk.aws_codepipeline_actions.CodeCommitTrigger.NONE
        source = pipelines.CodePipelineSource.code_commit(repository, branch, trigger=unwatched)
    synth = pipelines.ShellStep("Synth", input=source, commands=["true"])
    pipeline = pipelines.CodePipeline(stack, "Pipeline", synth=synth)

    stage = cdk.Stage(stack, "Rhi")
    endpoint = cdk.Stack(stage, "Endpoint")
    bounded(endpoint, boundary)
    iam.Role(endpoint, "Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    if stack_with_an_asset is not None:
        other = cdk.Stack(stage, "Other")
        (folder / "asset.txt").write_text("An asset.\n", encoding="utf-8")
        asset = cdk.aws_s3_assets.Asset(other, "Asset", path=str(folder / "asset.txt"))
        assumed_by = iam.ServicePrincipal("lambda.amazonaws.com")
        iam.Role(other, "Role", assumed_by=assumed_by, description=asset.s3_object_key)
        if stack_with_an_asset:
            bounded(other, stack_with_an_asset)

    if approval == "unchecked":
        gate = [pipelines.ManualApprovalStep("Approve")]
    else:
        gate = [pipelines.ConfirmPermissionsBroadening("Check", stage=stage)]
    more = [pipelines.ShellStep(step[0], commands=step[1])] if step is not None else []
    last = approval == "last"
    pipeline.add_stage(stage, pre=more if last else gate + more, post=gate if last else [])
    pipeline.build_pipeline()
    if change is not None:
        change(cdk, stack, pipeline.pipeline)

    outdir = Path(app.synth().directory)
    return {
        path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(outdir.rglob("*.template.json"))
    }


def refused(templates: Templates) -> set[str]:
    """What the rules refuse in the templates, without where."""
    return {what for _, _, what in breaches(templates)}


def test_detects_an_unlisted_action(cdk: ModuleType, tmp_path: Path) -> None:
    assert differences(built(cdk, tmp_path / "reviewed"), BUILT) == []

    one_more = built(cdk, tmp_path / "one more", step=("Extra", ["true"]))
    assert differences(one_more, BUILT) == [(UNLISTED, f"Rhi/Extra: {CODEBUILD}")]
    assert breaches(one_more) == []


def test_detects_a_missing_and_a_moved_action(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path / "reviewed")
    longer: Reviewed = [*BUILT, ("Verify", [("Smoke", CODEBUILD)])]
    assert differences(templates, longer) == [(MISSING, "stage Verify"), (MISSING, f"Verify/Smoke: {CODEBUILD}")]
    check, confirm = BUILT[3][1]
    assert [what for what, _ in differences(templates, [*BUILT[:3], ("Rhi", [confirm, check])])] == [ANOTHER_ORDER]

    # The order is the one the actions run in: this step starts with the check, before the approval.
    one_more = built(cdk, tmp_path / "one more", step=("Extra", ["true"]))
    extra = ("Extra", CODEBUILD)
    assert differences(one_more, [*BUILT[:3], ("Rhi", [check, extra, confirm])]) == []
    assert [what for what, _ in differences(one_more, [*BUILT[:3], ("Rhi", [check, confirm, extra])])] == [
        ANOTHER_ORDER
    ]


def test_a_new_stack_or_asset_needs_no_list_change(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path / "bounded", stack_with_an_asset=BOUNDARY)
    # The pipeline gained the asset stage with one action, and the new stack's two.
    (pipeline,) = [properties for _, _, properties, _ in resources(templates, "AWS::CodePipeline::Pipeline")]
    actions = {stage: [action["Name"] for action in actions] for stage, actions in stages_of(pipeline)}
    assert len(actions[ASSET_STAGE]) == 1
    assert {"Other.Prepare", "Other.Deploy"} < set(actions["Rhi"])
    assert differences(templates, BUILT) == []
    assert breaches(templates) == []

    unbounded = built(cdk, tmp_path / "unbounded", stack_with_an_asset="")
    assert differences(unbounded, BUILT) == []
    ((path, logical_id, what),) = breaches(unbounded)
    assert what == NO_BOUNDARY
    assert "Other" in path
    assert unbounded[path]["Resources"][logical_id]["Type"] == "AWS::IAM::Role"


def more_than_publishing(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
    codebuild = cdk.aws_codebuild
    spec = codebuild.BuildSpec.from_object({"version": "0.2", "phases": {"build": {"commands": [PUBLISH, "true"]}}})
    project = codebuild.PipelineProject(stack, "Extra", build_spec=spec)
    synthesized = cdk.aws_codepipeline.Artifact("Synth_Output")
    action = cdk.aws_codepipeline_actions.CodeBuildAction(action_name="Extra", project=project, input=synthesized)
    pipeline.stage(ASSET_STAGE).add_action(action)


def a_deletion(cdk: ModuleType, _stack: Any, pipeline: Any) -> None:
    delete = cdk.aws_codepipeline_actions.CloudFormationDeleteStackAction
    action = delete(action_name="Delete", stack_name="Rhi-Other", admin_permissions=False, run_order=9)
    pipeline.stage("Rhi").add_action(action)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(
            more_than_publishing,
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Extra: {CODEBUILD}"],
            id="a project of the asset stage that does more than publish assets",
        ),
        pytest.param(a_deletion, [f"Rhi/Delete: {CLOUDFORMATION}"], id="a CloudFormation action that deletes a stack"),
    ],
)
def test_only_publishing_and_deploying_are_left_out(
    cdk: ModuleType, tmp_path: Path, change: Change, expected: list[str]
) -> None:
    templates = built(cdk, tmp_path, stack_with_an_asset=BOUNDARY, change=change)
    assert differences(templates, BUILT) == [(UNLISTED, line) for line in expected]


def with_a_trigger(*, push: dict[str, Any], pull_request: dict[str, Any] | None = None) -> Change:
    """Gives the pipeline one trigger, with one filter on pushes and, when asked, one on opened
    pull requests."""

    def change(cdk: ModuleType, _stack: Any, pipeline: Any) -> None:
        codepipeline = cdk.aws_codepipeline
        opened = [codepipeline.GitPullRequestEvent.OPEN]
        git = codepipeline.GitConfiguration(
            source_action=pipeline.stages[0].actions[0],
            push_filter=[codepipeline.GitPushFilter(**push)],
            pull_request_filter=pull_request and [codepipeline.GitPullRequestFilter(events=opened, **pull_request)],
        )
        pipeline.add_trigger(provider_type=codepipeline.ProviderType.CODE_STAR_SOURCE_CONNECTION, git_configuration=git)

    return change


def a_rule_every_night(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
    events = cdk.aws_events
    nightly = events.Schedule.rate(cdk.Duration.days(1))
    events.Rule(stack, "Nightly", schedule=nightly, targets=[cdk.aws_events_targets.CodePipeline(pipeline)])


def a_schedule_every_night(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
    scheduler = cdk.aws_scheduler
    nightly = scheduler.ScheduleExpression.rate(cdk.Duration.days(1))
    target = cdk.aws_scheduler_targets.CodePipelineStartPipelineExecution(pipeline)
    scheduler.Schedule(stack, "Nightly", schedule=nightly, target=target)


def in_another_stack(starts: Change) -> Change:
    """Makes the same change in a stack of its own, beside the pipeline's and under the boundary,
    that reaches the pipeline across the stacks."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        other = cdk.Stack(stack.node.scope, "Nightly")
        boundary = cdk.aws_iam.ManagedPolicy.from_managed_policy_name(other, "Boundary", BOUNDARY)
        cdk.aws_iam.PermissionsBoundary.of(other).apply(boundary)
        starts(cdk, other, pipeline)

    return change


def a_check_that_only_prints_the_diff(cdk: ModuleType, stack: Any, _pipeline: Any) -> None:
    """Replaces the commands of the check on widened permissions by one that prints its diff."""
    (check,) = [
        node
        for node in stack.node.find_all()
        if isinstance(node, cdk.aws_codebuild.CfnProject) and "SecurityCheck" in node.node.path
    ]
    printed = {"version": "0.2", "phases": {"build": {"commands": [f"echo '{SECURITY_DIFF}\\*'"]}}}
    check.add_property_override("Source.BuildSpec", json.dumps(printed))


def a_project_with_a_webhook(cdk: ModuleType, stack: Any, _pipeline: Any) -> None:
    codebuild = cdk.aws_codebuild
    opened = codebuild.FilterGroup.in_event_of(codebuild.EventAction.PULL_REQUEST_CREATED)
    source = codebuild.Source.git_hub(owner="owner", repo="repo", webhook=True, webhook_filters=[opened])
    codebuild.Project(stack, "OnAPullRequest", source=source, build_spec=codebuild.BuildSpec.from_object({}))


def an_approval_in_the_asset_stage(cdk: ModuleType, _stack: Any, pipeline: Any) -> None:
    pipeline.stage(ASSET_STAGE).add_action(cdk.aws_codepipeline_actions.ManualApprovalAction(action_name="Extra"))


ON_MAIN = {"branches_includes": [BRANCH]}


def test_keeps_a_trigger_on_a_push_to_main(cdk: ModuleType, tmp_path: Path) -> None:
    assert breaches(built(cdk, tmp_path, change=with_a_trigger(push=ON_MAIN))) == []


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            {"change": with_a_trigger(push=ON_MAIN, pull_request=ON_MAIN)},
            ANOTHER_TRIGGER,
            id="a trigger on a pull request",
        ),
        pytest.param({"change": a_project_with_a_webhook}, WEBHOOK, id="a CodeBuild project with a webhook"),
        pytest.param(
            {"step": ("Publish", [PUBLISH])}, ASSETS_ELSEWHERE, id="an asset-publishing action outside the asset stage"
        ),
        pytest.param(
            {"approval": "last"}, NOT_AFTER_THE_APPROVAL, id="a deployment before the approval on widened permissions"
        ),
        # The clauses of the rules that the four above leave out.
        pytest.param({"approval": "unchecked"}, NOT_AFTER_THE_APPROVAL, id="a deployment after another approval"),
        pytest.param({"connection": False}, ANOTHER_TRIGGER, id="a source that is not the connection's"),
        pytest.param({"branch": "develop"}, ANOTHER_TRIGGER, id="a source on another branch"),
        pytest.param(
            {"change": with_a_trigger(push={"branches_includes": [BRANCH, "develop"]})},
            ANOTHER_TRIGGER,
            id="a trigger on a push to another branch",
        ),
        pytest.param(
            {"change": with_a_trigger(push=ON_MAIN | {"tags_includes": ["v*"]})},
            ANOTHER_TRIGGER,
            id="a trigger on a tag",
        ),
        pytest.param({"change": a_rule_every_night}, ANOTHER_TRIGGER, id="a rule that starts the pipeline"),
        pytest.param({"change": a_schedule_every_night}, ANOTHER_TRIGGER, id="a schedule that starts the pipeline"),
        pytest.param(
            {"change": in_another_stack(a_rule_every_night)},
            ANOTHER_TRIGGER,
            id="a rule of another stack that starts the pipeline",
        ),
        pytest.param(
            {"change": in_another_stack(a_schedule_every_night)},
            ANOTHER_TRIGGER,
            id="a schedule of another stack that starts the pipeline",
        ),
        pytest.param(
            {"change": a_check_that_only_prints_the_diff},
            NOT_AFTER_THE_APPROVAL,
            id="a deployment after a check that only prints the diff",
        ),
        pytest.param(
            {"stack_with_an_asset": BOUNDARY, "change": an_approval_in_the_asset_stage},
            NOT_CODEBUILD,
            id="an action of the asset stage that is not a CodeBuild action",
        ),
        pytest.param({"boundary": "another-boundary"}, NO_BOUNDARY, id="a boundary of another name"),
    ],
)
def test_each_rule_refuses_its_breach(cdk: ModuleType, tmp_path: Path, changes: dict[str, Any], expected: str) -> None:
    assert refused(built(cdk, tmp_path / "kept")) == set()
    assert refused(built(cdk, tmp_path / "breached", **changes)) == {expected}
