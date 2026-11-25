"""The pipeline's own steps, triggers and roles (system design, section 8.4, control 2).

A merge to `main` is deployment rights in the account, and the pipeline is what uses them. A
change to what the pipeline runs therefore shows up as a change to this protected file.

**The reviewed list**, `REVIEWED`, holds the pipeline's stages in order, each with its actions in
the order they run. An action is listed by its name and its type. Two kinds of action are not
listed, because every stack and every asset a later change adds to the stage brings its own:

- an asset-publishing action: a CodeBuild action whose project does nothing but publish assets
  with `cdk-assets`, after installing, to build an image for linux/arm64 as well as linux/amd64,
  an arm64 emulator and the buildx plugin, each a download from its project's releases on GitHub
  by its exact version, checked against a checksum before it is extracted or run: the commands of
  `INSTALLS_THE_IMAGE_BUILDER`, all of them and in their order, in the `commands` of one phase,
  and no other. It runs, pulls and creates no image, an emulator's or a builder's. It is left out
  in the asset stage, and so is that stage while it holds nothing else: CDK Pipelines creates it
  with the first asset;
- a stack's deployment action: a CloudFormation action that prepares or executes a change set, or
  creates and updates a stack.

Rules hold those two, and every action:

- an asset-publishing action is in the asset stage, and every action of that stage is a CodeBuild
  action. One that does more than publish assets is a step like any other, held by the list;
- no stage of a pipeline holds a manual approval, an action of the category `Approval`, whatever
  it follows or precedes: a deployment has no person in its path, a change that widens a
  permission among them, which the examiner and these rules hold before the merge and the bounded
  bootstrap's boundary after it (section 8.4, control 6). CDK Pipelines' approval on widened
  permissions (`ConfirmPermissionsBroadening`) and its `ManualApprovalStep` each make one. A
  CloudFormation action that deletes a stack is a step, held by the list;
- the pipeline starts on a push to `main` and on nothing else: its source is the connection's, on
  that branch; a trigger filters pushes to that branch, and never pull requests or tags; and no
  EventBridge rule or schedule of any template targets a pipeline: not one of the pipeline's
  template by its reference, not one of another template by an import of the pipeline's
  export, and none by a CodePipeline ARN (section 8.2). The promotion job starts an execution
  through the API, after the owner started the job by hand, which none of these is;
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
  that the stacks that stage deploys hold, each by its name; read by pool, with `dynamodb:Query`
  and no other action, the items of the stage's heartbeats table and of no other table: the table
  of a stack that stage deploys whose name the action is given in `RHI_VERIFY_HEARTBEATS_TABLE`,
  as an output of that stack, named as CloudFormation names a table that has no name of its own,
  the stack's name, a hyphen, the table's logical ID, a hyphen and a wildcard for the suffix it
  generates, in the deploying account and region (section 8.4); for the sessions it books, on the
  mock pool and on the coursework pool, read the one stack resource that names the stage's
  pool-state table, `cloudformation:DescribeStackResource` on a stack that the stage deploys and
  whose template holds the pool-state table, and read and write, with `dynamodb:GetItem` and
  `dynamodb:PutItem` and no other action, the items of that table whose partition key is the mock
  pool's name, `mock`, or the coursework pool's, `coursework`: the table is named as the hold's role
  names it, and the statement's condition is `ForAllValues:StringEquals` on `dynamodb:LeadingKeys`
  with those names, one or both and no other, alone, so the role reaches no third pool's items,
  not every item, and no other table; write objects under
  `verification/` of the pipeline's artifact bucket; and, for the decision whether the execution
  verifies on the coursework pool, describe Auto Scaling groups and their instance refreshes, which
  AWS grants on every resource only, and list the executions of the pipeline the action is in, by
  its ARN, and of no other pipeline. Beside those it holds what CDK Pipelines gives every project:
  a read of the artifact bucket, and its own log group and report groups. Nothing else: every
  action it is allowed is one of `VERIFICATION_MAY`, each on resources of its kind only, read as
  the reporter's are, so no other action of Auto Scaling or CodePipeline, on any resource;
- the role of the hold, the project of a CodeBuild action named `Hold` (sections 5.6 and 8.2), which
  passes once the session switch reads off, may read the one stack resource that names the stage's
  pool-state table, `cloudformation:DescribeStackResource` on a stack that the stage the action is
  in deploys and whose template holds the pool-state table, by its name, `stack/<name>/*`; and read
  that table's items, `dynamodb:GetItem` on the pool-state table of that stack and of no other
  stack, named as CloudFormation names a table that has no name of its own, the stack's name, a
  hyphen, the table's logical ID, a hyphen and a wildcard for the suffix it generates, in the
  deploying account and region. Beside those it holds what CDK Pipelines gives every project: a
  read of the artifact bucket, and its own log group and report groups. Nothing else: every action
  it is allowed is one of `HOLD_MAY`, each on resources of its kind only, read as the reporter's
  are;
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
  resources may name the bus, in any partition, region and account, read as above;
- no role of any template may write to the model bucket but the role of the step that copies the
  weights, the project of a pipeline's CodeBuild action named `CopyWeights` (section 4.4): the
  copy job verifies each file against its manifest before it writes it, so the bucket holds
  verified files only for as long as it has that one writer. A statement writes to the bucket
  when one of its actions matches the putting or the deleting of an object (`OBJECT_WRITES`) and
  one of its resources may name an object of a bucket whose name starts as the model bucket's,
  `rhi-models-`, in any partition, read as above. One value of the template is read for what it
  is: a reference to a bucket of the role's own template stands for that bucket's name, and a
  bucket with no name of its own, which CloudFormation names, is not the model bucket;
- the promotion job, the CodeBuild project named `rhi-promote-image` in a pipeline's template
  (section 4.3), is started by the owner by hand and by nothing else: it has no webhook, no action
  of any pipeline runs it, and no EventBridge rule or schedule of any template targets it, by a
  reference in its own template, by an import of what that template exports of it, or by its
  name in a CodeBuild ARN;
- no role of any template but the promotion job's may write a pool's image parameter: a statement
  writes one when one of its actions matches `ssm:PutParameter` and one of its resources may name
  a parameter under /rhi/images/, in any partition, region and account, read as above. The job
  writes it only after a canary start on the image passed, so no other path changes the driver
  under a pinned engine;
- no role of any template but the promotion job's may start an instance: a statement starts one
  when one of its actions matches `ec2:RunInstances` or `ec2:StartInstances` and one of its
  resources may name an instance, read as above. Auto Scaling starts a pool's instances with its
  own service-linked role, which no template holds, at the size the scaler alone sets
  (section 5.1);
- a spike job, a CodeBuild project whose build specification runs a script under `spikes/`
  (sections 8.4 and 12), is started by the owner and by nothing else, as the promotion job is: it
  has no webhook, no action of any pipeline runs it, and no EventBridge rule or schedule of any
  template targets it, by a reference in its own template, by an import of what that template
  exports of it, or by its name in a CodeBuild ARN;
- a spike job's role may do what `SPIKES_MAY` lists, each action on resources of its kind only,
  and nothing else: read the parameters under /rhi/config/ by path; read the records of the stage's
  pool-state table, an unnamed table of a stack a pipeline deploys whose logical ID is the pool
  tables' `PoolState`, by the name CloudFormation gives it; write, with `dynamodb:PutItem` and no
  other action, the items of that table whose partition key is the mock pool's name, `mock`, for
  the session a job books on that pool (section 12): the statement's condition is
  `OF_THE_MOCK_POOL`, alone, as on the verification's role, so the role writes no other pool's
  items, not every item, and no other table; read the spikes' one secret,
  `rhi/spike-keys`; query the workspace of a stage a pipeline deploys, by the condition on its tag
  alone, as the verification's role does; put events on the measurement bus; use the connection of
  its own template; write to the log group its project names and use the key its project is
  encrypted with. So no action of Auto Scaling, EC2 or IAM, and no other secret. A wildcard
  action, `NotAction`, `NotResource` and a managed policy the template does not hold are taken as
  reaching more;
- the weekly GPU canary, the CodeBuild project named `rhi-gpu-canary` (sections 8.2, step 6, and
  14), is started by its schedule, and its schedule starts it and nothing else. The schedule is
  the EventBridge rule or schedule named `rhi-gpu-canary`, known by that name and not by its
  target: a template that holds the canary's project holds exactly one, and each has one target,
  the canary's project of its own template, by a reference to its ARN. The role it gives that
  target may start a build of that project, `codebuild:StartBuild` on its ARN, and nothing else:
  a wildcard action or resource, `NotAction`, `NotResource` and a managed policy the template does
  not hold are taken as reaching more;
- the canary's job tests what is deployed and starts no deployment: its project's role may not
  start an execution of any pipeline, `codepipeline:StartPipelineExecution` on any CodePipeline
  ARN, read as above. A role its template does not hold is taken as one that may.

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
            # Before the stage's deployment actions, which the list leaves out: the copy of the
            # weights, then the hold, which passes once no session is on.
            ("CopyWeights", "Build/AWS/CodeBuild"),
            ("Hold", "Build/AWS/CodeBuild"),
            # After them.
            ("Verify", "Build/AWS/CodeBuild"),
        ],
    ),
]

# The types of action the rules name, as `Category/Owner/Provider`.
CODEBUILD = "Build/AWS/CodeBuild"
CLOUDFORMATION = "Deploy/AWS/CloudFormation"
CONNECTION = "Source/AWS/CodeStarSourceConnection"

# The name CDK Pipelines gives the stage that publishes assets.
ASSET_STAGE = "Assets"
# What a project that only publishes assets runs, as CDK Pipelines writes it.
PUBLISHES_ASSETS = re.compile(r'cdk-assets --path "[\w./-]+" --verbose publish "[\w:.-]+"')
INSTALLS_THE_PUBLISHER = re.compile(r"npm install -g cdk-assets@[\w.-]+")
# What a project that publishes images also runs, as `src/rhi/pipeline/stack.py` writes it: an
# arm64 emulator and the buildx plugin, each a download by its exact version, checked against a
# checksum (sections 4.2 and 8.2, step 4). The emulator is the two files of one release of the
# binfmt project, the tool and the emulators of the QEMU version the release is named after: each
# downloaded, checked and extracted in one folder, then registered by the tool. Nothing here runs
# an image or creates a builder: the build is the Docker daemon's own builder's.
# The commands are allowed as one sequence, whole and in this order, in the `commands` of one
# phase, and not one by one: a file is extracted, and a tool run, only under the check of its
# checksum, and a check that fails ends the `commands` of its phase and nothing else.
# `INSTALLS_THE_IMAGE_BUILDER` matches them joined by a line feed, so that the release and the
# QEMU version of one line are those of the others.
EMULATOR = r"/opt/binfmt"
EMULATOR_TOOL = r"binfmt_linux-amd64\.tar\.gz"
EMULATORS = r"qemu_v(?P=qemu)_linux-amd64\.tar\.gz"
BUILDX = r"\$HOME/\.docker/cli-plugins/docker-buildx"
EMULATOR_RELEASE = r"https://github\.com/tonistiigi/binfmt/releases/download/deploy/v(?P<qemu>\d+\.\d+\.\d+)-\d+"
BUILDX_RELEASE = r"https://github\.com/docker/buildx/releases/download/v(?P<buildx>\d+\.\d+\.\d+)"
INSTALLS_THE_IMAGE_BUILDER = re.compile(
    "\n".join(
        [
            rf'curl -fsSL --create-dirs -o "{EMULATOR}/{EMULATOR_TOOL}" (?P<release>{EMULATOR_RELEASE})/{EMULATOR_TOOL}',
            rf'echo "[0-9a-f]{{64}}  {EMULATOR}/{EMULATOR_TOOL}" \| sha256sum -c -',
            rf'tar -xzf "{EMULATOR}/{EMULATOR_TOOL}" -C "{EMULATOR}"',
            rf'curl -fsSL --create-dirs -o "{EMULATOR}/{EMULATORS}" (?P=release)/{EMULATORS}',
            rf'echo "[0-9a-f]{{64}}  {EMULATOR}/{EMULATORS}" \| sha256sum -c -',
            rf'tar -xzf "{EMULATOR}/{EMULATORS}" -C "{EMULATOR}"',
            rf"QEMU_BINARY_PATH={EMULATOR} {EMULATOR}/binfmt --install arm64",
            rf'curl -fsSL --create-dirs -o "{BUILDX}" {BUILDX_RELEASE}/buildx-v(?P=buildx)\.linux-amd64',
            rf'echo "[0-9a-f]{{64}}  {BUILDX}" \| sha256sum -c -',
            rf'chmod \+x "{BUILDX}"',
        ]
    )
)
# The phases of a build specification, in the order CodeBuild runs them.
PHASES = ("install", "pre_build", "build", "post_build")
# The modes of a CloudFormation action that only deploy a stack: the others delete one, or replace
# one that failed.
DEPLOYS = ("CHANGE_SET_REPLACE", "CHANGE_SET_EXECUTE", "CREATE_UPDATE")

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
# its stage's workspace is named by, the name of a pool's release parameter, the variable that names
# its stage's heartbeats table to it, as an output of a deployed stack, and the actions its role
# may be allowed, each with the kind of resource it may be allowed on. The last three groups are
# what CDK Pipelines gives every project of the pipeline.
VERIFY_ACTION = "Verify"
CANARY_SECRET = "rhi/canary-key"
VERIFICATION_PREFIX = "verification"
STAGE_TAG = "rhi-stage"
RELEASE_PARAMETER = re.compile(r"/rhi/pools/[a-z0-9-]+/release")
HEARTBEATS_TABLE = "RHI_VERIFY_HEARTBEATS_TABLE"
STACK_OUTPUT = re.compile(r"#\{(\w+)\.(\w+)\}")
# The pools the verification books, and the one condition under which its role may read and write
# items of the pool-state table: every partition key of the request is one of those pools' names.
MOCK_POOL = "mock"
COURSEWORK_POOL = "coursework"
BOOKED_POOLS = [MOCK_POOL, COURSEWORK_POOL]
OF_THE_BOOKED_POOLS = {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": BOOKED_POOLS}}
VERIFICATION_MAY = {
    "secretsmanager:GetSecretValue": "secret",
    "aps:QueryMetrics": "workspaces",
    "ssm:GetParameter": "parameters",
    "dynamodb:Query": "heartbeats",
    "cloudformation:DescribeStackResource": "pools stack",
    "dynamodb:GetItem": "pool-state",
    "dynamodb:PutItem": "pool-state",
    "s3:PutObject": "verification",
    "autoscaling:DescribeAutoScalingGroups": "every",
    "autoscaling:DescribeInstanceRefreshes": "every",
    "codepipeline:ListPipelineExecutions": "own pipeline",
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
# The hold: the name of its action, and the actions its role may be allowed, each with the kind of
# resource it may be allowed on. The last three groups are what CDK Pipelines gives every project.
HOLD_ACTION = "Hold"
HOLD_MAY = {
    "cloudformation:DescribeStackResource": "pools stack",
    "dynamodb:GetItem": "pool-state",
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
# The AWS managed policy CDK gives the functions it adds itself:
# it allows creating a log group and writing logs, and nothing else.
WRITES_LOGS_ONLY = re.compile(
    r"arn:(aws[a-z-]*|\$\{AWS::Partition\}):iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)
PUTS_EVENTS = ("events:PutEvents",)
BUS_ARN = f"arn:{FIELD}:events:{FIELD}:{FIELD}:event-bus/{MEASUREMENT_BUS}"
# The step that copies the weights, by its action's name; what the model bucket's name starts with,
# before the account and the region (`src/rhi/pools/model_store.py`); what writes to a bucket, the
# putting of an object, which an upload in parts is too, and the deleting of one; and every ARN of
# an object of the model bucket, as a pattern.
COPY_ACTION = "CopyWeights"
MODEL_BUCKET = "rhi-models"
OBJECT_WRITES = ("s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion")
MODEL_OBJECT_ARN = f"arn:{FIELD}:s3:::{MODEL_BUCKET}-*"
# A reference to a resource of the template, as `arn_text` writes one: by itself, which is a
# bucket's name, or by its ARN.
REFERENCE = re.compile(r"\$\{(\w+)(\.Arn)?\}")
# What stands for the name of a bucket that has none of its own: no name matches it.
UNNAMED = "\x01"
PARTITION = r"arn:(aws[a-z-]*|\$\{AWS::Partition\})"
HERE = r"\$\{AWS::Region\}:\$\{AWS::AccountId\}"
# The pseudo parameters that fill one field of an ARN.
PSEUDO_FIELD = re.compile(r"\$\{AWS::(Partition|Region|AccountId|URLSuffix)\}")

UNLISTED = "the pipeline has it and the reviewed list does not"
MISSING = "the reviewed list has it and the pipeline does not"
ANOTHER_ORDER = "the pipeline's stages and actions are not in the order of the reviewed list"
ASSETS_ELSEWHERE = "an asset-publishing action outside the asset stage"
NOT_CODEBUILD = "an action of the asset stage that is not a CodeBuild action"
MANUAL_APPROVAL = "a manual approval action"
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
    " read its stage's release parameters, query its stage's heartbeats table, read its stage's pools stack's"
    " resources, read and write the mock pool's items of its pool-state table, write under verification/,"
    " describe groups and their refreshes, and list its own pipeline's executions"
)
HOLDS_MORE = (
    "a hold's role that may do more than read the stage's pools stack's resources and the session switch of its"
    " pool-state table"
)
SECRET_ELSEWHERE = "a role other than the reporter function's that may read the reporter's secret"
MEASURES_AND_READS = "a role that may put events on the measurement bus and read the reporter's secret"
WRITES_WEIGHTS = "a role other than the copy step's that may write to the model bucket"
# The variable that names the reporter's topic to its function.
REPORTER_TOPIC = "RHI_REPORTER_TOPIC"
# The promotion job, by its project's name; what writes a parameter, and every ARN of a pool's image
# parameter; what starts an instance, and every ARN of an instance.
PROMOTION_PROJECT = "rhi-promote-image"
PARAMETER_WRITES = ("ssm:PutParameter",)
IMAGE_PARAMETER_ARN = f"arn:{FIELD}:ssm:{FIELD}:{FIELD}:parameter/rhi/images/*"
INSTANCE_STARTS = ("ec2:RunInstances", "ec2:StartInstances")
INSTANCE_ARN = f"arn:{FIELD}:ec2:{FIELD}:{FIELD}:instance/*"
PROMOTION_STARTED = "a promotion job that something other than the owner may start"
WRITES_IMAGES = "a role other than the promotion job's that may write an image parameter"
STARTS_INSTANCES = "a role other than the promotion job's that may start an instance"
# A spike job: a command of its project's build specification runs a script under `spikes/`. The
# spikes' one secret, the construct ID of the pool-state table, which its logical ID begins with
# after its parents' (`src/rhi/pools/tables.py`), and the actions a spike job's role may be allowed,
# each with the kind of resource it may be allowed on. `MOCK_POOL_ITEMS` is the pool-state table
# again, for the one write: only under the condition `OF_THE_MOCK_POOL`.
RUNS_A_SPIKE = re.compile(r"(^|\s)spikes/[\w./-]+")
SPIKE_SECRET = "rhi/spike-keys"
POOL_STATE = "PoolState"
MOCK_POOL_ITEMS = "mock pool's items"
SPIKES_MAY = {
    "ssm:GetParametersByPath": "configuration",
    "dynamodb:GetItem": "pool-state",
    "dynamodb:Query": "pool-state",
    "dynamodb:PutItem": MOCK_POOL_ITEMS,
    "secretsmanager:GetSecretValue": "secret",
    "aps:QueryMetrics": "workspaces",
    "events:PutEvents": "bus",
    "codeconnections:GetConnection": "connection",
    "codeconnections:GetConnectionToken": "connection",
    "logs:CreateLogStream": "logs",
    "logs:PutLogEvents": "logs",
    "kms:Decrypt": "key",
    "kms:DescribeKey": "key",
    "kms:Encrypt": "key",
    "kms:GenerateDataKey": "key",
}
SPIKE_STARTED = "a spike job that something other than the owner may start"
# The weekly GPU canary: its project's and its schedule's one name, what starts its build, and
# what starts a pipeline's execution, on every ARN of a pipeline.
CANARY = "rhi-gpu-canary"
STARTS_THE_CANARY = "codebuild:StartBuild"
PIPELINE_STARTS = ("codepipeline:StartPipelineExecution",)
PIPELINE_ARN = f"arn:{FIELD}:codepipeline:{FIELD}:{FIELD}:*"
CANARY_STARTS_MORE = "a canary's schedule that starts anything but the canary's job"
CANARY_UNSCHEDULED = "a canary's job without its one schedule"
CANARY_STARTS_PIPELINES = "a canary's job whose role may start a pipeline's execution"
SPIKES_MORE = (
    "a spike job's role that may do more than read the configuration, the pool-state records and the spikes'"
    " secret, write the mock pool's items of the pool-state table, query its stage's workspace, send a"
    " measurement, read the repository and write its logs"
)


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


def blocks_of(build_spec: Any) -> list[list[str]] | None:
    """The commands of a build specification written as text, as the blocks CodeBuild runs: the
    `commands` of each phase, then its `finally`, in the order CodeBuild runs the phases whatever
    the order they are written in, a phase it does not know last. A command that fails ends its
    `commands` block, and nothing else: the `finally` of its phase runs after it, and a later
    phase may. A phase's `commands` block is first of its two, at an even place. None when it is
    not such a build specification."""
    try:
        phases = json.loads(build_spec)["phases"]
        ordered = sorted(phases, key=lambda phase: PHASES.index(phase) if phase in PHASES else len(PHASES))
        blocks = [list(phases[phase].get(key, [])) for phase in ordered for key in ("commands", "finally")]
    except (TypeError, ValueError, KeyError, AttributeError):  # Not such a build specification.
        return None
    return blocks if all(isinstance(command, str) for block in blocks for command in block) else None


def commands_of(build_spec: Any) -> list[str] | None:
    """The commands of a build specification written as text, of every block, in the order
    CodeBuild runs them. None when it is not such a build specification."""
    blocks = blocks_of(build_spec)
    return None if blocks is None else [command for block in blocks for command in block]


def publishes_assets(action: Mapping[str, Any], template: Mapping[str, Any]) -> bool:
    """Whether an action is a CodeBuild action whose project does nothing but publish assets:
    its build specification installs `cdk-assets` and publishes with it, and runs nothing else,
    or nothing else than the whole sequence that installs the pinned emulator and buildx plugin an
    image is built with, in its order and in the `commands` of one phase: there, and only there,
    a checksum that does not match ends the sequence before the file is extracted or run."""
    blocks = blocks_of(build_spec_of(action, template))
    if blocks is None:
        return False
    publishes = any(PUBLISHES_ASSETS.fullmatch(command) for block in blocks for command in block)
    others = [
        (place, command)
        for place, block in enumerate(blocks)
        for command in block
        if not PUBLISHES_ASSETS.fullmatch(command) and not INSTALLS_THE_PUBLISHER.fullmatch(command)
    ]
    in_one_commands_block = len({place for place, _ in others}) == 1 and others[0][0] % 2 == 0
    installs = INSTALLS_THE_IMAGE_BUILDER.fullmatch("\n".join(command for _, command in others)) is not None
    return publishes and (not others or (in_one_commands_block and installs))


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


def copy_roles(template: Mapping[str, Any]) -> set[str]:
    """The logical IDs of the roles of the projects that the CodeBuild actions named `CopyWeights`
    run, in the pipelines of a template."""
    held = template.get("Resources", {})
    found = set()
    for _, _, pipeline, _ in resources({"": template}, "AWS::CodePipeline::Pipeline"):
        for _, actions in stages_of(pipeline):
            for action in actions:
                project = action.get("Configuration", {}).get("ProjectName")
                if action["Name"] != COPY_ACTION or type_of(action) != CODEBUILD or not isinstance(project, dict):
                    continue
                role = held.get(project.get("Ref"), {}).get("Properties", {}).get("ServiceRole")
                if isinstance(role, dict) and "Fn::GetAtt" in role:
                    found.add(role["Fn::GetAtt"][0])
    return found


def with_its_buckets_named(text: str, template: Mapping[str, Any]) -> str:
    """A resource of a statement, as `arn_text` writes it, with each reference to a bucket of the
    template replaced by that bucket's name, or by its ARN for a reference to its ARN. A bucket
    with no name of its own takes `UNNAMED`, and a name that cannot be read as text a wildcard."""
    held = template.get("Resources", {})

    def named(reference: re.Match[str]) -> str:
        bucket = held.get(reference[1], {})
        if bucket.get("Type") != "AWS::S3::Bucket":
            return reference[0]
        name = UNNAMED
        if "BucketName" in bucket.get("Properties", {}):
            name = arn_text(bucket["Properties"]["BucketName"]) or "*"
        return f"arn:{FIELD}:s3:::{name}" if reference[2] else name

    return REFERENCE.sub(named, text)


def writes_weights(template: Mapping[str, Any], role: str) -> bool:
    """Whether a role of the template may put or delete an object of the model bucket: one of its
    allowed statements matches a write of an object on a resource that may name one of a bucket
    whose name starts as the model bucket's. Read as `may` reads a role, but for a reference to a
    bucket of the template, which stands for that bucket and no other."""
    statements = statements_of(template, role, known=WRITES_LOGS_ONLY)
    if statements is None:
        return True
    for statement in statements:
        if "NotAction" in statement or "NotResource" in statement:
            return True
        if not any(
            isinstance(action, str) and fnmatch.fnmatchcase(write.lower(), action.lower())
            for action in as_list(statement.get("Action", []))
            for write in OBJECT_WRITES
        ):
            continue
        for resource in as_list(statement.get("Resource", [])):
            text = arn_text(resource)
            if text is None:
                return True
            text = PSEUDO_FIELD.sub(FIELD, with_its_buckets_named(text, template))
            if overlaps(MODEL_OBJECT_ARN, re.sub(r"\$\{[^}]*\}", "*", text)):
                return True
    return False


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


def heartbeat_tables(templates: Templates, actions: list[dict[str, Any]]) -> list[str]:
    """The heartbeats tables that the verifications of a stage are told of, each as the pattern of
    the name CloudFormation gives it: the stack's name, a hyphen, the table's logical ID, a hyphen
    and a wildcard for the generated suffix. A table is one when an action named `Verify` of the
    stage is given, in `HEARTBEATS_TABLE`, an output of a stack that the stage deploys, and that
    output is a reference to a DynamoDB table of the stack's template that has no name of its own."""
    found = []
    deployments = [action for action in actions if type_of(action) == CLOUDFORMATION]
    for action in actions:
        if action["Name"] != VERIFY_ACTION or type_of(action) != CODEBUILD:
            continue
        variables = action.get("Configuration", {}).get("EnvironmentVariables")
        for variable in json.loads(variables) if isinstance(variables, str) else []:
            output = STACK_OUTPUT.fullmatch(str(variable.get("value")))
            if variable.get("name") != HEARTBEATS_TABLE or output is None:
                continue
            # The action that executes a stack's change set carries the namespace of its outputs, and
            # the one that prepares the change set names the stack's template.
            for stack in sorted(
                {d["Configuration"]["StackName"] for d in deployments if d.get("Namespace") == output[1]}, key=str
            ):
                of_the_stack = [d for d in deployments if d["Configuration"]["StackName"] == stack]
                for template in deployed_by(of_the_stack, templates):
                    value = template.get("Outputs", {}).get(output[2], {}).get("Value")
                    table = value.get("Ref") if isinstance(value, dict) and list(value) == ["Ref"] else None
                    held = template.get("Resources", {}).get(table, {}) if isinstance(table, str) else {}
                    unnamed = "TableName" not in held.get("Properties", {})
                    if isinstance(stack, str) and held.get("Type") == "AWS::DynamoDB::Table" and unnamed:
                        found.append(f"{stack}-{table}-*")
    return found


def verification_resources(
    templates: Templates,
    actions: list[dict[str, Any]],
    pipeline: Mapping[str, Any],
    project: str,
) -> tuple[dict[str, list[re.Pattern[str]]], set[str]]:
    """For each kind of resource of `VERIFICATION_MAY`, what the role of the verification of a
    stage may name, as `arn_text` writes it: the canary's secret, every workspace of the deploying
    account and region, the release parameters the stage's stacks hold, the heartbeats table the
    stage's verification is told of (`heartbeat_tables`), the stacks of the stage that hold its
    pool-state table and that table (`stage_pool_state`), the objects under
    `verification/` of the pipeline's artifact bucket, every resource for the describing of groups
    and their refreshes, the pipeline the stage is in, that bucket and its objects, and the
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
    pipeline_ids = [
        logical_id for _, logical_id, held, _ in resources(templates, "AWS::CodePipeline::Pipeline") if held is pipeline
    ]
    elsewhere = [template for template in templates.values() if not any(template is one for one in deployed)]
    tags = set().union(*map(workspace_tags, deployed)) - set().union(*map(workspace_tags, elsewhere))
    tables = stage_pool_state(templates, actions)
    allowed = {
        "secret": [re.compile(rf"{PARTITION}:secretsmanager:{HERE}:secret:{re.escape(CANARY_SECRET)}-\?{{6}}")],
        "workspaces": [re.compile(rf"{PARTITION}:aps:{HERE}:workspace/\*")],
        "parameters": [re.compile(rf"{PARTITION}:ssm:{HERE}:parameter{re.escape(name)}") for name in parameters],
        "heartbeats": [
            re.compile(rf"{PARTITION}:dynamodb:{HERE}:table/{re.escape(name)}")
            for name in heartbeat_tables(templates, actions)
        ],
        "pools stack": [
            re.compile(rf"{PARTITION}:cloudformation:{HERE}:stack/{re.escape(stack)}/\*")
            for stack in sorted({stack for stack, _ in tables})
        ],
        "pool-state": [
            re.compile(rf"{PARTITION}:dynamodb:{HERE}:table/{re.escape(f'{stack}-{logical_id}')}-\*")
            for stack, logical_id in tables
        ],
        "verification": [re.compile(rf"\$\{{{store}\.Arn\}}/{VERIFICATION_PREFIX}/\*") for store in stores],
        "every": [re.compile(r"\*")],
        "own pipeline": [
            re.compile(rf"{PARTITION}:codepipeline:{HERE}:\$\{{{re.escape(pipeline_id)}\}}")
            for pipeline_id in pipeline_ids
        ],
        "artifacts": [re.compile(rf"\$\{{{store}\.Arn\}}(/\*)?") for store in stores],
        "logs": [re.compile(rf"{PARTITION}:logs:{HERE}:log-group:/aws/codebuild/{own}(:\*)?")],
        "reports": [re.compile(rf"{PARTITION}:codebuild:{HERE}:report-group/{own}-\*")],
    }
    return allowed, tags


def verifies_more(statement: Mapping[str, Any], allowed: Mapping[str, list[re.Pattern[str]]], tags: set[str]) -> bool:
    """Whether an allowed statement of a verification's role reaches an action it is not given, a
    resource beyond those of the action's kind, where it reads or writes items of the pool-state
    table, any item but those of the pools the verification books: its condition must be the one
    `of_the_booked_pools` admits, alone, or, where it queries workspaces, any workspace but its
    stage's: its condition must be the one that admits a workspace by the stage's tag, alone."""
    if reports_more(statement, allowed, VERIFICATION_MAY):
        return True
    named = {action.lower() for action in as_list(statement.get("Action", [])) if isinstance(action, str)}
    items = {action.lower() for action, kind in VERIFICATION_MAY.items() if kind == "pool-state"}
    queries = {action.lower() for action, kind in VERIFICATION_MAY.items() if kind == "workspaces"}
    condition = statement.get("Condition")
    if named & items and not of_the_booked_pools(condition):
        return True
    if not named & queries:
        return False
    if not isinstance(condition, dict) or list(condition) != ["StringEquals"]:
        return True
    tagged = condition["StringEquals"]
    if not isinstance(tagged, dict) or list(tagged) != [f"aws:ResourceTag/{STAGE_TAG}"]:
        return True
    (value,) = tagged.values()
    return not (isinstance(value, str) and value in tags)


def of_the_booked_pools(condition: Any) -> bool:
    """Whether a statement's condition admits items by the names of `BOOKED_POOLS` and nothing
    else: the one operator of `OF_THE_BOOKED_POOLS` on its one key, with one of those names or both
    as its only values, and no other name."""
    if not isinstance(condition, dict) or list(condition) != list(OF_THE_BOOKED_POOLS):
        return False
    (keys,) = condition.values()
    if not isinstance(keys, dict) or list(keys) != ["dynamodb:LeadingKeys"]:
        return False
    pools = as_list(keys["dynamodb:LeadingKeys"])
    return bool(pools) and all(isinstance(pool, str) and pool in BOOKED_POOLS for pool in pools)


def holds(template: Mapping[str, Any]) -> list[tuple[list[dict[str, Any]], Mapping[str, Any], str, str | None]]:
    """The CodeBuild actions named `Hold` of the pipelines of a template, each as `verifications`
    gives the actions named `Verify`."""
    held = template.get("Resources", {})
    found: list[tuple[list[dict[str, Any]], Mapping[str, Any], str, str | None]] = []
    for _, _, pipeline, _ in resources({"": template}, "AWS::CodePipeline::Pipeline"):
        for stage, actions in stages_of(pipeline):
            for action in actions:
                if action["Name"] != HOLD_ACTION or type_of(action) != CODEBUILD:
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


def stage_pool_state(templates: Templates, actions: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The pool-state tables of the stacks a stage's CloudFormation actions deploy, each as `(the
    stack's name, the table's logical ID)`: a table with no name of its own whose logical ID begins
    with the pool tables' construct ID, after its parents'."""
    found = []
    deployments = [action for action in actions if type_of(action) == CLOUDFORMATION]
    for stack in sorted({str(action["Configuration"].get("StackName")) for action in deployments}):
        of_the_stack = [action for action in deployments if action["Configuration"].get("StackName") == stack]
        for template in deployed_by(of_the_stack, templates):
            for _, logical_id, table, _ in resources({"": template}, "AWS::DynamoDB::Table"):
                if "TableName" not in table and re.fullmatch(rf"\w*{POOL_STATE}[0-9A-F]{{8}}", logical_id):
                    found.append((stack, logical_id))
    return sorted(set(found))


def hold_resources(
    templates: Templates, actions: list[dict[str, Any]], pipeline: Mapping[str, Any], project: str
) -> dict[str, list[re.Pattern[str]]]:
    """For each kind of resource of `HOLD_MAY`, what the role of the hold of a stage may name, as
    `arn_text` writes it: the stacks of the stage that hold its pool-state table, that table, the
    pipeline's artifact bucket and its objects, and the project's own log group and report groups."""
    tables = stage_pool_state(templates, actions)
    stores = [
        re.escape(store["Ref"])
        for store in [pipeline.get("ArtifactStore", {}).get("Location")]
        + [entry.get("ArtifactStore", {}).get("Location") for entry in pipeline.get("ArtifactStores", [])]
        if isinstance(store, dict) and isinstance(store.get("Ref"), str)
    ]
    own = rf"\$\{{{re.escape(project)}\}}"
    return {
        "pools stack": [
            re.compile(rf"{PARTITION}:cloudformation:{HERE}:stack/{re.escape(stack)}/\*")
            for stack in sorted({stack for stack, _ in tables})
        ],
        "pool-state": [
            re.compile(rf"{PARTITION}:dynamodb:{HERE}:table/{re.escape(f'{stack}-{logical_id}')}-\*")
            for stack, logical_id in tables
        ],
        "artifacts": [re.compile(rf"\$\{{{store}\.Arn\}}(/\*)?") for store in stores],
        "logs": [re.compile(rf"{PARTITION}:logs:{HERE}:log-group:/aws/codebuild/{own}(:\*)?")],
        "reports": [re.compile(rf"{PARTITION}:codebuild:{HERE}:report-group/{own}-\*")],
    }


def promotions(template: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """The CodeBuild projects named `PROMOTION_PROJECT` of a template that holds a pipeline, each as
    `(its logical ID, its role)`, the role None when the template does not hold it."""
    if not resources({"": template}, "AWS::CodePipeline::Pipeline"):
        return []
    held = template.get("Resources", {})
    found: list[tuple[str, str | None]] = []
    for _, logical_id, project, _ in resources({"": template}, "AWS::CodeBuild::Project"):
        if project.get("Name") != PROMOTION_PROJECT:
            continue
        role = project.get("ServiceRole")
        role_id = role["Fn::GetAtt"][0] if isinstance(role, dict) and "Fn::GetAtt" in role else None
        found.append((logical_id, role_id if isinstance(role_id, str) and role_id in held else None))
    return found


def names_the_job(value: Any) -> bool:
    """Whether a value of a template holds text of the promotion project's ARN, or its name alone."""
    if isinstance(value, dict):
        return any(names_the_job(part) for part in value.values())
    if isinstance(value, list):
        return any(names_the_job(part) for part in value)
    return isinstance(value, str) and (value == PROMOTION_PROJECT or f"project/{PROMOTION_PROJECT}" in value)


def promotion_breaches(templates: Templates) -> list[tuple[str, str, str]]:
    """What the rules on the promotion job refuse: something that starts it, and another role that
    may write an image parameter or start an instance."""
    found = []
    jobs = [(path, project) for path, template in sorted(templates.items()) for project, _ in promotions(template)]
    exports = [
        output["Export"]["Name"]
        for path, project in jobs
        for output in templates[path].get("Outputs", {}).values()
        if "Name" in output.get("Export", {}) and names(output.get("Value"), project)
    ]
    for path, project in jobs:
        properties = templates[path]["Resources"][project].get("Properties", {})
        if properties.get("Triggers", {"Webhook": False}) != {"Webhook": False}:
            found.append((path, project, PROMOTION_STARTED))
    # A pipeline's action that runs it, and a rule's or a schedule's target.
    for path, pipeline_id, pipeline, _ in resources(templates, "AWS::CodePipeline::Pipeline"):
        own = [project for job_path, project in jobs if job_path == path]
        for stage, actions in stages_of(pipeline):
            for action in actions:
                configuration = action.get("Configuration", {})
                if (
                    any(names(configuration, project) for project in own)
                    or imports(configuration, exports)
                    or names_the_job(configuration)
                ):
                    found.append((path, f"{stage}/{action['Name']}", PROMOTION_STARTED))
    for kind, key in (("AWS::Events::Rule", "Targets"), ("AWS::Scheduler::Schedule", "Target")):
        for path, logical_id, properties, _ in resources(templates, kind):
            targets = properties.get(key)
            own = [project for job_path, project in jobs if job_path == path]
            if any(names(targets, project) for project in own) or imports(targets, exports) or names_the_job(targets):
                found.append((path, logical_id, PROMOTION_STARTED))
    # The image parameters, and the start of an instance, by the job's role alone.
    for path, template in sorted(templates.items()):
        promoters = {role for _, role in promotions(template)}
        for _, role, _, _ in resources({"": template}, "AWS::IAM::Role"):
            if role in promoters:
                continue
            if may(template, role, PARAMETER_WRITES, IMAGE_PARAMETER_ARN):
                found.append((path, role, WRITES_IMAGES))
            if may(template, role, INSTANCE_STARTS, INSTANCE_ARN):
                found.append((path, role, STARTS_INSTANCES))
    return found


def spike_jobs(template: Mapping[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """The spike jobs of a template, each as `(its logical ID, its name, its role)`: the CodeBuild
    projects one of whose build commands runs a script under `spikes/`. The name and the role are
    None when the template does not write the one or hold the other."""
    held = template.get("Resources", {})
    found: list[tuple[str, str | None, str | None]] = []
    for _, logical_id, project, _ in resources({"": template}, "AWS::CodeBuild::Project"):
        spec = project.get("Source", {}).get("BuildSpec")
        if not any(RUNS_A_SPIKE.search(command) for command in commands_of(spec) or []):
            continue
        role = project.get("ServiceRole")
        role_id = role["Fn::GetAtt"][0] if isinstance(role, dict) and "Fn::GetAtt" in role else None
        name = project.get("Name") if isinstance(project.get("Name"), str) else None
        found.append((logical_id, name, role_id if isinstance(role_id, str) and role_id in held else None))
    return found


def names_a_project(value: Any, name: str) -> bool:
    """Whether a value of a template holds a CodeBuild project's name anywhere in its text, as a
    whole name: alone, in the project's ARN, or inside text such as the JSON input of a schedule's
    universal target (`{"projectName": "<name>"}`)."""
    if isinstance(value, dict):
        return any(names_a_project(part, name) for part in value.values())
    if isinstance(value, list):
        return any(names_a_project(part, name) for part in value)
    return isinstance(value, str) and re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", value) is not None


def pool_state_tables(templates: Templates) -> list[str]:
    """The pool-state tables of the stacks the pipelines of `templates` deploy, each as the pattern
    of the name CloudFormation gives it: the stack's name, a hyphen, the table's logical ID, a
    hyphen and a wildcard for the generated suffix. A table is one when it has no name of its own
    and its logical ID begins with the pool tables' construct ID, after its parents'."""
    found = []
    for _, _, pipeline, _ in resources(templates, "AWS::CodePipeline::Pipeline"):
        for _, actions in stages_of(pipeline):
            deployments = [action for action in actions if type_of(action) == CLOUDFORMATION]
            for stack in sorted({str(action["Configuration"].get("StackName")) for action in deployments}):
                of_the_stack = [action for action in deployments if action["Configuration"].get("StackName") == stack]
                for template in deployed_by(of_the_stack, templates):
                    for _, logical_id, table, _ in resources({"": template}, "AWS::DynamoDB::Table"):
                        if "TableName" not in table and re.fullmatch(rf"\w*{POOL_STATE}[0-9A-F]{{8}}", logical_id):
                            found.append(f"{stack}-{logical_id}-*")
    return sorted(set(found))


def stage_tags(templates: Templates) -> set[str]:
    """The values of the tag `rhi-stage` on the workspaces of the stacks the pipelines of
    `templates` deploy, and of no other template."""
    deployed = [
        template
        for _, _, pipeline, _ in resources(templates, "AWS::CodePipeline::Pipeline")
        for _, actions in stages_of(pipeline)
        for template in deployed_by(actions, templates)
    ]
    elsewhere = [template for template in templates.values() if not any(template is one for one in deployed)]
    return set().union(*map(workspace_tags, deployed)) - set().union(*map(workspace_tags, elsewhere))


def spike_resources(
    templates: Templates, template: Mapping[str, Any], project: str
) -> dict[str, list[re.Pattern[str]]]:
    """For each kind of resource of `SPIKES_MAY`, what the role of the spike job `project` of
    `template` may name, as `arn_text` writes it."""
    properties = template["Resources"][project].get("Properties", {})
    group = properties.get("LogsConfig", {}).get("CloudWatchLogs", {}).get("GroupName")
    key = properties.get("EncryptionKey")
    own_logs = group.get("Ref") if isinstance(group, dict) else None
    own_key = key["Fn::GetAtt"][0] if isinstance(key, dict) and "Fn::GetAtt" in key else None
    connections = [logical_id for _, logical_id, _, _ in resources({"": template}, "AWS::CodeConnections::Connection")]
    pool_state = [
        re.compile(rf"{PARTITION}:dynamodb:{HERE}:table/{re.escape(name)}") for name in pool_state_tables(templates)
    ]
    return {
        "configuration": [CONFIGURATION],
        "pool-state": pool_state,
        MOCK_POOL_ITEMS: pool_state,
        "secret": [re.compile(rf"{PARTITION}:secretsmanager:{HERE}:secret:{re.escape(SPIKE_SECRET)}-\?{{6}}")],
        "workspaces": [re.compile(rf"{PARTITION}:aps:{HERE}:workspace/\*")],
        "bus": [re.compile(rf"{PARTITION}:events:{HERE}:event-bus/{MEASUREMENT_BUS}")],
        "connection": [re.compile(rf"\$\{{{re.escape(found)}\.ConnectionArn\}}") for found in connections],
        "logs": [re.compile(rf"\$\{{{re.escape(own_logs)}\.Arn\}}")] if isinstance(own_logs, str) else [],
        "key": [re.compile(rf"\$\{{{re.escape(own_key)}\.Arn\}}")] if isinstance(own_key, str) else [],
    }


def spikes_more(statement: Mapping[str, Any], allowed: Mapping[str, list[re.Pattern[str]]], tags: set[str]) -> bool:
    """Whether an allowed statement of a spike job's role reaches an action it is not given, a
    resource beyond those of the action's kind, where it writes items of the pool-state table, any
    item but the mock pool's: its condition must be `OF_THE_MOCK_POOL`, alone, or, where it queries
    workspaces, any workspace but its stage's: its condition must be the one that admits a workspace
    by the stage's tag, alone."""
    if reports_more(statement, allowed, SPIKES_MAY):
        return True
    named = {action.lower() for action in as_list(statement.get("Action", []))}
    writes = {action.lower() for action, kind in SPIKES_MAY.items() if kind == MOCK_POOL_ITEMS}
    if named & writes and not of_the_mock_pool(statement.get("Condition")):
        return True
    queries = {action.lower() for action, kind in SPIKES_MAY.items() if kind == "workspaces"}
    if not named & queries:
        return False
    return statement.get("Condition") not in [{"StringEquals": {f"aws:ResourceTag/{STAGE_TAG}": tag}} for tag in tags]


def spike_breaches(templates: Templates) -> list[tuple[str, str, str]]:
    """What the rules on the spike jobs refuse: something that starts one, and a role that may do
    more than a spike job does."""
    found = []
    jobs = [(path, *job) for path, template in sorted(templates.items()) for job in spike_jobs(template)]
    exports = [
        output["Export"]["Name"]
        for path, project, _, _ in jobs
        for output in templates[path].get("Outputs", {}).values()
        if "Name" in output.get("Export", {}) and names(output.get("Value"), project)
    ]

    def starts_one(value: Any, path: str) -> bool:
        own = [project for job_path, project, _, _ in jobs if job_path == path]
        return (
            any(names(value, project) for project in own)
            or imports(value, exports)
            or any(names_a_project(value, name) for _, _, name, _ in jobs if name is not None)
        )

    for path, project, _, _ in jobs:
        properties = templates[path]["Resources"][project].get("Properties", {})
        if properties.get("Triggers", {"Webhook": False}) != {"Webhook": False}:
            found.append((path, project, SPIKE_STARTED))
    for path, _, pipeline, _ in resources(templates, "AWS::CodePipeline::Pipeline"):
        for stage, actions in stages_of(pipeline):
            for action in actions:
                if starts_one(action.get("Configuration", {}), path):
                    found.append((path, f"{stage}/{action['Name']}", SPIKE_STARTED))
    for kind, key in (("AWS::Events::Rule", "Targets"), ("AWS::Scheduler::Schedule", "Target")):
        for path, logical_id, properties, _ in resources(templates, kind):
            if starts_one(properties.get(key), path):
                found.append((path, logical_id, SPIKE_STARTED))
    tags = stage_tags(templates)
    for path, project, _, role in jobs:
        template = templates[path]
        allowed = spike_resources(templates, template, project)
        statements = statements_of(template, role) if role is not None else None
        if statements is None or any(spikes_more(statement, allowed, tags) for statement in statements):
            found.append((path, role or project, SPIKES_MORE))
    return found


def canaries(template: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """The canary's projects of a template, those named `CANARY`, each as `(its logical ID, its
    role)`, the role None when the template does not hold it."""
    held = template.get("Resources", {})
    found: list[tuple[str, str | None]] = []
    for _, logical_id, project, _ in resources({"": template}, "AWS::CodeBuild::Project"):
        if project.get("Name") != CANARY:
            continue
        role = project.get("ServiceRole")
        role_id = role["Fn::GetAtt"][0] if isinstance(role, dict) and "Fn::GetAtt" in role else None
        found.append((logical_id, role_id if isinstance(role_id, str) and role_id in held else None))
    return found


def canary_schedules(template: Mapping[str, Any]) -> list[tuple[str, list[Any]]]:
    """The canary's schedules of a template, the rules and schedules named `CANARY` whatever they
    target, each as `(its logical ID, its targets)`."""
    found = []
    for kind, key in (("AWS::Events::Rule", "Targets"), ("AWS::Scheduler::Schedule", "Target")):
        for _, logical_id, properties, _ in resources({"": template}, kind):
            if properties.get("Name") == CANARY:
                found.append((logical_id, as_list(properties.get(key, []))))
    return found


def starts_more_than(template: Mapping[str, Any], role: Any, project: str) -> bool:
    """Whether the role a target names, by a reference to its ARN, may do more than start a build
    of the project `project` of the template. A role named any other way, or the template does not
    hold, is taken as one that may."""
    if role is None:
        return False
    role_id = role["Fn::GetAtt"][0] if isinstance(role, dict) and "Fn::GetAtt" in role else None
    if not isinstance(role_id, str) or role_id not in template.get("Resources", {}):
        return True
    statements = statements_of(template, role_id)
    if statements is None:
        return True
    for statement in statements:
        if "NotAction" in statement or "NotResource" in statement:
            return True
        if any(action != STARTS_THE_CANARY for action in as_list(statement.get("Action", []))):
            return True
        if any(arn_text(resource) != f"${{{project}.Arn}}" for resource in as_list(statement.get("Resource", []))):
            return True
    return False


def canary_breaches(templates: Templates) -> list[tuple[str, str, str]]:
    """What the rules on the canary refuse: a schedule that starts anything but the canary's job, a
    job without its one schedule, and a job whose role may start a pipeline's execution."""
    found = []
    for path, template in sorted(templates.items()):
        projects = canaries(template)
        schedules = canary_schedules(template)
        for project, role in projects:
            if len(schedules) != 1:
                found.append((path, project, CANARY_UNSCHEDULED))
            if role is None or may(template, role, PIPELINE_STARTS, PIPELINE_ARN):
                found.append((path, role or project, CANARY_STARTS_PIPELINES))
        for schedule, targets in schedules:
            starts_the_canary = len(projects) == 1 and len(targets) == 1
            if starts_the_canary:
                ((project, _),) = projects
                (target,) = targets
                starts_the_canary = (
                    isinstance(target, dict)
                    and target.get("Arn") == {"Fn::GetAtt": [project, "Arn"]}
                    and not starts_more_than(template, target.get("RoleArn"), project)
                )
            if not starts_the_canary:
                found.append((path, schedule, CANARY_STARTS_MORE))
    return found


def breaches(templates: Templates) -> list[tuple[str, str, str]]:
    """Everything of the templates that a rule refuses, as `(template path, what it is named,
    what is refused)`: an action by its stage and name, a resource by its logical ID."""
    found = []
    for path, pipeline_id, pipeline, template in resources(templates, "AWS::CodePipeline::Pipeline"):
        for stage, actions in stages_of(pipeline):
            for action in actions:
                name = f"{stage}/{action['Name']}"
                if stage != ASSET_STAGE and publishes_assets(action, template):
                    found.append((path, name, ASSETS_ELSEWHERE))
                if stage == ASSET_STAGE and type_of(action) != CODEBUILD:
                    found.append((path, name, NOT_CODEBUILD))
                if action["ActionTypeId"]["Category"] == "Approval":
                    found.append((path, name, MANUAL_APPROVAL))
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
    for path, template in sorted(templates.items()):
        for actions, pipeline, named, role in holds(template):
            allowed = hold_resources(templates, actions, pipeline, named)
            statements = statements_of(template, role) if role is not None else None
            if statements is None or any(reports_more(statement, allowed, HOLD_MAY) for statement in statements):
                found.append((path, role or named, HOLDS_MORE))
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
    # The model bucket, written by the role of the step that copies the weights and by no other.
    for path, template in sorted(templates.items()):
        copies = copy_roles(template)
        for _, role, _, _ in resources({"": template}, "AWS::IAM::Role"):
            if role not in copies and writes_weights(template, role):
                found.append((path, role, WRITES_WEIGHTS))
    found += promotion_breaches(templates)
    found += spike_breaches(templates)
    found += canary_breaches(templates)
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
    ("Rhi", []),
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
    approval: str | None = None,
    step: tuple[str, list[str]] | None = None,
    stack_with_an_asset: str | None = None,
    verification: bool = False,
    hold: bool = False,
    change: Change | None = None,
) -> dict[str, dict[str, Any]]:
    """The templates of an app, built in the test, whose pipeline CDK Pipelines makes as the
    design describes it (section 8.2): a source on a connection, a synth step, the self-update,
    and one stage with one stack, deployed with no approval. Every role carries the boundary.

    A test changes one thing:

    - the source: a CodeCommit repository in place of the connection, or another branch;
    - the boundary's name;
    - an approval before the stage's deployment: `broadening`, CDK Pipelines' approval on widened
      permissions, or `manual`, its manual approval step;
    - one more step before the stage's deployment, by its name and its commands;
    - one more stack in the stage, with a file asset and a role under the boundary of the name
      given or, when the name is empty, under none;
    - a verification: a CodeBuild step named `Verify` after the stage's deployment, and in the
      stage's stack what it reads, a workspace tagged with the stage's name, the release
      parameter of the pool `mock`, the table `Heartbeats`, whose name the stack declares as
      an output that the step is given, and a pool-state table, `PoolState`. Its role holds what
      CDK Pipelines gives it, until `change`;
    - a hold: a CodeBuild step named `Hold` before the stage's deployment, and in the stage's stack
      a pool-state table, `PoolState`. Its role holds what CDK Pipelines gives it, until `change`;
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
    told: dict[str, Any] = {}
    if verification:
        cdk.aws_aps.CfnWorkspace(endpoint, "Workspace", tags=[cdk.CfnTag(key=STAGE_TAG, value="Rhi")])
        cdk.aws_ssm.StringParameter(endpoint, "Release", parameter_name="/rhi/pools/mock/release", string_value="{}")
        dynamodb = cdk.aws_dynamodb
        table = dynamodb.CfnTable(
            endpoint,
            "Heartbeats",
            key_schema=[dynamodb.CfnTable.KeySchemaProperty(attribute_name="pool", key_type="HASH")],
            attribute_definitions=[
                dynamodb.CfnTable.AttributeDefinitionProperty(attribute_name="pool", attribute_type="S")
            ],
            billing_mode="PAY_PER_REQUEST",
        )
        told = {HEARTBEATS_TABLE: cdk.CfnOutput(endpoint, "HeartbeatsTable", value=table.ref)}
    if hold or verification:
        pool = cdk.aws_dynamodb.Attribute(name="pool", type=cdk.aws_dynamodb.AttributeType.STRING)
        cdk.aws_dynamodb.Table(endpoint, POOL_STATE, partition_key=pool)
    if stack_with_an_asset is not None:
        other = cdk.Stack(stage, "Other")
        (folder / "asset.txt").write_text("An asset.\n", encoding="utf-8")
        asset = cdk.aws_s3_assets.Asset(other, "Asset", path=str(folder / "asset.txt"))
        assumed_by = iam.ServicePrincipal("lambda.amazonaws.com")
        iam.Role(other, "Role", assumed_by=assumed_by, description=asset.s3_object_key)
        if stack_with_an_asset:
            bounded(other, stack_with_an_asset)

    gate = {
        None: [],
        "broadening": [pipelines.ConfirmPermissionsBroadening("Check", stage=stage)],
        "manual": [pipelines.ManualApprovalStep("Approve")],
    }[approval]
    more = [pipelines.ShellStep(step[0], commands=step[1])] if step is not None else []
    verify = (
        [pipelines.CodeBuildStep(VERIFY_ACTION, commands=["true"], env_from_cfn_outputs=told)] if verification else []
    )
    held = [pipelines.CodeBuildStep(HOLD_ACTION, commands=["true"])] if hold else []
    pipeline.add_stage(stage, pre=gate + more + held, post=verify)
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
    assert [what for what, _ in differences(templates, [BUILT[1], BUILT[0], *BUILT[2:]])] == [ANOTHER_ORDER]

    # The order is the one the actions run in, not the one the template declares them in: this
    # step comes before the deployment, the verification after it, whichever the template writes
    # first.
    both = built(cdk, tmp_path / "both", step=("Extra", ["true"]), verification=True)
    (pipeline,) = [properties for _, _, properties, _ in resources(both, "AWS::CodePipeline::Pipeline")]
    (rhi,) = [stage for stage in pipeline["Stages"] if stage["Name"] == "Rhi"]
    rhi["Actions"].reverse()
    extra, verify = ("Extra", CODEBUILD), (VERIFY_ACTION, CODEBUILD)
    assert differences(both, [*BUILT[:3], ("Rhi", [extra, verify])]) == []
    assert [what for what, _ in differences(both, [*BUILT[:3], ("Rhi", [verify, extra])])] == [ANOTHER_ORDER]


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


# The emulator and the buildx plugin as a project that publishes images installs them, pinned.
PLUGIN = "$HOME/.docker/cli-plugins/docker-buildx"
EMULATOR_FOLDER = "/opt/binfmt"
EMULATOR_URL = "https://github.com/tonistiigi/binfmt/releases/download/deploy/v10.2.3-68"
IMAGE_BUILDER = [
    *(
        command
        for file in ("binfmt_linux-amd64.tar.gz", "qemu_v10.2.3_linux-amd64.tar.gz")
        for command in (
            f'curl -fsSL --create-dirs -o "{EMULATOR_FOLDER}/{file}" {EMULATOR_URL}/{file}',
            f'echo "{"0" * 64}  {EMULATOR_FOLDER}/{file}" | sha256sum -c -',
            f'tar -xzf "{EMULATOR_FOLDER}/{file}" -C "{EMULATOR_FOLDER}"',
        )
    ),
    f"QEMU_BINARY_PATH={EMULATOR_FOLDER} {EMULATOR_FOLDER}/binfmt --install arm64",
    (
        f'curl -fsSL --create-dirs -o "{PLUGIN}" '
        "https://github.com/docker/buildx/releases/download/v0.38.2/buildx-v0.38.2.linux-amd64"
    ),
    f'echo "{"0" * 64}  {PLUGIN}" | sha256sum -c -',
    f'chmod +x "{PLUGIN}"',
]
# What a project that publishes images installed before, each image pinned by its digest: the
# emulator from its image, and a builder that ran BuildKit from its image. Both are on Docker Hub.
PINNED = "@sha256:" + "0" * 64
RUNS_THE_EMULATOR_IMAGE = f"docker run --privileged --rm tonistiigi/binfmt:qemu-v10.2.3{PINNED} --install arm64"
CREATES_A_BUILDER = (
    f"docker buildx create --name rhi-assets --driver docker-container --driver-opt image=moby/buildkit:v0.34.2{PINNED}"
    " --driver-opt default-load=true --bootstrap --use"
)


# In `IMAGE_BUILDER`, the download of the emulator's tool, the check of its checksum and its
# extraction, in that order.
DOWNLOADS_THE_TOOL, CHECKS_THE_TOOL, EXTRACTS_THE_TOOL = IMAGE_BUILDER[:3]
INSTALLS_CDK_ASSETS = "npm install -g cdk-assets@latest"


def a_publisher_that_installs(installs: list[str]) -> Change:
    """Adds to the asset stage a project that installs the publisher and `installs`, then publishes."""
    return a_publisher_with({"install": [INSTALLS_CDK_ASSETS, *installs], "build": [PUBLISH]})


def a_publisher_with(commands: dict[str, list[str]], last: dict[str, list[str]] | None = None) -> Change:
    """Adds to the asset stage a project whose build specification writes these phases, in this
    order, each with its commands, and with those of `last` in its `finally` block."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        codebuild = cdk.aws_codebuild
        phases: dict[str, dict[str, list[str]]] = {phase: {"commands": commands[phase]} for phase in commands}
        for phase, run_last in (last or {}).items():
            phases[phase]["finally"] = run_last
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
            a_publisher_that_installs(
                [command.replace("download/deploy/v10.2.3-68", "latest/download") for command in IMAGE_BUILDER]
            ),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that downloads the emulator release without its exact version",
        ),
        pytest.param(
            a_publisher_that_installs([command for command in IMAGE_BUILDER if command != CHECKS_THE_TOOL]),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that extracts the emulator's tool without checking its checksum",
        ),
        pytest.param(
            a_publisher_that_installs(
                [DOWNLOADS_THE_TOOL, EXTRACTS_THE_TOOL, CHECKS_THE_TOOL, *IMAGE_BUILDER[3:]],
            ),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that extracts the emulator's tool before checking its checksum",
        ),
        pytest.param(
            a_publisher_with(
                {
                    "build": [DOWNLOADS_THE_TOOL, CHECKS_THE_TOOL, PUBLISH],
                    "install": [INSTALLS_CDK_ASSETS, *IMAGE_BUILDER[2:]],
                }
            ),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that writes the check first, in a phase that runs after the extraction",
        ),
        pytest.param(
            a_publisher_with(
                {"install": [INSTALLS_CDK_ASSETS, DOWNLOADS_THE_TOOL, CHECKS_THE_TOOL], "build": [PUBLISH]},
                last={"install": IMAGE_BUILDER[2:]},
            ),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that extracts the emulator's tool in a block a failed check does not end",
        ),
        pytest.param(
            a_publisher_with(
                {
                    "build": [INSTALLS_CDK_ASSETS, DOWNLOADS_THE_TOOL, CHECKS_THE_TOOL],
                    "post_build": [*IMAGE_BUILDER[2:], PUBLISH],
                }
            ),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that extracts the emulator's tool in a phase a failed check does not end",
        ),
        pytest.param(
            a_publisher_that_installs(
                [command.replace("qemu_v10.2.3_linux", "qemu_v9.2.2_linux") for command in IMAGE_BUILDER]
            ),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage whose emulators are not those of the release it downloads",
        ),
        pytest.param(
            a_publisher_that_installs([*IMAGE_BUILDER, RUNS_THE_EMULATOR_IMAGE]),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that runs the emulator image, pinned by digest",
        ),
        pytest.param(
            a_publisher_that_installs([*IMAGE_BUILDER, CREATES_A_BUILDER]),
            [f"stage {ASSET_STAGE}", f"{ASSET_STAGE}/Image: {CODEBUILD}"],
            id="a project of the asset stage that creates a builder from the BuildKit image, pinned by digest",
        ),
        pytest.param(
            a_publisher_that_installs(
                [command.replace("download/v0.38.2", "latest/download") for command in IMAGE_BUILDER]
            ),
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


def an_approval_in(stage: str) -> Change:
    """Adds a manual approval action, CodePipeline's own, to a stage of the pipeline."""

    def change(cdk: ModuleType, _stack: Any, pipeline: Any) -> None:
        pipeline.stage(stage).add_action(cdk.aws_codepipeline_actions.ManualApprovalAction(action_name="Extra"))

    return change


def a_deletion_in_the_asset_stage(cdk: ModuleType, _stack: Any, pipeline: Any) -> None:
    delete = cdk.aws_codepipeline_actions.CloudFormationDeleteStackAction
    action = delete(action_name="Delete", stack_name="Rhi-Other", admin_permissions=False)
    pipeline.stage(ASSET_STAGE).add_action(action)


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
            {"approval": "broadening"}, MANUAL_APPROVAL, id="a pipeline with the approval on widened permissions"
        ),
        # The clauses of the rules that the four above leave out.
        pytest.param({"approval": "manual"}, MANUAL_APPROVAL, id="a pipeline with a manual approval step"),
        pytest.param({"change": an_approval_in("Build")}, MANUAL_APPROVAL, id="a manual approval in another stage"),
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
            {"stack_with_an_asset": BOUNDARY, "change": a_deletion_in_the_asset_stage},
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
    # And so is the heartbeats table it is told of, the one table its role may query: the pools
    # stack's, by the logical ID recorded for it, and no other action of DynamoDB.
    recorded = json.loads((Path(__file__).resolve().parent / "stateful_logical_ids.json").read_text(encoding="utf-8"))
    (table,) = heartbeat_tables(templates, actions)
    assert table in {f"{stack}-{logical_id}-*" for stack, ids in recorded.items() for logical_id in ids}
    (template,) = [template for template in templates.values() if role in template["Resources"]]
    on_tables = [
        statement
        for statement in statements_of(template, role) or []
        if any(str(action).lower().startswith("dynamodb:") for action in as_list(statement.get("Action")))
    ]
    (heartbeats,) = allowed["heartbeats"]
    (query,) = [statement for statement in on_tables if statement["Action"] == "dynamodb:Query"]
    assert heartbeats.fullmatch(arn_text(query["Resource"]) or "")
    # And so is the stage's pool-state table, by the logical ID recorded for it: its role reads
    # that one stack's resources, and reads and writes that one table's items under the condition
    # that names the partition keys of the two pools it books, the mock pool's and the coursework
    # pool's, and nothing else of CloudFormation or DynamoDB.
    ((stack, logical_id),) = stage_pool_state(templates, actions)
    assert logical_id in recorded[stack]
    ((stacks,), (pool_state,)) = allowed["pools stack"], allowed["pool-state"]
    (items,) = [statement for statement in on_tables if statement is not query]
    assert sorted(items["Action"]) == ["dynamodb:GetItem", "dynamodb:PutItem"]
    assert pool_state.fullmatch(arn_text(items["Resource"]) or "") and items["Condition"] == OF_THE_BOOKED_POOLS
    (describe,) = [
        statement
        for statement in statements_of(template, role) or []
        if any(str(action).lower().startswith("cloudformation:") for action in as_list(statement.get("Action")))
    ]
    assert describe["Action"] == "cloudformation:DescribeStackResource"
    assert stacks.fullmatch(arn_text(describe["Resource"]) or "")
    # And, for the decision on the coursework pool, the describing of groups and their refreshes on
    # every resource, and the listing of the executions of the pipeline it is in, and nothing else
    # of Auto Scaling or CodePipeline.
    (own,) = allowed["own pipeline"]

    def of(service: str) -> list[tuple[Any, str]]:
        return [
            (s["Action"], arn_text(s["Resource"]) or "")
            for s in statements_of(template, role) or []
            if any(str(action).lower().startswith(f"{service}:") for action in as_list(s.get("Action")))
        ]

    assert of("autoscaling") == [
        (["autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeInstanceRefreshes"], "*")
    ]
    ((action, resource),) = of("codepipeline")
    assert action == "codepipeline:ListPipelineExecutions" and own.fullmatch(resource)


def the_workspaces(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return stack.format_arn(service="aps", resource="workspace", resource_name="*")


def the_parameter(name: str) -> Names:
    def arn(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        return stack.format_arn(service="ssm", resource="parameter", resource_name=name)

    return arn


def the_table(name: str) -> Names:
    def arn(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        return stack.format_arn(service="dynamodb", resource="table", resource_name=name)

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


def the_pool_state(suffix: str = "-*") -> Names:
    """The pool-state table of the stage of `built`, by the name CloudFormation gives it, with
    `suffix` after its logical ID."""

    def arn(cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        endpoint = stack.node.find_child("Rhi").node.find_child("Endpoint")
        table = endpoint.node.find_child(POOL_STATE).node.default_child
        name = f"Rhi-Endpoint-{endpoint.resolve(table.logical_id)}{suffix}"
        return stack.format_arn(service="dynamodb", resource="table", resource_name=name)

    return arn


def of_pool(*pools: str, operator: str = "ForAllValues:StringEquals") -> dict[str, Any]:
    """The condition that admits an item of a table by its partition key, the name of one of `pools`."""
    return {operator: {"dynamodb:LeadingKeys": list(pools)}}


# One more statement of a verification's role: its action, what it names and its condition.
More = tuple[str, "Names", "dict[str, Any] | None"]
# The heartbeats table of the stage of `built`, as CloudFormation names it: the stage deploys the
# stack `Rhi-Endpoint`, which holds the table under the logical ID `Heartbeats`.
HEARTBEATS = "Rhi-Endpoint-Heartbeats-*"
# The narrow role: what the verification of the stage of `built` reads and writes. Of the
# pool-state table, the items of the pools it books: one statement names one of them, one both.
VERIFIES: list[More] = [
    ("secretsmanager:GetSecretValue", the_secret(f"{CANARY_SECRET}-??????"), None),
    ("aps:QueryMetrics", the_workspaces, tagged("Rhi")),
    ("ssm:GetParameter", the_parameter("rhi/pools/mock/release"), None),
    ("dynamodb:Query", the_table(HEARTBEATS), None),
    ("cloudformation:DescribeStackResource", the_stack("Rhi-Endpoint"), None),
    ("dynamodb:GetItem", the_pool_state(), of_pool(MOCK_POOL)),
    ("dynamodb:PutItem", the_pool_state(), of_pool(MOCK_POOL, COURSEWORK_POOL)),
    ("s3:PutObject", under(VERIFICATION_PREFIX), None),
    ("autoscaling:DescribeAutoScalingGroups", every_resource, None),
    ("autoscaling:DescribeInstanceRefreshes", every_resource, None),
    ("codepipeline:ListPipelineExecutions", the_pipeline, None),
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


# A verification that may write to every resource: the model bucket's objects are among them, and
# only the copy step's role may write those, so this one is refused under that rule as well.
ON_EVERY_RESOURCE = a_verification(("s3:PutObject", every_resource, None))


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
        pytest.param(
            a_verification(("dynamodb:Query", the_table("Rhi-Endpoint-Other-*"), None)),
            id="another table of the stage",
        ),
        pytest.param(
            a_verification(("dynamodb:Query", the_table("Rhi-Endpoint-Heartbeats*"), None)),
            id="every table whose logical ID starts as the heartbeats table's",
        ),
        pytest.param(a_verification(("dynamodb:Query", the_table("*"), None)), id="every table"),
        pytest.param(
            a_verification(("dynamodb:Scan", the_table(HEARTBEATS), None)), id="a scan of the heartbeats table"
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_table(HEARTBEATS), None)), id="a write to the heartbeats table"
        ),
        pytest.param(
            a_verification(("dynamodb:Query", the_table(f"{HEARTBEATS}/index/*"), None)),
            id="the indexes of the heartbeats table",
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_pool_state(), of_pool("other-1"))),
            id="a write of a third pool's items",
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_pool_state(), None)), id="a write that names no partition key"
        ),
        pytest.param(
            a_verification(("dynamodb:GetItem", the_pool_state(), of_pool("other-1"))),
            id="a read of a third pool's items",
        ),
        pytest.param(
            a_verification(("dynamodb:GetItem", the_pool_state(), None)), id="a read that names no partition key"
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_pool_state(), of_pool(MOCK_POOL, COURSEWORK_POOL, "other-1"))),
            id="a write of the items of the two pools and of a third",
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_pool_state(), of_pool())),
            id="a write whose condition names no pool",
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_pool_state(), of_pool("*"))),
            id="a write whose partition key is a wildcard",
        ),
        pytest.param(
            a_verification(
                ("dynamodb:PutItem", the_pool_state(), of_pool(MOCK_POOL, operator="ForAllValues:StringLike"))
            ),
            id="a write of the mock pool's items under another operator",
        ),
        pytest.param(
            a_verification(
                ("dynamodb:PutItem", the_pool_state(), {"StringEquals": {"aws:RequestedRegion": "eu-west-1"}})
            ),
            id="a write under a condition on another key",
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_table("Rhi-Endpoint-Other-*"), of_pool(MOCK_POOL))),
            id="a write of the mock pool's items of another table",
        ),
        pytest.param(
            a_verification(("dynamodb:GetItem", the_table(HEARTBEATS), of_pool(MOCK_POOL))),
            id="a read of the mock pool's items of the heartbeats table",
        ),
        pytest.param(
            a_verification(("dynamodb:PutItem", the_pool_state("*"), of_pool(MOCK_POOL))),
            id="a write to every table whose name starts as the pool-state table's",
        ),
        pytest.param(
            a_verification(("dynamodb:DeleteItem", the_pool_state(), of_pool(MOCK_POOL))),
            id="another action on the mock pool's items",
        ),
        pytest.param(
            a_verification(("dynamodb:Query", the_pool_state(), of_pool(MOCK_POOL))),
            id="a query of the pool-state table",
        ),
        pytest.param(
            a_verification(("cloudformation:DescribeStackResource", the_stack("Rhi-Other"), None)),
            id="the resources of another stack",
        ),
        pytest.param(
            a_verification(("cloudformation:DescribeStackResource", every_resource, None)),
            id="the resources of every stack",
        ),
        pytest.param(
            a_verification(("cloudformation:DescribeStacks", the_stack("Rhi-Endpoint"), None)),
            id="another action on the pools stack",
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
        pytest.param(ON_EVERY_RESOURCE, id="every resource"),
        pytest.param(
            a_verification(("autoscaling:UpdateAutoScalingGroup", every_resource, None)),
            id="a change of a group",
        ),
        pytest.param(
            a_verification(("autoscaling:StartInstanceRefresh", every_resource, None)),
            id="the start of a refresh",
        ),
        pytest.param(
            a_verification(("codepipeline:ListPipelineExecutions", another_pipeline, None)),
            id="the executions of another pipeline",
        ),
        pytest.param(
            a_verification(("codepipeline:ListPipelineExecutions", every_resource, None)),
            id="the executions of every pipeline",
        ),
        pytest.param(
            a_verification(("codepipeline:StartPipelineExecution", the_pipeline, None)),
            id="another action on the pipeline",
        ),
        pytest.param(a_verification(managed=True), id="a managed policy the template does not hold"),
    ],
)
def test_refuses_a_verification_that_may_do_more(cdk: ModuleType, tmp_path: Path, change: Change) -> None:
    assert refused(built(cdk, tmp_path / "kept", verification=True, change=a_verification())) == set()
    also = {WRITES_WEIGHTS} if change is ON_EVERY_RESOURCE else set()
    assert refused(built(cdk, tmp_path / "breached", verification=True, change=change)) == {VERIFIES_MORE} | also


def test_refuses_a_verification_that_writes_to_every_resource(cdk: ModuleType, tmp_path: Path) -> None:
    # Every resource holds the model bucket's objects too, which only the copy step's role may write.
    change = a_verification(("s3:PutObject", every_resource, None))
    assert refused(built(cdk, tmp_path, verification=True, change=change)) == {VERIFIES_MORE, WRITES_WEIGHTS}


def test_the_hold_s_role_is_the_one_held(templates: dict[str, dict[str, Any]]) -> None:
    # The rule holds a role only once it finds it: the app's hold is found, with its role, and so is
    # the stage's pool-state table, by the logical ID recorded for it.
    (found,) = [found for template in templates.values() for found in holds(template)]
    actions, pipeline, project, role = found
    assert role is not None
    recorded = json.loads((Path(__file__).resolve().parent / "stateful_logical_ids.json").read_text(encoding="utf-8"))
    ((stack, logical_id),) = stage_pool_state(templates, actions)
    assert logical_id in recorded[stack]
    # Its role reads that one stack's resources and that one table's items, and nothing else of
    # CloudFormation or DynamoDB.
    allowed = hold_resources(templates, actions, pipeline, project)
    (template,) = [template for template in templates.values() if role in template["Resources"]]

    def of(service: str) -> list[tuple[Any, str]]:
        return [
            (s["Action"], arn_text(s["Resource"]) or "")
            for s in statements_of(template, role) or []
            if any(str(action).lower().startswith(f"{service}:") for action in as_list(s.get("Action")))
        ]

    ((stacks,), (tables,)) = allowed["pools stack"], allowed["pool-state"]
    ((action, resource),) = of("cloudformation")
    assert action == "cloudformation:DescribeStackResource" and stacks.fullmatch(resource)
    ((action, resource),) = of("dynamodb")
    assert action == "dynamodb:GetItem" and tables.fullmatch(resource)


# The narrow role: what the hold of the stage of `built` reads.
HOLDS: list[tuple[str, Names]] = [
    ("cloudformation:DescribeStackResource", the_stack("Rhi-Endpoint")),
    ("dynamodb:GetItem", the_pool_state()),
]


def a_hold(more: tuple[str, Names] | None = None) -> Change:
    """Gives the role of the hold of `built` the narrow statements and, when asked, one more."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        (project,) = [
            node
            for node in stack.node.find_all()
            if isinstance(node, cdk.aws_codebuild.Project) and f"/Rhi/{HOLD_ACTION}" in node.node.path
        ]
        for action, arn in [*HOLDS, *([more] if more is not None else [])]:
            project.role.add_to_principal_policy(
                cdk.aws_iam.PolicyStatement(actions=[action], resources=[arn(cdk, stack, pipeline)])
            )

    return change


def test_keeps_a_hold_with_the_narrow_role(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path, hold=True, change=a_hold())
    ((_, _, _, role),) = [found for template in templates.values() for found in holds(template)]
    assert role is not None
    assert breaches(templates) == []


@pytest.mark.parametrize(
    "more",
    [
        pytest.param(("cloudformation:DescribeStackResource", the_stack("Rhi-Other")), id="another stack"),
        pytest.param(("cloudformation:DescribeStackResource", every_resource), id="every stack"),
        pytest.param(("cloudformation:DescribeStacks", the_stack("Rhi-Endpoint")), id="another action on the stack"),
        pytest.param(("dynamodb:GetItem", the_table("Rhi-Endpoint-Other-*")), id="another table"),
        pytest.param(("dynamodb:GetItem", the_pool_state("*")), id="every table whose name starts as the table's"),
        pytest.param(("dynamodb:PutItem", the_pool_state()), id="a write to the table"),
        pytest.param(("dynamodb:Scan", the_pool_state()), id="a scan of the table"),
        pytest.param(("secretsmanager:GetSecretValue", the_secret(f"{CANARY_SECRET}-??????")), id="a secret"),
    ],
)
def test_refuses_a_hold_that_may_do_more(cdk: ModuleType, tmp_path: Path, more: tuple[str, Names]) -> None:
    assert refused(built(cdk, tmp_path / "kept", hold=True, change=a_hold())) == set()
    assert refused(built(cdk, tmp_path / "breached", hold=True, change=a_hold(more))) == {HOLDS_MORE}


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
        pytest.param(a_role(reads=the_secret(f"{REPORTER_SECRET}-GhIjKl")), {SECRET_ELSEWHERE}, id="by its full name"),
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


def the_models(objects: str | None = "*") -> Names:
    """The objects of the model bucket of the deploying account and region that a pattern names,
    the bucket by its fixed name; or, with no pattern, the bucket itself."""

    def arn(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
        bucket = f"{MODEL_BUCKET}-{stack.account}-{stack.region}"
        resource = bucket if objects is None else f"{bucket}/{objects}"
        return stack.format_arn(service="s3", region="", account="", resource=resource)

    return arn


def a_bucket_named_as_the_models(cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    """The objects of a bucket of the template that carries the model bucket's name, by a reference
    to the bucket, as a grant of the stack that creates the model bucket would name them."""
    bucket = cdk.aws_s3.Bucket(stack, "Models", bucket_name=f"{MODEL_BUCKET}-{stack.account}-{stack.region}")
    return bucket.arn_for_objects("*")


# The step that copies the weights, as `built` adds a step, and what its role holds of the model bucket.
COPY: tuple[str, list[str]] = (COPY_ACTION, ["true"])
COPIES: list[tuple[list[str], Names]] = [
    (["s3:ListBucket"], the_models(None)),
    (["s3:PutObject", "s3:DeleteObject"], the_models()),
]


def a_copy(then: Change | None = None) -> Change:
    """Gives the role of the step `CopyWeights` of `built` what it holds of the model bucket, then
    makes the change `then`, when there is one."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        (project,) = [
            node
            for node in stack.node.find_all()
            if isinstance(node, cdk.aws_codebuild.Project) and f"/Rhi/{COPY_ACTION}" in node.node.path
        ]
        for actions, arn in COPIES:
            statement = cdk.aws_iam.PolicyStatement(actions=actions, resources=[arn(cdk, stack, pipeline)])
            project.role.add_to_principal_policy(statement)
        if then is not None:
            then(cdk, stack, pipeline)

    return change


def test_the_copy_step_s_role_is_the_one_held(templates: dict[str, dict[str, Any]]) -> None:
    # The rule leaves out a role only once it finds it: the app's copy step is found, with a role
    # that the rule reads as writing to the model bucket.
    ((template, role),) = [(template, role) for template in templates.values() for role in copy_roles(template)]
    assert writes_weights(template, role)
    # And the bucket the rule guards is the app's: the one bucket of the stage whose name starts as
    # the rule's, which a reference to it in its own template is read as naming.
    ((held, bucket),) = [
        (held, logical_id)
        for _, logical_id, properties, held in resources(templates, "AWS::S3::Bucket")
        if (arn_text(properties.get("BucketName")) or "").startswith(f"{MODEL_BUCKET}-")
    ]
    objects = PSEUDO_FIELD.sub(FIELD, with_its_buckets_named(f"${{{bucket}.Arn}}/*", held))
    assert overlaps(MODEL_OBJECT_ARN, objects)


def test_keeps_the_copy_step_as_the_model_bucket_s_one_writer(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path / "copies", step=COPY, change=a_copy())
    ((template, role),) = [(template, role) for template in templates.values() for role in copy_roles(template)]
    assert writes_weights(template, role)
    assert breaches(templates) == []

    # Another role may read the bucket's objects, as a GPU pool's instances do, and write to
    # another bucket, one of its own template that CloudFormation names.
    reads = a_role(reads=the_models(), action="s3:GetObject")
    assert breaches(built(cdk, tmp_path / "reads", step=COPY, change=a_copy(reads))) == []
    elsewhere = a_role(reads=another_bucket, action="s3:PutObject")
    assert breaches(built(cdk, tmp_path / "elsewhere", step=COPY, change=a_copy(elsewhere))) == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(a_role(reads=the_models(), action="s3:PutObject"), id="by the bucket's name"),
        pytest.param(
            a_role(reads=the_models("coursework/*"), action="s3:DeleteObject"), id="a deletion under one pool's prefix"
        ),
        pytest.param(a_role(reads=every_resource, action="s3:PutObject"), id="on every resource"),
        pytest.param(a_role(reads=the_models(), action="s3:*"), id="by a wildcard action"),
        pytest.param(
            a_role(reads=a_bucket_named_as_the_models, action="s3:PutObject"),
            id="by a reference to a bucket of the model bucket's name",
        ),
    ],
)
def test_refuses_another_role_that_writes_to_the_model_bucket(cdk: ModuleType, tmp_path: Path, change: Change) -> None:
    assert refused(built(cdk, tmp_path / "breached", step=COPY, change=a_copy(change))) == {WRITES_WEIGHTS}
    # With no copy step, the bucket has no writer at all: the same role is refused.
    assert refused(built(cdk, tmp_path / "alone", change=change)) == {WRITES_WEIGHTS}


def test_the_promotion_job_s_role_is_the_one_held(templates: dict[str, dict[str, Any]]) -> None:
    # The rules leave out a role only once they find it: the app's promotion job is found, in the
    # pipeline's template, with a role the rules read as writing an image parameter and starting an
    # instance.
    ((template, role),) = [(template, role) for template in templates.values() for _, role in promotions(template)]
    assert role is not None
    assert may(template, role, PARAMETER_WRITES, IMAGE_PARAMETER_ARN)
    assert may(template, role, INSTANCE_STARTS, INSTANCE_ARN)


def the_launch_templates(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return stack.format_arn(service="ec2", resource="launch-template", resource_name="*")


def the_instances(_cdk: ModuleType, stack: Any, _pipeline: Any) -> str:
    return stack.format_arn(service="ec2", resource="instance", resource_name="*")


# What the promotion job's role holds of the image parameters and of a start, in `a_promotion`.
PROMOTES: list[tuple[list[str], Names]] = [
    (["ssm:PutParameter"], the_parameter("rhi/images/*")),
    (["ec2:RunInstances"], the_launch_templates),
    (["ec2:RunInstances"], the_instances),
]


def a_promotion(then: Change | None = None) -> Change:
    """Adds to the pipeline's stack the promotion job, a project of its name whose role writes the
    image parameters and starts an instance, then makes the change `then`, when there is one."""

    def change(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
        iam, codebuild = cdk.aws_iam, cdk.aws_codebuild
        role = iam.Role(stack, "PromotionRole", assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"))
        for actions, arn in PROMOTES:
            role.add_to_policy(iam.PolicyStatement(actions=actions, resources=[arn(cdk, stack, pipeline)]))
        spec = codebuild.BuildSpec.from_object({"version": "0.2", "phases": {"build": {"commands": ["true"]}}})
        codebuild.Project(stack, "Promotion", project_name=PROMOTION_PROJECT, role=role, build_spec=spec)
        if then is not None:
            then(cdk, stack, pipeline)

    return change


def the_job(stack: Any) -> Any:
    return stack.node.find_child("Promotion")


def a_rule_that_promotes(cdk: ModuleType, stack: Any, _pipeline: Any) -> None:
    nightly = cdk.aws_events.Schedule.rate(cdk.Duration.days(1))
    target = cdk.aws_events_targets.CodeBuildProject(the_job(stack))
    cdk.aws_events.Rule(stack, "NightlyPromotion", schedule=nightly, targets=[target])


def a_schedule_that_promotes(cdk: ModuleType, stack: Any, _pipeline: Any) -> None:
    nightly = cdk.aws_scheduler.ScheduleExpression.rate(cdk.Duration.days(1))
    target = cdk.aws_scheduler_targets.CodeBuildStartBuild(the_job(stack))
    cdk.aws_scheduler.Schedule(stack, "NightlyPromotion", schedule=nightly, target=target)


def a_rule_of_another_stack_that_promotes(cdk: ModuleType, stack: Any, _pipeline: Any) -> None:
    other = cdk.Stack(stack.node.scope, "Nightly")
    boundary = cdk.aws_iam.ManagedPolicy.from_managed_policy_name(other, "Boundary", BOUNDARY)
    cdk.aws_iam.PermissionsBoundary.of(other).apply(boundary)
    nightly = cdk.aws_events.Schedule.rate(cdk.Duration.days(1))
    target = cdk.aws_events_targets.CodeBuildProject(the_job(stack))
    cdk.aws_events.Rule(other, "NightlyPromotion", schedule=nightly, targets=[target])


def an_action_that_promotes(cdk: ModuleType, stack: Any, pipeline: Any) -> None:
    synthesized = cdk.aws_codepipeline.Artifact("Synth_Output")
    action = cdk.aws_codepipeline_actions.CodeBuildAction(
        action_name="Promote", project=the_job(stack), input=synthesized, run_order=9
    )
    pipeline.stage("Rhi").add_action(action)


def a_webhook_on_the_job(_cdk: ModuleType, stack: Any, _pipeline: Any) -> None:
    the_job(stack).node.default_child.add_property_override("Triggers", {"Webhook": True})


def test_keeps_the_promotion_job_as_the_one_writer_of_the_images(cdk: ModuleType, tmp_path: Path) -> None:
    templates = built(cdk, tmp_path / "promotes", change=a_promotion())
    assert [role for template in templates.values() for _, role in promotions(template)] != [None]
    assert breaches(templates) == []

    # Another role may read the image parameters, and stop or terminate an instance.
    reads = a_role(reads=the_parameter("rhi/images/*"), action="ssm:GetParameter")
    assert breaches(built(cdk, tmp_path / "reads", change=a_promotion(reads))) == []
    stops = a_role(reads=the_instances, action="ec2:TerminateInstances")
    assert breaches(built(cdk, tmp_path / "stops", change=a_promotion(stops))) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(
            a_role(reads=the_parameter("rhi/images/coursework"), action="ssm:PutParameter"),
            {WRITES_IMAGES},
            id="another role that writes a pool's image parameter",
        ),
        pytest.param(
            a_role(reads=the_parameter("rhi/*"), action="ssm:*"),
            {WRITES_IMAGES},
            id="another role that writes every parameter by wildcards",
        ),
        pytest.param(
            a_role(reads=every_resource, action="ec2:RunInstances"),
            {STARTS_INSTANCES},
            id="another role that starts an instance",
        ),
        pytest.param(
            a_role(reads=the_instances, action="ec2:StartInstances"),
            {STARTS_INSTANCES},
            id="another role that starts a stopped instance",
        ),
        pytest.param(a_rule_that_promotes, {PROMOTION_STARTED}, id="a rule that starts the promotion job"),
        pytest.param(a_schedule_that_promotes, {PROMOTION_STARTED}, id="a schedule that starts the promotion job"),
        pytest.param(
            a_rule_of_another_stack_that_promotes,
            {PROMOTION_STARTED},
            id="a rule of another stack that starts the promotion job",
        ),
        pytest.param(
            an_action_that_promotes, {PROMOTION_STARTED}, id="a pipeline's action that runs the promotion job"
        ),
        pytest.param(a_webhook_on_the_job, {PROMOTION_STARTED, WEBHOOK}, id="a webhook on the promotion job"),
    ],
)
def test_refuses_another_writer_of_the_images_and_a_trigger_of_the_job(
    cdk: ModuleType, tmp_path: Path, change: Change, expected: set[str]
) -> None:
    assert refused(built(cdk, tmp_path / "kept", change=a_promotion())) == set()
    assert refused(built(cdk, tmp_path / "breached", change=a_promotion(change))) == expected


# Two spike jobs, as their issues add them to the list of `src/rhi/pipeline/spikes.py`.
TWO_SPIKES = (("first_spike", "first_spike.py"), ("second_spike", "second_spike.py"))


@pytest.fixture(scope="module")
def two_spikes(cdk: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """The templates of the app of `app.py`, from the example configuration, with the two spike
    jobs of `TWO_SPIKES` in place of the app's list, synthesized under cdk-nag."""
    import importlib.util

    from rhi import config
    from rhi.pipeline.spikes import SpikeJob
    from rhi.pipeline.stack import PipelineStack
    from rhi.stage import EndpointStage

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("app", repo / "app.py")
    assert spec is not None and spec.loader is not None
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    context = json.loads((repo / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    outdir = tmp_path_factory.mktemp("spikes")
    app = cdk.App(outdir=str(outdir), context=context, analytics_reporting=False)
    example = config.load({"RHI_CONFIG": "example"})
    stage = EndpointStage(app, "Rhi", config=example)
    app_module.add_nag(app)
    jobs = [SpikeJob(name, script) for name, script in TWO_SPIKES]
    PipelineStack(app, "RhiPipeline", config=example, stage=stage, spike_jobs=jobs)
    out = Path(app.synth().directory)
    return {
        path.relative_to(out).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(out.rglob("*.template.json"))
    }


def found_spikes(templates: Templates) -> list[tuple[str, str, str | None, str | None]]:
    return [(path, *job) for path, template in sorted(templates.items()) for job in spike_jobs(template)]


def test_the_spike_jobs_are_those_of_the_list(templates: dict[str, dict[str, Any]]) -> None:
    from rhi.pipeline.spikes import JOBS

    # The rules hold every job of the app's list: each is found, by its name, with its role.
    found = found_spikes(templates)
    assert sorted(name for _, _, name, _ in found if name is not None) == sorted(job.project_name for job in JOBS)
    assert all(role is not None for _, _, _, role in found)


def test_two_spike_jobs_keep_the_rules(two_spikes: dict[str, dict[str, Any]]) -> None:
    found = found_spikes(two_spikes)
    assert sorted(str(name) for _, _, name, _ in found) == [f"rhi-spike-{name}" for name, _ in TWO_SPIKES]
    ((path, role),) = {(path, role) for path, _, _, role in found}
    assert role is not None
    # The rules found what the role may read: the stage's pool-state table and its workspace's tag.
    assert pool_state_tables(two_spikes) and stage_tags(two_spikes) == {"Rhi"}
    assert all(spike_resources(two_spikes, two_spikes[path], project)["logs"] for _, project, _, _ in found)
    assert breaches(two_spikes) == []


def the_spike_role(templates: Templates) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """The template of the spike jobs and the policy document of their role."""
    ((path, role),) = {(path, role) for path, _, _, role in found_spikes(templates)}
    template = templates[path]
    (policy,) = [p for _, _, p, _ in resources({"": template}, "AWS::IAM::Policy") if {"Ref": role} in p["Roles"]]
    return template, policy["PolicyDocument"]


def a_statement(action: str, resource: str, condition: dict[str, Any] | None = None) -> Callable[[Templates], None]:
    """Allows the spike jobs' role one more action, on one resource of the deploying account."""

    def change(templates: Templates) -> None:
        _, document = the_spike_role(templates)
        statement = {"Effect": "Allow", "Action": action, "Resource": {"Fn::Sub": resource}}
        document["Statement"].append(statement | ({"Condition": condition} if condition is not None else {}))

    return change


def an_unconditioned_query(templates: Templates) -> None:
    _, document = the_spike_role(templates)
    for statement in document["Statement"]:
        statement.pop("Condition", None)


def a_managed_policy(templates: Templates) -> None:
    ((path, role),) = {(path, role) for path, _, _, role in found_spikes(templates)}
    assert role is not None
    arn = "arn:${AWS::Partition}:iam::aws:policy/ReadOnlyAccess"
    templates[path]["Resources"][role]["Properties"]["ManagedPolicyArns"] = [{"Fn::Sub": arn}]


def a_webhook(templates: Templates) -> None:
    path, project, _, _ = found_spikes(templates)[0]
    templates[path]["Resources"][project]["Properties"]["Triggers"] = {"Webhook": True}


def a_target(kind: str, *, elsewhere: bool = False) -> Callable[[Templates], None]:
    """A rule or a schedule, every night, whose target is the first spike job: by a reference in the
    job's own template, or, `elsewhere`, by the job's ARN in a template of the stage."""

    def change(templates: Templates) -> None:
        path, project, name, _ = found_spikes(templates)[0]
        arn: Any = {"Fn::GetAtt": [project, "Arn"]}
        if elsewhere:
            path = next(other for other in sorted(templates) if other != path)
            arn = {"Fn::Sub": f"arn:${{AWS::Partition}}:codebuild:${{AWS::Region}}:${{AWS::AccountId}}:project/{name}"}
        if kind == "AWS::Events::Rule":
            properties = {"ScheduleExpression": "rate(1 day)", "Targets": [{"Arn": arn, "Id": "Spike"}]}
        else:
            properties = {
                "ScheduleExpression": "rate(1 day)",
                "FlexibleTimeWindow": {"Mode": "OFF"},
                "Target": {"Arn": arn},
            }
        templates[path]["Resources"]["Nightly"] = {"Type": kind, "Properties": properties}

    return change


def a_universal_target(templates: Templates) -> None:
    """A schedule, every night, whose universal target calls CodeBuild's StartBuild with the first
    spike job's name in its JSON input."""
    _, _, name, _ = found_spikes(templates)[0]
    path = min(templates)
    target = {
        "Arn": "arn:aws:scheduler:::aws-sdk:codebuild:startBuild",
        "RoleArn": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/Nightly"},
        "Input": json.dumps({"projectName": name}),
    }
    properties = {"ScheduleExpression": "rate(1 day)", "FlexibleTimeWindow": {"Mode": "OFF"}, "Target": target}
    templates[path]["Resources"]["Nightly"] = {"Type": "AWS::Scheduler::Schedule", "Properties": properties}


def an_action(templates: Templates) -> None:
    """A CodeBuild action of the pipeline, after the stage's deployment, that runs the first job."""
    path, project, _, _ = found_spikes(templates)[0]
    ((_, _, pipeline, _),) = resources({path: templates[path]}, "AWS::CodePipeline::Pipeline")
    kind = {"Category": "Build", "Owner": "AWS", "Provider": "CodeBuild", "Version": "1"}
    action = {"Name": "Spike", "ActionTypeId": kind, "Configuration": {"ProjectName": {"Ref": project}}, "RunOrder": 99}
    pipeline["Stages"][-1]["Actions"].append(action)


HERE_ARN = "arn:${AWS::Partition}:{}:${AWS::Region}:${AWS::AccountId}:{}"


def here(service: str, resource: str) -> str:
    return HERE_ARN.replace("{}", service, 1).replace("{}", resource, 1)


# The stage's pool-state table, as the spike jobs' role names it.
POOL_STATE_TABLE = here("dynamodb", "table/Rhi-Pools-TablesPoolState68A872F7-*")


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(a_webhook, {SPIKE_STARTED, WEBHOOK}, id="a webhook"),
        pytest.param(a_target("AWS::Events::Rule"), {SPIKE_STARTED}, id="a rule"),
        pytest.param(a_target("AWS::Scheduler::Schedule"), {SPIKE_STARTED}, id="a schedule"),
        pytest.param(a_universal_target, {SPIKE_STARTED}, id="a schedule's universal target"),
        pytest.param(a_target("AWS::Events::Rule", elsewhere=True), {SPIKE_STARTED}, id="a rule of another stack"),
        pytest.param(an_action, {SPIKE_STARTED}, id="a pipeline's action"),
        pytest.param(
            a_statement("autoscaling:SetDesiredCapacity", "*"), {SPIKES_MORE}, id="setting a group's capacity"
        ),
        pytest.param(
            a_statement("ec2:StartInstances", here("ec2", "instance/*")),
            {SPIKES_MORE, STARTS_INSTANCES},
            id="starting an instance",
        ),
        pytest.param(
            a_statement("ec2:StopInstances", here("ec2", "instance/*")), {SPIKES_MORE}, id="stopping an instance"
        ),
        pytest.param(a_statement("iam:PassRole", "*"), {SPIKES_MORE}, id="an action of IAM"),
        pytest.param(
            a_statement("secretsmanager:GetSecretValue", here("secretsmanager", f"secret:{CANARY_SECRET}-??????")),
            {SPIKES_MORE},
            id="reading another secret",
        ),
        pytest.param(
            a_statement("secretsmanager:*", here("secretsmanager", f"secret:{SPIKE_SECRET}-??????")),
            {SPIKES_MORE},
            id="every action on the spikes' secret",
        ),
        pytest.param(
            a_statement("dynamodb:PutItem", POOL_STATE_TABLE, of_pool("coursework")),
            {SPIKES_MORE},
            id="writing the coursework pool's items",
        ),
        pytest.param(
            a_statement("dynamodb:PutItem", POOL_STATE_TABLE), {SPIKES_MORE}, id="a write that names no partition key"
        ),
        pytest.param(
            a_statement("dynamodb:PutItem", POOL_STATE_TABLE, of_pool(MOCK_POOL, "coursework")),
            {SPIKES_MORE},
            id="writing the mock pool's items and another's",
        ),
        pytest.param(
            a_statement("dynamodb:DeleteItem", POOL_STATE_TABLE, of_pool(MOCK_POOL)),
            {SPIKES_MORE},
            id="deleting the mock pool's items",
        ),
        pytest.param(
            a_statement("dynamodb:PutItem", here("dynamodb", "table/*"), of_pool(MOCK_POOL)),
            {SPIKES_MORE},
            id="writing the mock pool's items of every table",
        ),
        pytest.param(
            a_statement("dynamodb:Query", here("dynamodb", "table/*")), {SPIKES_MORE}, id="reading every table"
        ),
        pytest.param(an_unconditioned_query, {SPIKES_MORE}, id="every workspace"),
        pytest.param(
            a_statement("ssm:GetParametersByPath", here("ssm", "parameter/rhi/*")), {SPIKES_MORE}, id="other parameters"
        ),
    ],
)
def test_each_rule_on_the_spike_jobs_refuses_its_breach(
    two_spikes: dict[str, dict[str, Any]], change: Callable[[Templates], None], expected: set[str]
) -> None:
    changed = json.loads(json.dumps(two_spikes))
    change(changed)
    assert refused(changed) == expected


def test_a_spike_role_with_a_managed_policy_may_do_more(two_spikes: dict[str, dict[str, Any]]) -> None:
    changed = json.loads(json.dumps(two_spikes))
    a_managed_policy(changed)
    assert SPIKES_MORE in refused(changed)


def the_canary(templates: Templates) -> tuple[Mapping[str, Any], str, str | None, str, dict[str, Any]]:
    """The template of the canary, its project, its project's role, its schedule and the schedule's
    one target."""
    (template,) = [template for template in templates.values() if canaries(template)]
    ((project, role),) = canaries(template)
    ((schedule, (target,)),) = canary_schedules(template)
    return template, project, role, schedule, target


def test_the_canary_is_held_by_its_rules(templates: dict[str, dict[str, Any]]) -> None:
    # The rules find the app's canary, its role and its one schedule, which starts it alone.
    template, project, role, _, target = the_canary(templates)
    assert role is not None
    assert target["Arn"] == {"Fn::GetAtt": [project, "Arn"]}
    assert not starts_more_than(template, target["RoleArn"], project)
    # Its role sends measurement events, and starts no pipeline's execution.
    assert may(template, role, PUTS_EVENTS, BUS_ARN)
    assert not may(template, role, PIPELINE_STARTS, PIPELINE_ARN)
    assert canary_breaches(templates) == []


def a_second_target(templates: Templates) -> None:
    template, _, _, schedule, _ = the_canary(templates)
    other = {"Fn::Sub": "arn:${AWS::Partition}:codebuild:${AWS::Region}:${AWS::AccountId}:project/another-job"}
    template["Resources"][schedule]["Properties"]["Targets"].append({"Arn": other, "Id": "Another"})


def another_job(templates: Templates) -> None:
    """The schedule's one target, replaced by another job: the schedule still has its name."""
    _, _, _, _, target = the_canary(templates)
    target["Arn"] = {"Fn::Sub": "arn:${AWS::Partition}:codebuild:${AWS::Region}:${AWS::AccountId}:project/another-job"}


def the_pipeline_as_target(templates: Templates) -> None:
    template, _, _, _, target = the_canary(templates)
    ((_, pipeline, _, _),) = resources({"": template}, "AWS::CodePipeline::Pipeline")
    target["Arn"] = {
        "Fn::Sub": "arn:${AWS::Partition}:codepipeline:${AWS::Region}:${AWS::AccountId}:${" + pipeline + "}"
    }


def a_renamed_schedule(templates: Templates) -> None:
    template, _, _, schedule, _ = the_canary(templates)
    template["Resources"][schedule]["Properties"]["Name"] = "rhi-nightly"


def a_second_schedule(templates: Templates) -> None:
    template, _, _, schedule, _ = the_canary(templates)
    template["Resources"]["Nightly"] = json.loads(json.dumps(template["Resources"][schedule]))


def a_schedule_statement(action: str, resource: Any) -> Callable[[Templates], None]:
    """Allows the schedule's role one more action on one resource."""

    def change(templates: Templates) -> None:
        template, _, _, _, target = the_canary(templates)
        role = target["RoleArn"]["Fn::GetAtt"][0]
        (policy,) = [p for _, _, p, _ in resources({"": template}, "AWS::IAM::Policy") if {"Ref": role} in p["Roles"]]
        statement = {"Effect": "Allow", "Action": action, "Resource": resource}
        policy["PolicyDocument"]["Statement"].append(statement)

    return change


def a_job_statement(action: str, resource: Any) -> Callable[[Templates], None]:
    """Allows the canary's job role one more action on one resource."""

    def change(templates: Templates) -> None:
        template, _, role, _, _ = the_canary(templates)
        (policy,) = [p for _, _, p, _ in resources({"": template}, "AWS::IAM::Policy") if {"Ref": role} in p["Roles"]]
        policy["PolicyDocument"]["Statement"].append({"Effect": "Allow", "Action": action, "Resource": resource})

    return change


def a_managed_job_policy(templates: Templates) -> None:
    template, _, role, _, _ = the_canary(templates)
    arn = {"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AWSCodePipeline_FullAccess"}
    template["Resources"][role]["Properties"]["ManagedPolicyArns"] = [arn]


EVERY_PROJECT = {"Fn::Sub": here("codebuild", "project/*")}
EVERY_PIPELINE = {"Fn::Sub": here("codepipeline", "*")}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(a_second_target, {CANARY_STARTS_MORE}, id="a second target"),
        pytest.param(another_job, {CANARY_STARTS_MORE}, id="another job in place of the canary"),
        pytest.param(the_pipeline_as_target, {CANARY_STARTS_MORE, ANOTHER_TRIGGER}, id="the pipeline as target"),
        pytest.param(a_renamed_schedule, {CANARY_UNSCHEDULED}, id="a schedule under another name"),
        pytest.param(a_second_schedule, {CANARY_UNSCHEDULED}, id="two schedules"),
        pytest.param(
            a_schedule_statement("codebuild:StartBuild", EVERY_PROJECT), {CANARY_STARTS_MORE}, id="every project"
        ),
        pytest.param(
            a_schedule_statement("codepipeline:StartPipelineExecution", EVERY_PIPELINE),
            {CANARY_STARTS_MORE},
            id="a schedule's role that starts a pipeline",
        ),
        pytest.param(
            a_job_statement("codepipeline:StartPipelineExecution", EVERY_PIPELINE),
            {CANARY_STARTS_PIPELINES},
            id="a job's role that starts a pipeline",
        ),
        pytest.param(
            a_job_statement("codepipeline:*", "*"), {CANARY_STARTS_PIPELINES}, id="every action of CodePipeline"
        ),
    ],
)
def test_each_rule_on_the_canary_refuses_its_breach(
    templates: dict[str, dict[str, Any]], change: Callable[[Templates], None], expected: set[str]
) -> None:
    changed = json.loads(json.dumps(templates))
    change(changed)
    assert refused(changed) == expected


def test_a_canary_role_with_a_managed_policy_may_start_a_pipeline(templates: dict[str, dict[str, Any]]) -> None:
    changed = json.loads(json.dumps(templates))
    a_managed_job_policy(changed)
    assert CANARY_STARTS_PIPELINES in refused(changed)
