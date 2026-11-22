"""The deployments dashboard: the pipeline's runs and their results, and the release each pool serves.

One of the six dashboards of the system design (section 7.3). It reads two sources (section 7.2):

- **The pipeline's executions**, from the log group the pipeline keeps its state changes in
  (`rhi.pipeline.events.LOG_GROUP_NAME`, `/rhi/pipeline/events`), through the CloudWatch data source. Each record holds the
  fields of `rhi.pipeline.events.FIELDS`. The panel keeps the executions' own state changes, not
  those of a stage or an action, and shows one line for each execution: the pipeline, the
  execution ID, when it started, when it last changed, and its last state, which is its result
  (`SUCCEEDED`, `FAILED`, `SUPERSEDED`, `STOPPED`, or `STARTED` while it runs). Newest first.
- **The release each instance serves**, from the lifecycle agent's identity report,
  `rhi_lifecycle_release_info` (section 4.1). The engine does not know its image digest, so the
  agent reports it: one series of value 1, whose labels `image_digest` and `model_revision` are the
  release the instance holds. The node's collector adds `pool` and `instance`.

A deployment does not reach an instance that is already running: a release starts an instance
refresh, and the pipeline's verification asserts that the served release equals the deployed one
(section 4.3). The release panels show the admin the same thing. There is one row for each pool of
the configuration, in its order, and its panel draws one band for each instance and the release it
serves, over the dashboard's range. When an instance's release changes, or a new instance replaces
it, one band ends and another starts.

The release series is not an engine metric: it is the agent's, and the agent reports it from the
moment its instance serves. Nothing in this dashboard is read from the per-engine table, so it has
no "asleep" series. A pool with no instance draws no band.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import cloudwatch as cloudwatch_builders
from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.builders import statetimeline as statetimeline_builders
from grafana_foundation_sdk.builders import table as table_builders
from grafana_foundation_sdk.models import cloudwatch, common

from rhi_dashboards import rules

if TYPE_CHECKING:
    from rhi.config import Config
    from rhi_common.pool_config import Pool

UID = "deployments"
TITLE = "Deployments and releases"

# The lifecycle agent's identity report, and the file that emits it.
RELEASE = "rhi_lifecycle_release_info"
RELEASE_SOURCE = "services/engine/lifecycle/rhi_lifecycle/serving.py"
# The labels of the release the agent gives it, and the one the node's collector gives every series
# of an instance (its ID).
IMAGE_DIGEST = "image_digest"
MODEL_REVISION = "model_revision"
INSTANCE = "instance"
# A release panel's series: one for each instance and the release it serves.
RELEASE_BY = (rules.POOL, INSTANCE, IMAGE_DIGEST, MODEL_REVISION)

METRICS: tuple[rules.Declared, ...] = (rules.EmittedBy(RELEASE, RELEASE_SOURCE),)

# The log group of the pipeline's state changes, by its fixed name (`rhi.pipeline.events`), as
# Grafana's role names it: the dashboards are built without the CDK, which that module imports.
PIPELINE_EVENTS = "/rhi/pipeline/events"
# The kind of event of an execution's own state change (`rhi.pipeline.reporter.DETAIL_TYPES`).
EXECUTION = "CodePipeline Pipeline Execution State Change"
EXECUTIONS_TITLE = "Pipeline executions and their result"
RELEASE_TITLE = "Release each instance serves: image digest and model revision"
# The data source's own region, which the image provisions from the deployment environment.
DEFAULT_REGION = "default"
# How many executions the table lists: the most recent ones.
EXECUTIONS_SHOWN = 100

WHOLE = 24


def _field(name: str) -> str:
    """A field of a record, as a Logs Insights query names it: in backticks, since some hold a hyphen."""
    return f"`{name}`"


def executions_query() -> str:
    """The Logs Insights query of the executions: one line each, with its last state as its result."""
    pipeline, execution, state = (_field(name) for name in ("pipeline", "execution-id", "state"))
    times = "min(@timestamp) as started, max(@timestamp) as updated"
    return "\n".join(
        (
            f'filter {_field("detail-type")} = "{EXECUTION}"',
            f"| stats latest({state}) as result, {times} by {pipeline}, {execution}",
            "| sort started desc",
            f"| limit {EXECUTIONS_SHOWN}",
        )
    )


def executions() -> table_builders.Panel:
    """The pipeline's executions with their result, from the log group of its state changes."""
    query = (
        cloudwatch_builders.LogsQuery()
        .query_mode(cloudwatch.QueryMode.LOGS)
        .query_language(cloudwatch.LogsQueryLanguage.CWLI)
        .region(DEFAULT_REGION)
        .log_group_names([PIPELINE_EVENTS])
        .expression(executions_query())
        .ref_id("A")
        .datasource(rules.CLOUDWATCH)
    )
    return table_builders.Panel().title(EXECUTIONS_TITLE).datasource(rules.CLOUDWATCH).with_target(query)


def release_expression(pool: Pool) -> str:
    """One series of value 1 for each instance of the pool and the release it serves."""
    return f"max by ({', '.join(RELEASE_BY)}) ({rules.selector(RELEASE, {rules.POOL: pool.name})})"


def release(pool: Pool) -> statetimeline_builders.Panel:
    """The image digest and model revision each instance of the pool serves, over the range."""
    legend = " ".join(f"{{{{{label}}}}}" for label in (INSTANCE, IMAGE_DIGEST, MODEL_REVISION))
    return (
        statetimeline_builders.Panel()
        .title(RELEASE_TITLE)
        .datasource(rules.PROMETHEUS)
        .with_target(rules.query(release_expression(pool), legend, "A"))
        .show_value(common.VisibilityMode.NEVER)
    )


def dashboard(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard: the executions first, then one row for each pool of the configuration, in its order."""
    built = dashboard_builders.Dashboard(TITLE).uid(UID)
    built.with_panel(executions().span(WHOLE))
    for pool in config.pools:
        built.with_row(dashboard_builders.Row(pool.name))
        built.with_panel(release(pool).span(WHOLE))
    return built
