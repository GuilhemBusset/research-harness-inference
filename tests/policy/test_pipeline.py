"""The pipeline's own steps, triggers and roles (system design, section 8.4, control 2).

A merge to `main` is deployment rights in the account, and the pipeline is what uses them. A
change to what the pipeline runs therefore shows up as a change to this protected file.

**The reviewed list**, `REVIEWED`, holds the pipeline's stages in order, each with its actions in
the order they run. An action is listed by its name and its type. Two kinds of action are not
listed, because every stack and every asset a later change adds to the stage brings its own:

- an asset-publishing action: a CodeBuild action whose project does nothing but publish assets
  with `cdk-assets`, after installing, to build an image for linux/arm64 as well as linux/amd64,
  an arm64 emulator and a multi-platform builder, each pinned by its digest or by its version and
  checksum (`INSTALLS_THE_IMAGE_BUILDER`). It is left out in the asset stage, and so is that stage
  while it holds nothing else: CDK Pipelines creates it with the first asset;
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
  fixed name, in the deploying account (`setup/bootstrap/README.md`);
- the role of the synth step, the project of a CodeBuild action of the stage `Build`, may read the
  parameters under /rhi/config/ and no other parameter or secret (section 8.2, step 2): every
  statement it is allowed whose actions reach a read of a parameter or a secret, or assume another
  role, which could read them in its place, names only parameters under that path, in the
  deploying account. A statement written as `NotAction` or `NotResource`, and a managed policy the
  template does not hold, are taken as reaching everything;
- the role of a function that an EventBridge rule of a pipeline's template invokes, the reporter
  (section 8.2, step 7), may read the reporter's one secret, `rhi/reporter/github-app`; read and
  list the executions of the template's own pipeline, and of no other pipeline; the events of the
  stacks that pipeline deploys, by the names its CloudFormation actions give them, and of no other
  stack; the objects of the pipeline's artifact bucket, where each execution's synthesized
  templates are, and the marks of its verification under `verification/`; list that bucket, under
  the condition that the listed prefix is under `verification/` (`LISTS_THE_MARKS`), alone, so it
  is shown the marks' keys and no other; publish to the owner's topic, the one topic of the
  template that the function's `RHI_REPORTER_TOPIC` names, and to no other; and write to the
  function's own log group. Nothing else: every action it is allowed is one of `REPORTER_READS`,
  each on resources of its kind only, and a wildcard action, `NotAction`, `NotResource` and a
  managed policy the template does not hold are taken as reaching more;
- the role of the verification, the project of a CodeBuild action named `Verify` (section 8.2,
  step 6), may read the canary's one secret, `rhi/canary-key`; query the workspace of the stage
  the action is in: the workspaces of the deploying account and region, under the condition that
  names the tag `rhi-stage` and a value that a workspace of a stack that stage deploys carries,
  and no workspace of any other template; read the release parameters, `/rhi/pools/<pool>/release`,
  that the stacks that stage deploys hold, each by its name; and write objects under
  `verification/` of the pipeline's artifact bucket. Beside those it holds what CDK Pipelines gives
  every project: a read of the artifact bucket, and its own log group and report groups. Nothing
  else: every action it is allowed is one of `VERIFICATION_MAY`, each on resources of its kind
  only, read as the reporter's are;
- no role of any template but the reporter's may read the reporter's secret (section 8.4): the
  App's key is the only GitHub credential in the account. A statement reaches the secret when one
  of its actions matches a read of a secret (`SECRET_READS`) and one of its resources may name the
  secret, in any partition, region and account and whatever six characters Secrets Manager adds
  to its name, a wildcard and a value of the template read as matching anything; `NotAction` and
  `NotResource` reach everything. A managed policy the template does not hold is taken as
  reaching it too;
- no role may both put events on the measurement bus, `rhi-measurements`, and read that secret: a
  job sends the reporter its numbers because it must not hold the credential (section 12). A
  statement reaches the bus when one of its actions matches `events:PutEvents` and one of its
  resources may name the bus, in any partition, region and account, read as above.

The list is that of the app's pipeline, `src/rhi/pipeline/stack.py`. The checks are shown on
pipelines built here with CDK Pipelines, against `BUILT`, the list of the pipeline `built` makes.
"""

from __future__ import annotations

import fnmatch
import functools
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
    # What a statement of a reporter's role names, given the same.
    Names = Callable[[ModuleType, Any, Any], str]

# The reviewed list: the pipeline of `src/rhi/pipeline/stack.py`.
REVIEWED: Reviewed = [
    ("Source", [("GitHub", "Source/AWS/CodeStarSourceConnection")]),
    ("Build", [("Synth", "Build/AWS/CodeBuild")]),
    ("UpdatePipeline", [("SelfMutate", "Build/AWS/CodeBuild")]),
    (
        "Rhi",
        [
            ("Check.Check", "Build/AWS/CodeBuild"),
            ("Check.Confirm", "Approval/AWS/Manual"),
            # After the stage's deployment actions, which the list leaves out.
            ("Verify", "Build/AWS/CodeBuild"),
        ],
    ),
]

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
# What a project that publishes images may also run, as `src/rhi/pipeline/stack.py` writes it: an
# arm64 emulator and a builder for either platform, each image by its digest and the builder's
# download by its version, checked against its checksum (sections 4.2 and 8.2, step 4).
DIGEST = r"(:[\w.-]+)?@sha256:[0-9a-f]{64}"
BUILDX = r"\$HOME/\.docker/cli-plugins/docker-buildx"
INSTALLS_THE_IMAGE_BUILDER = [
    re.compile(rf"docker run --privileged --rm tonistiigi/binfmt{DIGEST} --install arm64"),
    re.compile(
        rf'curl -fsSL --create-dirs -o "{BUILDX}" '
        r"https://github\.com/docker/buildx/releases/download/v(\d+\.\d+\.\d+)/buildx-v\1\.linux-amd64"
    ),
    re.compile(rf'echo "[0-9a-f]{{64}}  {BUILDX}" \| sha256sum -c -'),
    re.compile(rf'chmod \+x "{BUILDX}"'),
    re.compile(
        rf"docker buildx create --name [\w-]+ --driver docker-container --driver-opt image=moby/buildkit{DIGEST}"
        r" --driver-opt default-load=true --bootstrap --use"
    ),
]
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

# What the synth role may read: the configuration's parameters, by path or by name, in the deploying
# account, as `Fn::Sub` would write their ARNs.
SYNTH_STAGE = "Build"
CONFIGURATION = re.compile(
    r"arn:(aws[a-z-]*|\$\{AWS::Partition\}):ssm:(\$\{AWS::Region\}|[a-z0-9-]+|\*):\$\{AWS::AccountId\}:"
    r"parameter/rhi/config(/[\w./*-]*)?"
)
# The actions that read a parameter or a secret, or assume a role that could read them.
READS = (
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "ssm:GetParameterHistory",
    "secretsmanager:GetSecretValue",
    "secretsmanager:BatchGetSecretValue",
    "sts:AssumeRole",
)

# The reporter's one secret, which the owner creates (section 8.6, step 3), and the actions its role
# may be allowed, each with the kind of resource it may be allowed on.
REPORTER_SECRET = "rhi/reporter/github-app"
REPORTER_READS = {
    "secretsmanager:GetSecretValue": "secret",
    "codepipeline:GetPipelineExecution": "pipeline",
    "codepipeline:GetPipelineState": "pipeline",
    "codepipeline:ListActionExecutions": "pipeline",
    "codepipeline:ListPipelineExecutions": "pipeline",
    "cloudformation:DescribeStackEvents": "stacks",
    "s3:GetObject": "artifacts",
    "s3:ListBucket": "marks",
    "sns:Publish": "topic",
    "logs:CreateLogStream": "logs",
    "logs:PutLogEvents": "logs",
}
# The kind of resource a listing is allowed on, the artifact bucket itself, and the one condition
# such a statement carries: the listed prefix is under the verification's.
MARKS = "marks"
LISTS_THE_MARKS = {"StringLike": {"s3:prefix": "verification/*"}}
# The verification: the name of its action, the canary's secret, the prefix it writes under, the tag
# its stage's workspace is named by, the name of a pool's release parameter, and the actions its
# role may be allowed, each with the kind of resource it may be allowed on. The last three groups
# are what CDK Pipelines gives every project of the pipeline.
VERIFY_ACTION = "Verify"
CANARY_SECRET = "rhi/canary-key"
VERIFICATION_PREFIX = "verification"
STAGE_TAG = "rhi-stage"
RELEASE_PARAMETER = re.compile(r"/rhi/pools/[a-z0-9-]+/release")
VERIFICATION_MAY = {
    "secretsmanager:GetSecretValue": "secret",
    "aps:QueryMetrics": "workspaces",
    "ssm:GetParameter": "parameters",
    "s3:PutObject": "verification",
    "s3:GetBucket*": "artifacts",
    "s3:GetObject*": "artifacts",
    "s3:List*": "artifacts",
    "logs:CreateLogGroup": "logs",
    "logs:CreateLogStream": "logs",
    "logs:PutLogEvents": "logs",
    "codebuild:BatchPutCodeCoverages": "reports",
    "codebuild:BatchPutTestCases": "reports",
    "codebuild:CreateReport": "reports",
    "codebuild:CreateReportGroup": "reports",
    "codebuild:UpdateReport": "reports",
}
# What reads a secret, and every ARN of the reporter's secret and of the measurement bus, as patterns
# that a resource of a statement must overlap to reach them (`overlaps`): any partition, region and
# account, and the six characters Secrets Manager chooses and adds to a secret's name. `FIELD` stands
# for one field of an ARN, any run of characters without a colon.
FIELD = "\x00"
SECRET_READS = ("secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue")
SECRET_ARN = f"arn:{FIELD}:secretsmanager:{FIELD}:{FIELD}:secret:{REPORTER_SECRET}-??????"
MEASUREMENT_BUS = "rhi-measurements"
# The AWS managed policy CDK gives the functions it adds itself, CDK Pipelines' approval among them:
# it allows creating a log group and writing logs, and nothing else.
WRITES_LOGS_ONLY = re.compile(
    r"arn:(aws[a-z-]*|\$\{AWS::Partition\}):iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)
PUTS_EVENTS = ("events:PutEvents",)
BUS_ARN = f"arn:{FIELD}:events:{FIELD}:{FIELD}:event-bus/{MEASUREMENT_BUS}"
PARTITION = r"arn:(aws[a-z-]*|\$\{AWS::Partition\})"
HERE = r"\$\{AWS::Region\}:\$\{AWS::AccountId\}"
# The pseudo parameters that fill one field of an ARN.
PSEUDO_FIELD = re.compile(r"\$\{AWS::(Partition|Region|AccountId|URLSuffix)\}")

UNLISTED = "the pipeline has it and the reviewed list does not"
MISSING = "the reviewed list has it and the pipeline does not"
ANOTHER_ORDER = "the pipeline's stages and actions are not in the order of the reviewed list"
ASSETS_ELSEWHERE = "an asset-publishing action outside the asset stage"
NOT_CODEBUILD = "an action of the asset stage that is not a CodeBuild action"
NOT_AFTER_THE_APPROVAL = "a CloudFormation action that does not come after the approval on widened permissions"
ANOTHER_TRIGGER = "a trigger other than a push to `main`"
WEBHOOK = "a CodeBuild project with a webhook"
NO_BOUNDARY = "a role without the permissions boundary of the bounded bootstrap"
READS_MORE = "a synth role that may read more than the parameters under /rhi/config/"
REPORTS_MORE = (
    "a reporter's role that may do more than read its secret, its pipeline's records, templates and"
    " verification marks, and publish to its own topic"
)
VERIFIES_MORE = (
    "a verification's role that may do more than read the canary's secret, query its stage's workspace,"
    " read its stage's release parameters and write under verification/"
)
SECRET_ELSEWHERE = "a role other than the reporter function's that may read the reporter's secret"
MEASURES_AND_READS = "a role that may put events on the measurement bus and read the reporter's secret"
# The variable that names the reporter's topic to its function.
REPORTER_TOPIC = "RHI_REPORTER_TOPIC"


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
    every command of its build specification installs `cdk-assets`, publishes with it, or
    installs the pinned emulator and builder an image is built with."""
    commands = commands_of(build_spec_of(action, template))
    if commands is None:
        return False
    publishing = [command for command in commands if PUBLISHES_ASSETS.fullmatch(command)]
    installs = [INSTALLS_THE_PUBLISHER, *INSTALLS_THE_IMAGE_BUILDER]
    return bool(publishing) and all(
        any(install.fullmatch(command) for install in installs) for command in commands if command not in publishing
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


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def synth_roles(template: Mapping[str, Any]) -> set[str]:
    """The logical IDs of the roles of the projects that the CodeBuild actions of the stage `Build`
    run, in the pipelines of a template."""
    found = set()
    for _, _, pipeline, _ in resources({"": template}, "AWS::CodePipeline::Pipeline"):
        for stage, actions in stages_of(pipeline):
            for action in actions:
                project = action.get("Configuration", {}).get("ProjectName")
                if stage != SYNTH_STAGE or type_of(action) != CODEBUILD or not isinstance(project, dict):
                    continue
                role = template["Resources"].get(project.get("Ref"), {}).get("Properties", {}).get("ServiceRole")
                if isinstance(role, dict) and "Fn::GetAtt" in role:
                    found.add(role["Fn::GetAtt"][0])
    return found


def statements_of(
    template: Mapping[str, Any], role: str, *, known: re.Pattern[str] | None = None
) -> list[Mapping[str, Any]] | None:
    """Every statement a role of the template is allowed: those of the policies attached to it and
    of its own. None when it holds a managed policy the template does not, other than those whose
    ARN `known` matches, whose statements the caller knows."""
    properties = template["Resources"][role].get("Properties", {})
    held = {logical_id for _, logical_id, _, _ in resources({"": template}, "AWS::IAM::ManagedPolicy")}
    documents = [policy["PolicyDocument"] for policy in properties.get("Policies", [])]
    for arn in properties.get("ManagedPolicyArns", []):
        if known is not None and known.fullmatch(written(arn) or ""):
            continue
        if not (isinstance(arn, dict) and arn.get("Ref") in held):
            return None
    for kind in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for _, logical_id, policy, _ in resources({"": template}, kind):
            attached = {"Ref": role} in policy.get("Roles", []) or {"Ref": logical_id} in properties.get(
                "ManagedPolicyArns", []
            )
            if attached:
                documents.append(policy["PolicyDocument"])
    return [
        statement
        for document in documents
        for statement in as_list(document.get("Statement", []))
        if statement.get("Effect") == "Allow"
    ]


def reads_more(statement: Mapping[str, Any]) -> bool:
    """Whether an allowed statement reaches a read of a parameter or a secret, or a role, beyond
    the parameters under /rhi/config/."""
    if "NotAction" in statement or "NotResource" in statement:
        return True
    reaches = any(
        fnmatch.fnmatchcase(read.lower(), action.lower())
        for action in as_list(statement.get("Action", []))
        for read in READS
    )
    if not reaches:
        return False
    named = [written(resource) for resource in as_list(statement.get("Resource", []))]
    return not named or not all(arn is not None and CONFIGURATION.fullmatch(arn) for arn in named)


def arn_text(value: Any) -> str | None:
    """A resource of a statement as `Fn::Sub` would write it: a reference as `${Name}`, an
    attribute as `${Name.Attribute}`. None when it is built any other way."""
    if isinstance(value, str):
        return None if "$" in value else value
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((function, argument),) = value.items()
    if function == "Ref" and isinstance(argument, str):
        return "${" + argument + "}"
    if function == "Fn::GetAtt" and isinstance(argument, list) and all(isinstance(part, str) for part in argument):
        return "${" + ".".join(argument) + "}"
    if function == "Fn::Sub" and isinstance(argument, str):
        return argument
    if function == "Fn::Join" and isinstance(argument, list) and len(argument) == 2 and isinstance(argument[1], list):
        parts = [arn_text(part) for part in argument[1]]
        if isinstance(argument[0], str) and all(isinstance(part, str) for part in parts):
            return argument[0].join(part for part in parts if part is not None)
    return None


def overlaps(one: str, other: str) -> bool:
    """Whether some text matches both patterns, where `*` stands for any run of characters, `FIELD`
    for any run without a colon and `?` for any one character, as IAM reads a resource."""
    runs = {"*", FIELD}

    def absorbs(run: str, char: str) -> bool:
        return run == "*" or char != ":"

    @functools.cache
    def rest(i: int, j: int) -> bool:
        if i == len(one) or j == len(other):
            return set(one[i:] + other[j:]) <= runs
        a, b = one[i], other[j]
        if a in runs:
            return rest(i + 1, j) or ((b in runs or absorbs(a, b)) and rest(i, j + 1))
        if b in runs:
            return rest(i, j + 1) or (absorbs(b, a) and rest(i + 1, j))
        return (a == b or "?" in (a, b)) and rest(i + 1, j + 1)

    return rest(0, 0)


def reaches(statement: Mapping[str, Any], actions: tuple[str, ...], arn: str) -> bool:
    """Whether an allowed statement reaches one of `actions` on a resource of an ARN the pattern
    `arn` matches. A value of the template in a resource, a reference or a pseudo parameter,
    matches anything there, and a resource that cannot be read as text matches everything."""
    if "NotAction" in statement or "NotResource" in statement:
        return True
    if not any(
        isinstance(action, str) and fnmatch.fnmatchcase(wanted.lower(), action.lower())
        for action in as_list(statement.get("Action", []))
        for wanted in actions
    ):
        return False
    for resource in as_list(statement.get("Resource", [])):
        text = arn_text(resource)
        if text is None or overlaps(arn, re.sub(r"\$\{[^}]*\}", "*", PSEUDO_FIELD.sub(FIELD, text))):
            return True
    return False


def may(template: Mapping[str, Any], role: str, actions: tuple[str, ...], arn: str) -> bool:
    """Whether a role of the template may do one of `actions` on `arn`: a managed policy the
    template does not hold, but the one that only writes a function's logs, is taken as allowing it."""
    statements = statements_of(template, role, known=WRITES_LOGS_ONLY)
    return statements is None or any(reaches(statement, actions, arn) for statement in statements)


def reporter_roles(template: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """The functions of a template that its EventBridge rules invoke, as `(function, its role)`,
    the role None when the template does not hold it. A function two rules invoke is named once."""
    functions = {
        logical_id: properties for _, logical_id, properties, _ in resources({"": template}, "AWS::Lambda::Function")
    }
    found = []
    for _, _, rule, _ in resources({"": template}, "AWS::Events::Rule"):
        for target in as_list(rule.get("Targets", [])):
            arn = target.get("Arn") if isinstance(target, dict) else None
            function = arn["Fn::GetAtt"][0] if isinstance(arn, dict) and "Fn::GetAtt" in arn else None
            if function not in functions:
                continue
            role = functions[function].get("Role")
            held = isinstance(role, dict) and "Fn::GetAtt" in role and role["Fn::GetAtt"][0] in template["Resources"]
            if (function, role["Fn::GetAtt"][0] if held else None) not in found:
                found.append((function, role["Fn::GetAtt"][0] if held else None))
    return found


def reporter_resources(template: Mapping[str, Any], function: str) -> dict[str, list[re.Pattern[str]]]:
    """For each kind of resource of `REPORTER_READS`, what the reporter's role may name, as
    `arn_text` writes it: the secret, the template's own pipelines, the stacks they deploy, their
    artifact buckets' objects, those buckets themselves for a listing of the marks, the topic of
    the template that the function's `REPORTER_TOPIC` names, and the function's own log group."""
    pipelines = resources({"": template}, "AWS::CodePipeline::Pipeline")
    stacks = {
        action.get("Configuration", {}).get("StackName")
        for _, _, pipeline, _ in pipelines
        for _, actions in stages_of(pipeline)
        for action in actions
        if type_of(action) == CLOUDFORMATION
    }
    stores = [
        store.get("Location")
        for _, _, pipeline, _ in pipelines
        for store in [pipeline.get("ArtifactStore", {})]
        + [entry.get("ArtifactStore", {}) for entry in pipeline.get("ArtifactStores", [])]
    ]
    properties = template["Resources"][function].get("Properties", {})
    log_group = properties.get("LoggingConfig", {}).get("LogGroup")
    own_logs = log_group.get("Ref") if isinstance(log_group, dict) else None
    named_topic = properties.get("Environment", {}).get("Variables", {}).get(REPORTER_TOPIC)
    topics = {logical_id for _, logical_id, _, _ in resources({"": template}, "AWS::SNS::Topic")}
    own_topic = named_topic.get("Ref") if isinstance(named_topic, dict) else None
    return {
        "secret": [re.compile(rf"{PARTITION}:secretsmanager:{HERE}:secret:{re.escape(REPORTER_SECRET)}-\?{{6}}")],
        "pipeline": [
            re.compile(rf"{PARTITION}:codepipeline:{HERE}:\$\{{{re.escape(pipeline_id)}\}}")
            for _, pipeline_id, _, _ in pipelines
        ],
        "stacks": [
            re.compile(rf"{PARTITION}:cloudformation:{HERE}:stack/{re.escape(name)}/\*")
            for name in sorted(name for name in stacks if isinstance(name, str))
        ],
        "artifacts": [
            re.compile(rf"\$\{{{re.escape(store['Ref'])}\.Arn\}}/\S*")
            for store in stores
            if isinstance(store, dict) and isinstance(store.get("Ref"), str)
        ],
        MARKS: [
            re.compile(rf"\$\{{{re.escape(store['Ref'])}\.Arn\}}")
            for store in stores
            if isinstance(store, dict) and isinstance(store.get("Ref"), str)
        ],
        "topic": [re.compile(rf"\$\{{{re.escape(own_topic)}\}}")] if own_topic in topics else [],
        "logs": [re.compile(rf"\$\{{{re.escape(own_logs)}\.Arn\}}")] if isinstance(own_logs, str) else [],
    }


def reports_more(
    statement: Mapping[str, Any],
    allowed: Mapping[str, list[re.Pattern[str]]],
    may: Mapping[str, str] = REPORTER_READS,
) -> bool:
    """Whether an allowed statement of the reporter's role reaches an action it is not given, or
    a resource beyond those of the action's kind, or lists a bucket under any condition but
    `LISTS_THE_MARKS`. `may` is the role's actions, each with its kind: the reporter's, or another
    role's held the same way."""
    if "NotAction" in statement or "NotResource" in statement:
        return True
    reads = {action.lower(): kind for action, kind in may.items()}
    named = [arn_text(resource) for resource in as_list(statement.get("Resource", []))]
    for action in as_list(statement.get("Action", [])):
        kind = reads.get(action.lower()) if isinstance(action, str) else None
        if kind is None or not named:
            return True
        if kind == MARKS and statement.get("Condition") != LISTS_THE_MARKS:
            return True
        if not all(arn is not None and any(shape.fullmatch(arn) for shape in allowed[kind]) for arn in named):
            return True
    return False


def verifications(template: Mapping[str, Any]) -> list[tuple[list[dict[str, Any]], Mapping[str, Any], str, str | None]]:
    """The CodeBuild actions named `Verify` of the pipelines of a template, each as `(the actions
    of its stage, its pipeline, what names it, its project's role)`: named by its project when the
    template holds it, by its stage and name otherwise, and the role None when the template does
    not hold it."""
    held = template.get("Resources", {})
    found: list[tuple[list[dict[str, Any]], Mapping[str, Any], str, str | None]] = []
    for _, _, pipeline, _ in resources({"": template}, "AWS::CodePipeline::Pipeline"):
        for stage, actions in stages_of(pipeline):
            for action in actions:
                if action["Name"] != VERIFY_ACTION or type_of(action) != CODEBUILD:
                    continue
                named = action.get("Configuration", {}).get("ProjectName")
                project = named.get("Ref") if isinstance(named, dict) else None
                if not isinstance(project, str) or held.get(project, {}).get("Type") != "AWS::CodeBuild::Project":
                    found.append((actions, pipeline, f"{stage}/{action['Name']}", None))
                    continue
                role = held[project].get("Properties", {}).get("ServiceRole")
                role_id = role["Fn::GetAtt"][0] if isinstance(role, dict) and "Fn::GetAtt" in role else None
                held_role = role_id if isinstance(role_id, str) and role_id in held else None
                found.append((actions, pipeline, project, held_role))
    return found


def deployed_by(actions: list[dict[str, Any]], templates: Templates) -> list[Mapping[str, Any]]:
    """The templates of the stacks a stage's CloudFormation actions deploy, among `templates`: an
    action names its template by its path in the synth step's output."""
    paths = {
        str(action.get("Configuration", {}).get("TemplatePath", "")).partition("::")[2]
        for action in actions
        if type_of(action) == CLOUDFORMATION
    }
    return [template for path, template in sorted(templates.items()) if path in paths]


def workspace_tags(template: Mapping[str, Any]) -> set[str]:
    """The values of the tag `rhi-stage` on the workspaces of a template."""
    return {
        tag.get("Value")
        for _, _, workspace, _ in resources({"": template}, "AWS::APS::Workspace")
        for tag in as_list(workspace.get("Tags", []))
        if isinstance(tag, dict) and tag.get("Key") == STAGE_TAG and isinstance(tag.get("Value"), str)
    }


def verification_resources(
    templates: Templates,
    actions: list[dict[str, Any]],
    pipeline: Mapping[str, Any],
    project: str,
) -> tuple[dict[str, list[re.Pattern[str]]], set[str]]:
    """For each kind of resource of `VERIFICATION_MAY`, what the role of the verification of a
    stage may name, as `arn_text` writes it: the canary's secret, every workspace of the deploying
    account and region, the release parameters the stage's stacks hold, the objects under
    `verification/` of the pipeline's artifact bucket, that bucket and its objects, and the
    project's own log group and report groups. With them, the values of the tag `rhi-stage` that
    name the stage's workspace: those a workspace of a stack the stage deploys carries, and no
    workspace of any other template."""
    deployed = deployed_by(actions, templates)
    parameters = sorted(
        {
            parameter["Name"]
            for template in deployed
            for _, _, parameter, _ in resources({"": template}, "AWS::SSM::Parameter")
            if isinstance(parameter.get("Name"), str) and RELEASE_PARAMETER.fullmatch(parameter["Name"])
        }
    )
    stores = [
        re.escape(store["Ref"])
        for store in [pipeline.get("ArtifactStore", {}).get("Location")]
        + [entry.get("ArtifactStore", {}).get("Location") for entry in pipeline.get("ArtifactStores", [])]
        if isinstance(store, dict) and isinstance(store.get("Ref"), str)
    ]
    own = rf"\$\{{{re.escape(project)}\}}"
    elsewhere = [template for template in templates.values() if not any(template is one for one in deployed)]
    tags = set().union(*map(workspace_tags, deployed)) - set().union(*map(workspace_tags, elsewhere))
    allowed = {
        "secret": [re.compile(rf"{PARTITION}:secretsmanager:{HERE}:secret:{re.escape(CANARY_SECRET)}-\?{{6}}")],
        "workspaces": [re.compile(rf"{PARTITION}:aps:{HERE}:workspace/\*")],
        "parameters": [re.compile(rf"{PARTITION}:ssm:{HERE}:parameter{re.escape(name)}") for name in parameters],
        "verification": [re.compile(rf"\$\{{{store}\.Arn\}}/{VERIFICATION_PREFIX}/\*") for store in stores],
        "artifacts": [re.compile(rf"\$\{{{store}\.Arn\}}(/\*)?") for store in stores],
        "logs": [re.compile(rf"{PARTITION}:logs:{HERE}:log-group:/aws/codebuild/{own}(:\*)?")],
        "reports": [re.compile(rf"{PARTITION}:codebuild:{HERE}:report-group/{own}-\*")],
    }
    return allowed, tags


def verifies_more(statement: Mapping[str, Any], allowed: Mapping[str, list[re.Pattern[str]]], tags: set[str]) -> bool:
    """Whether an allowed statement of a verification's role reaches an action it is not given, a
    resource beyond those of the action's kind, or, where it queries workspaces, any workspace but
    its stage's: its condition must be the one that admits a workspace by the stage's tag, alone."""
    if reports_more(statement, allowed, VERIFICATION_MAY):
        return True
    queries = {action.lower() for action, kind in VERIFICATION_MAY.items() if kind == "workspaces"}
    if not any(action.lower() in queries for action in as_list(statement.get("Action", []))):
        return False
    condition = statement.get("Condition")
    if not isinstance(condition, dict) or list(condition) != ["StringEquals"]:
        return True
    tagged = condition["StringEquals"]
    if not isinstance(tagged, dict) or list(tagged) != [f"aws:ResourceTag/{STAGE_TAG}"]:
        return True
    (value,) = tagged.values()
    return not (isinstance(value, str) and value in tags)


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
    for path, template in sorted(templates.items()):
        for role in sorted(synth_roles(template)):
            statements = statements_of(template, role)
            if statements is None or any(reads_more(statement) for statement in statements):
                found.append((path, role, READS_MORE))
    for path, template in sorted(templates.items()):
        if not resources({"": template}, "AWS::CodePipeline::Pipeline"):
            continue
        for function, role in reporter_roles(template):
            allowed = reporter_resources(template, function)
            statements = statements_of(template, role) if role is not None else None
            if statements is None or any(reports_more(statement, allowed) for statement in statements):
                found.append((path, role or function, REPORTS_MORE))
    for path, template in sorted(templates.items()):
        for actions, pipeline, named, role in verifications(template):
            allowed, tags = verification_resources(templates, actions, pipeline, named)
            statements = statements_of(template, role) if role is not None else None
            if statements is None or any(verifies_more(statement, allowed, tags) for statement in statements):
                found.append((path, role or named, VERIFIES_MORE))
    # The reporter's secret, read by its function's role and by no other; never by a role that may
    # also put events on the measurement bus.
    for path, template in sorted(templates.items()):
        reporters = (
            {role for _, role in reporter_roles(template)}
            if resources({"": template}, "AWS::CodePipeline::Pipeline")
            else set()
        )
        for _, role, _, _ in resources({"": template}, "AWS::IAM::Role"):
            if not may(template, role, SECRET_READS, SECRET_ARN):
                continue
            if role not in reporters:
                found.append((path, role, SECRET_ELSEWHERE))
            if may(template, role, PUTS_EVENTS, BUS_ARN):
                found.append((path, role, MEASURES_AND_READS))
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
    verification: bool = False,
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
    - a verification: a CodeBuild step named `Verify` after the stage's deployment, and in the
      stage's stack what it reads, a workspace tagged with the stage's name and the release
      parameter of the pool `mock`. Its role holds what CDK Pipelines gives it, until `change`;
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
    if verification:
        cdk.aws_aps.CfnWorkspace(endpoint, "Workspace", tags=[cdk.CfnTag(key=STAGE_TAG, value="Rhi")])
        cdk.aws_ssm.StringParameter(endpoint, "Release", parameter_name="/rhi/pools/mock/release", string_value="{}")
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
    verify = [pipelines.CodeBuildStep(VERIFY_ACTION, commands=["true"])] if verification else []
    pipeline.add_stage(stage, pre=more if last else gate + more, post=(gate if last else []) + verify)
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


# The emulator and the builder as a project that publishes images installs them, pinned.
PINNED = "@sha256:" + "0" * 64
PLUGIN = "$HOME/.docker/cli-plugins/docker-buildx"
IMAGE_BUILDER = [
    f"docker run --privileged --rm tonistiigi/binfmt:qemu-v10.2.3{PINNED} --install arm64",
    (
        f'curl -fsSL --create-dirs -o "{PLUGIN}" '
        "https://github.com/docker/buildx/releases/download/v0.38.2/buildx-v0.38.2.linux-amd64"
    ),
    f'echo "{"0" * 64}  {PLUGIN}" | sha256sum -c -',
    f'chmod +x "{PLUGIN}"',
    (
        f"docker buildx create --name rhi-assets --driver docker-container --driver-opt image=moby/buildkit{PINNED}"
        " --driver-opt default-load=true --bootstrap --use"
    ),
]


def a_publisher_that_installs(installs: list[str]) -> Change:
    """Adds to the asset stage a project that installs the publisher and `installs`, then publishes."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        codebuild = cdk.aws_codebuild
        phases = {"install": {"commands": ["npm install -g cdk-assets@latest", *installs]}, "build": {"commands": [PUBLISH]}}
        spec = codebuild.BuildSpec.from_object({"version": "0.2", "phases": phases})
        project = codebuild.PipelineProject(stack, "Image", build_spec=spec)
        synthesized = cdk.aws_codepipeline.Artifact("Synth_Output")
        action = cdk.aws_codepipeline_actions.CodeBuildAction(action_name="Image", project=project, input=synthesized)
        pipeline.stage(ASSET_STAGE).add_action(action)

    return change


def test_a_publisher_that_installs_the_pinned_image_builder_is_left_out(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path, stack_with_an_asset=BOUNDARY, change=a_publisher_that_installs(IMAGE_BUILDER))
    assert differences(templates, BUILT) == []
    assert breaches(templates) == []


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
        pytest.param(
            a_publisher_that_installs([IMAGE_BUILDER[0].replace(f":qemu-v10.2.3{PINNED}", ":latest")]),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that installs an emulator without its digest",
        ),
        pytest.param(
            a_publisher_that_installs([IMAGE_BUILDER[1].replace("download/v0.38.2", "latest/download")]),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that downloads the builder without its version",
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


def a_synth_role_that_reads(action: str, service: str, resource: str, name: str) -> Change:
    """Allows the synth step's role one more action, on one resource of the deploying account."""

    def change(cdk: ModuleType, stack: Any, _pipeline: Any) -> None:
        (project,) = [
            node
            for node in stack.node.find_all()
            if isinstance(node, cdk.aws_codebuild.Project) and "/Build/Synth/" in node.node.path
        ]
        colon = cdk.ArnFormat.COLON_RESOURCE_NAME if service == "secretsmanager" else cdk.ArnFormat.SLASH_RESOURCE_NAME
        arn = stack.format_arn(service=service, resource=resource, resource_name=name, arn_format=colon)
        project.role.add_to_principal_policy(cdk.aws_iam.PolicyStatement(actions=[action], resources=[arn]))

    return change


def an_approval_in_the_asset_stage(cdk: ModuleType, _stack: Any, pipeline: Any) -> None:
    pipeline.stage(ASSET_STAGE).add_action(cdk.aws_codepipeline_actions.ManualApprovalAction(action_name="Extra"))


ON_MAIN = {"branches_includes": [BRANCH]}


def test_keeps_a_trigger_on_a_push_to_main(cdk: ModuleType, tmp_path: Path) -> None:
    assert breaches(built(cdk, tmp_path, change=with_a_trigger(push=ON_MAIN))) == []


def test_keeps_a_synth_role_that_reads_the_configuration(cdk: ModuleType, tmp_path: Path) -> None:
    reads = a_synth_role_that_reads("ssm:GetParametersByPath", "ssm", "parameter", "rhi/config/*")
    assert breaches(built(cdk, tmp_path, change=reads)) == []


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
        pytest.param(
            {"change": a_synth_role_that_reads("ssm:GetParameter", "ssm", "parameter", "rhi/other")},
            READS_MORE,
            id="a synth role that reads another parameter",
        ),
        pytest.param(
            {"change": a_synth_role_that_reads("secretsmanager:GetSecretValue", "secretsmanager", "secret", "rhi-*")},
            READS_MORE,
            id="a synth role that reads a secret",
        ),
    ],
)
def test_each_rule_refuses_its_breach(cdk: ModuleType, tmp_path: Path, changes: dict[str, Any], expected: str) -> None:
    assert refused(built(cdk, tmp_path / "kept")) == set()
    assert refused(built(cdk, tmp_path / "breached", **changes)) == {expected}


def test_the_reporter_s_role_is_the_one_held(templates: dict[str, dict[str, Any]]) -> None:
    # The rule above holds a role only once it finds it: the app's reporter is found.
    found = [
        role
        for template in templates.values()
        if resources({"": template}, "AWS::CodePipeline::Pipeline")
        for _, role in reporter_roles(template)
    ]
    assert len(found) == 1 and found[0] is not None
    # And so is the bucket whose marks it may list, the pipeline's artifact bucket: the role holds
    # that listing, under its condition.
    ((template, function, role),) = [
        (template, function, role)
        for template in templates.values()
        if resources({"": template}, "AWS::CodePipeline::Pipeline")
        for function, role in reporter_roles(template)
    ]
    (bucket,) = reporter_resources(template, function)[MARKS]
    assert role is not None
    listings = [s for s in statements_of(template, role) or [] if "s3:ListBucket" in as_list(s.get("Action"))]
    assert [(bucket.fullmatch(arn_text(s["Resource"]) or "") is not None, s["Condition"]) for s in listings] == [
        (True, LISTS_THE_MARKS)
    ]


def the_secret(name: str) -> Names:
    def arn(cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        colon = cdk.ArnFormat.COLON_RESOURCE_NAME
        return stack.format_arn(service="secretsmanager", resource="secret", resource_name=name, arn_format=colon)

    return arn


def the_stack(name: str) -> Names:
    def arn(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        return stack.format_arn(service="cloudformation", resource="stack", resource_name=f"{name}/*")

    return arn


def the_pipeline(_cdk: ModuleType, _stack: Any, pipeline: Any) -> str:
    return pipeline.pipeline_arn


def the_artifacts(_cdk: ModuleType, _stack: Any, pipeline: Any) -> str:
    return pipeline.artifact_bucket.arn_for_objects("*")


def another_pipeline(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return stack.format_arn(service="codepipeline", resource="Another")


def the_bucket(_cdk: ModuleType, _stack: Any, pipeline: Any) -> str:
    """The pipeline's artifact bucket itself, which a listing names."""
    return pipeline.artifact_bucket.bucket_arn


def another_bucket(cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return cdk.aws_s3.Bucket(stack, "Another").arn_for_objects("*")


def another_bucket_itself(cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return cdk.aws_s3.Bucket(stack, "Another").bucket_arn


def every_resource(_cdk: ModuleType, _stack: Any, _pipeline: Any) -> str:
    return "*"


def the_topic(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    """The reporter's own topic, the one its function names, which `a_reporter` creates first."""
    return stack.node.find_child("ReporterTopic").topic_arn


def another_topic(cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return cdk.aws_sns.Topic(stack, "AnotherTopic").topic_arn


# The narrow role: each action of `REPORTER_READS` on what it reads, under its condition when it
# has one. The stage of `built` deploys the stack `Rhi-Endpoint`.
NARROW: list[tuple[list[str], Names, dict[str, Any] | None]] = [
    (["secretsmanager:GetSecretValue"], the_secret(f"{REPORTER_SECRET}-??????"), None),
    (
        [
            "codepipeline:GetPipelineExecution",
            "codepipeline:GetPipelineState",
            "codepipeline:ListActionExecutions",
            "codepipeline:ListPipelineExecutions",
        ],
        the_pipeline,
        None,
    ),
    (["cloudformation:DescribeStackEvents"], the_stack("Rhi-Endpoint"), None),
    (["s3:GetObject"], the_artifacts, None),
    (["s3:ListBucket"], the_bucket, LISTS_THE_MARKS),
    (["sns:Publish"], the_topic, None),
]


def a_reporter(
    more: tuple[str, Names] | None = None, *, managed: bool = False, condition: dict[str, Any] | None = None
) -> Change:
    """A rule on the pipeline's state that invokes a function, told its own topic, whose role
    holds the narrow statements, writes its own log group and, when asked, one more statement,
    under `condition` when one is given, or the AWS managed policy for a function's logs."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        iam, lambda_ = cdk.aws_iam, cdk.aws_lambda
        topic = cdk.aws_sns.Topic(stack, "ReporterTopic")
        logs = cdk.aws_logs.LogGroup(stack, "ReporterLogs")
        role = iam.Role(stack, "ReporterRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        statements = [(["logs:CreateLogStream", "logs:PutLogEvents"], logs.log_group_arn, None)]
        statements += [(actions, arn(cdk, stack, pipeline), held) for actions, arn, held in NARROW]
        if more is not None:
            statements.append(([more[0]], more[1](cdk, stack, pipeline), condition))
        for actions, resource, held in statements:
            role.add_to_policy(iam.PolicyStatement(actions=actions, resources=[resource], conditions=held))
        if managed:
            role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            )
        function = lambda_.Function(
            stack,
            "Reporter",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline("def handler(event, context):\n    pass\n"),
            role=role,
            log_group=logs,
            environment={REPORTER_TOPIC: topic.topic_arn},
        )
        pattern = cdk.aws_events.EventPattern(source=["aws.codepipeline"], resources=[pipeline.pipeline_arn])
        targets = [cdk.aws_events_targets.LambdaFunction(function)]
        cdk.aws_events.Rule(stack, "OnTheState", event_pattern=pattern, targets=targets)

    return change


def test_keeps_a_reporter_with_the_narrow_role(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path, change=a_reporter())
    assert [role for template in templates.values() for _, role in reporter_roles(template)] != []
    assert breaches(templates) == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            a_reporter(("secretsmanager:GetSecretValue", the_secret("rhi/other-??????"))), id="another secret"
        ),
        pytest.param(a_reporter(("secretsmanager:*", the_secret(f"{REPORTER_SECRET}-??????"))), id="a wildcard action"),
        pytest.param(a_reporter(("s3:GetObject", another_bucket)), id="another bucket"),
        pytest.param(
            a_reporter(("codepipeline:ListPipelineExecutions", another_pipeline)),
            id="another pipeline's executions",
        ),
        pytest.param(a_reporter(("cloudformation:DescribeStackEvents", every_resource)), id="every resource"),
        pytest.param(
            a_reporter(("cloudformation:DescribeStackEvents", the_stack("Unrelated"))),
            id="a stack the pipeline does not deploy",
        ),
        pytest.param(a_reporter(("codepipeline:StartPipelineExecution", the_pipeline)), id="another action"),
        pytest.param(a_reporter(managed=True), id="a managed policy the template does not hold"),
        pytest.param(a_reporter(("sns:Publish", another_topic)), id="publishing to another topic"),
        pytest.param(a_reporter(("sns:Publish", every_resource)), id="publishing to every topic"),
        pytest.param(a_reporter(("sns:*", the_topic)), id="every action on its own topic"),
        pytest.param(a_reporter(("s3:ListBucket", the_bucket)), id="a listing of the whole artifact bucket"),
        pytest.param(
            a_reporter(("s3:ListBucket", the_bucket), condition={"StringLike": {"s3:prefix": "*"}}),
            id="a listing under another prefix",
        ),
        pytest.param(
            a_reporter(("s3:ListBucket", another_bucket_itself), condition=LISTS_THE_MARKS),
            id="a listing of another bucket",
        ),
        pytest.param(a_reporter(("s3:PutObject", the_artifacts)), id="a write to the artifact bucket"),
    ],
)
def test_refuses_a_reporter_that_may_do_more(cdk: ModuleType, tmp_path: Path, change: Change) -> None:
    assert refused(built(cdk, tmp_path / "kept", change=a_reporter())) == set()
    assert refused(built(cdk, tmp_path / "breached", change=change)) == {REPORTS_MORE}


def test_the_verification_s_role_is_the_one_held(templates: dict[str, dict[str, Any]]) -> None:
    # The rule holds a role only once it finds it: the app's verification is found, with its role,
    # and so are the workspace and the release parameters of the stage it verifies.
    (found,) = [found for template in templates.values() for found in verifications(template)]
    actions, pipeline, project, role = found
    assert role is not None
    allowed, tags = verification_resources(templates, actions, pipeline, project)
    assert tags == {"Rhi"} and allowed["parameters"] and allowed["verification"]


def the_workspaces(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return stack.format_arn(service="aps", resource="workspace", resource_name="*")


def the_parameter(name: str) -> Names:
    def arn(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        return stack.format_arn(service="ssm", resource="parameter", resource_name=name)

    return arn


def under(prefix: str, bucket: Names | None = None) -> Names:
    """The objects under a prefix of the pipeline's artifact bucket, or of the bucket `bucket` makes."""

    def arn(cdk: ModuleType, stack: Any, pipeline: Any) -> str:
        if bucket is None:
            return pipeline.artifact_bucket.arn_for_objects(f"{prefix}/*")
        return bucket(cdk, stack, pipeline).removesuffix("*") + f"{prefix}/*"

    return arn


def tagged(stage: str) -> dict[str, Any]:
    """The condition that admits a workspace by the name of its stage."""
    return {"StringEquals": {f"aws:ResourceTag/{STAGE_TAG}": stage}}


# One more statement of a verification's role: its action, what it names and its condition.
More = tuple[str, "Names", "dict[str, Any] | None"]
# The narrow role: what the verification of the stage of `built` reads and writes.
VERIFIES: list[More] = [
    ("secretsmanager:GetSecretValue", the_secret(f"{CANARY_SECRET}-??????"), None),
    ("aps:QueryMetrics", the_workspaces, tagged("Rhi")),
    ("ssm:GetParameter", the_parameter("rhi/pools/mock/release"), None),
    ("s3:PutObject", under(VERIFICATION_PREFIX), None),
]


def a_verification(more: More | None = None, *, managed: bool = False, elsewhere: bool = False) -> Change:
    """Gives the role of the verification of `built` the narrow statements and, when asked, one
    more statement, an AWS managed policy, or, in a stack the pipeline does not deploy, a second
    workspace that carries the stage's tag."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        iam = cdk.aws_iam
        (project,) = [
            node
            for node in stack.node.find_all()
            if isinstance(node, cdk.aws_codebuild.Project) and f"/Rhi/{VERIFY_ACTION}" in node.node.path
        ]
        for action, arn, condition in [*VERIFIES, *([more] if more is not None else [])]:
            statement = iam.PolicyStatement(
                actions=[action], resources=[arn(cdk, stack, pipeline)], conditions=condition
            )
            project.role.add_to_principal_policy(statement)
        if managed:
            # The one managed policy the rules on the reporter's secret know: only this rule refuses it.
            project.role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            )
        if elsewhere:
            other = cdk.Stack(stack.node.scope, "Elsewhere")
            cdk.aws_aps.CfnWorkspace(other, "Workspace", tags=[cdk.CfnTag(key=STAGE_TAG, value="Rhi")])

    return change


def test_keeps_a_verification_with_the_narrow_role(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path, verification=True, change=a_verification())
    ((_, _, _, role),) = [found for template in templates.values() for found in verifications(template)]
    assert role is not None
    assert breaches(templates) == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            a_verification(("secretsmanager:GetSecretValue", the_secret("rhi/other-??????"), None)),
            id="another secret",
        ),
        pytest.param(
            a_verification(("secretsmanager:*", the_secret(f"{CANARY_SECRET}-??????"), None)),
            id="every action on the canary's secret",
        ),
        pytest.param(a_verification(("aps:QueryMetrics", the_workspaces, None)), id="every workspace"),
        pytest.param(
            a_verification(("aps:QueryMetrics", the_workspaces, tagged("Other"))),
            id="the workspace of another stage",
        ),
        pytest.param(
            a_verification(("aps:QueryMetrics", the_workspaces, {"StringLike": tagged("Rhi")["StringEquals"]})),
            id="workspaces under another condition",
        ),
        pytest.param(
            a_verification(elsewhere=True), id="a second workspace of the stage's tag, in a stack it does not deploy"
        ),
        pytest.param(a_verification(("aps:RemoteWrite", the_workspaces, tagged("Rhi"))), id="a write to the workspace"),
        pytest.param(
            a_verification(("ssm:GetParameter", the_parameter("rhi/pools/*/release"), None)),
            id="every pool's release parameter",
        ),
        pytest.param(
            a_verification(("ssm:GetParameter", the_parameter("rhi/pools/other/release"), None)),
            id="the release parameter of a pool the stage does not deploy",
        ),
        pytest.param(
            a_verification(("ssm:GetParameter", the_parameter("rhi/config/domain"), None)), id="another parameter"
        ),
        pytest.param(a_verification(("s3:PutObject", the_artifacts, None)), id="a write outside verification/"),
        pytest.param(
            a_verification(("s3:PutObject", under(VERIFICATION_PREFIX, another_bucket), None)),
            id="a write under verification/ of another bucket",
        ),
        pytest.param(
            a_verification(("s3:DeleteObject", under(VERIFICATION_PREFIX), None)),
            id="another action under verification/",
        ),
        pytest.param(a_verification(("s3:PutObject", every_resource, None)), id="every resource"),
        pytest.param(a_verification(managed=True), id="a managed policy the template does not hold"),
    ],
)
def test_refuses_a_verification_that_may_do_more(cdk: ModuleType, tmp_path: Path, change: Change) -> None:
    assert refused(built(cdk, tmp_path / "kept", verification=True, change=a_verification())) == set()
    assert refused(built(cdk, tmp_path / "breached", verification=True, change=change)) == {VERIFIES_MORE}


def a_role(
    *, measures: bool = False, reads: Names | None = None, action: str = "secretsmanager:GetSecretValue"
) -> Change:
    """A job's role in the pipeline's stack, given the grant of `rhi.pipeline.reporter` when it
    measures, and allowed `action` on what `reads` names when asked."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        from rhi.pipeline import reporter

        role = cdk.aws_iam.Role(stack, "Job", assumed_by=cdk.aws_iam.ServicePrincipal("codebuild.amazonaws.com"))
        if measures:
            reporter.measuring_job(role, {})
        if reads is not None:
            statement = cdk.aws_iam.PolicyStatement(actions=[action], resources=[reads(cdk, stack, pipeline)])
            role.add_to_policy(statement)

    return change


def test_keeps_a_job_given_the_measurement_grant(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path, change=a_role(measures=True))
    _, role, _, template = next(found for found in resources(templates, "AWS::IAM::Role") if found[1].startswith("Job"))
    statements = statements_of(template, role)
    assert statements is not None and any(reaches(s, PUTS_EVENTS, BUS_ARN) for s in statements)
    assert not may(template, role, SECRET_READS, SECRET_ARN)
    assert breaches(templates) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(a_role(reads=the_secret(f"{REPORTER_SECRET}-??????")), {SECRET_ELSEWHERE}, id="by its name"),
        pytest.param(
            a_role(reads=the_secret(f"{REPORTER_SECRET}-GhIjKl")), {SECRET_ELSEWHERE}, id="by its full name"
        ),
        pytest.param(a_role(reads=every_resource), {SECRET_ELSEWHERE}, id="on every resource"),
        pytest.param(
            a_role(reads=the_secret("rhi/*"), action="secretsmanager:*"), {SECRET_ELSEWHERE}, id="by wildcards"
        ),
        pytest.param(
            a_role(measures=True, reads=the_secret(f"{REPORTER_SECRET}-??????")),
            {SECRET_ELSEWHERE, MEASURES_AND_READS},
            id="by a job given the measurement grant",
        ),
        pytest.param(
            a_role(measures=True, reads=the_secret(f"{REPORTER_SECRET}-GhIjKl")),
            {SECRET_ELSEWHERE, MEASURES_AND_READS},
            id="by its full name, by a job given the measurement grant",
        ),
    ],
)
def test_refuses_another_role_that_reads_the_reporter_s_secret(
    cdk: ModuleType, tmp_path: Path, change: Change, expected: set[str]
) -> None:
    assert refused(built(cdk, tmp_path / "kept", change=a_role(reads=the_secret("rhi/other-??????")))) == set()
    assert refused(built(cdk, tmp_path / "breached", change=change)) == expected


def test_refuses_a_reporter_that_also_puts_measurements(cdk: ModuleType, tmp_path: Path) -> None:
    def the_bus(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        return stack.format_arn(service="events", resource="event-bus", resource_name=MEASUREMENT_BUS)

    templates = built(cdk, tmp_path, change=a_reporter(("events:PutEvents", the_bus)))
    assert refused(templates) == {REPORTS_MORE, MEASURES_AND_READS}
