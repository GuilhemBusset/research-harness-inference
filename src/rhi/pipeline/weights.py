"""The pipeline's step that copies the pools' weights into the model bucket (system design,
sections 4.4 and 8.2).

Before the stage is deployed, the step runs the copy job: `scripts/pipeline/copy_weights.py`, on
the source, in a locked environment. So a release whose revision is new finds its weights when
the instance refresh starts the engine. The job writes a new revision beside the one in service,
and a configuration with no GPU pool copies nothing and passes. A copy that fails fails the step,
and the stage is not deployed.

**The action is named `ACTION`.** `tests/policy/test_pipeline.py` lists it, and holds every other
role of the app to writing nothing to the model bucket: the step's role is the bucket's one
writer.

**What the step is told.** `RHI_MODEL_BUCKET` is the model bucket's name, which
`rhi.pools.model_store.bucket_name` builds from the pipeline stack's own account and region: they
are the stage's, since the stage names no environment, and the pipeline stack can hold no
reference to a resource of the stage. `PYTHONPATH` holds the folders of pytest's import path, as
in the synth step: the job imports the configuration loader. The manifests are in the source, at
the commit the execution deploys, and the configuration is read from SSM when the job runs.

**The role** may list the model bucket, put and delete its objects, and read the parameters under
the configuration prefix. It may not read an object of the bucket: the job learns what a prefix
holds by listing it, and the bucket's policy refuses a read from outside the VPC's endpoint in any
case. Beside that it holds what CDK Pipelines gives every project of the pipeline: its own logs,
its own test reports and a read of its input in the artifact bucket. Nothing else:
`tests/synth/test_weights_job.py` holds it to that.

The step is a step, not a construct: CDK Pipelines creates its project and role inside the
pipeline construct of `rhi.pipeline.stack`, whose acknowledgement file covers them. This module
creates no resource.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import pipelines

from rhi.pipeline.synth import UV_VERSION, config_reader, import_path
from rhi.pools import model_store

# The name of the step's action.
ACTION = "CopyWeights"
SCRIPT = "scripts/pipeline/copy_weights.py"
# The variable that names the model bucket to the job.
BUCKET_VARIABLE = "RHI_MODEL_BUCKET"

INSTALL_COMMANDS = [f"python3 -m pip install uv=={UV_VERSION}"]
COMMANDS = ["uv sync --locked", f"uv run --locked python {SCRIPT}"]


def copy_weights_step(scope: cdk.Stack, source: pipelines.IFileSetProducer) -> pipelines.CodeBuildStep:
    """The step of the pipeline of `scope` that copies the pools' weights, on `source`."""
    bucket = model_store.bucket_arn(scope)
    return pipelines.CodeBuildStep(
        ACTION,
        input=source,
        install_commands=INSTALL_COMMANDS,
        commands=COMMANDS,
        env={
            BUCKET_VARIABLE: model_store.bucket_name(scope.account, scope.region),
            "PYTHONPATH": ":".join(import_path()),
        },
        role_policy_statements=[
            # What a revision's prefix holds, by a listing.
            iam.PolicyStatement(actions=["s3:ListBucket"], resources=[bucket]),
            # The files of each pool's manifest, under a prefix only known from the configuration,
            # and the removal of what a manifest no longer lists.
            iam.PolicyStatement(actions=["s3:PutObject", "s3:DeleteObject"], resources=[f"{bucket}/*"]),
            config_reader(scope),
        ],
    )
