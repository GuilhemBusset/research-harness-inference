"""The students dashboard: what each student asked of the endpoint, by roster alias.

One of the six dashboards of the system design (section 7.3). Per student, it shows what the
gateway counts (section 7.2): requests, input tokens and their cached part, output tokens, the
tokens of the token counts asked, and errors by cause. There is no token budget (section 11.3):
these are the counts the engine reported, which the gateway reads from each answer as it passes,
and a token count (`POST /v1/messages/count_tokens`) is counted apart from a generation
(`rhi_gateway.metrics`).

| Panel | Series | From |
| --- | --- | --- |
| Requests | One for each alias and pool | `rhi_gateway_student_requests_total` |
| Input tokens | One for each alias and pool | `rhi_gateway_student_input_tokens_total` |
| Cached input tokens | One for each alias and pool | `rhi_gateway_student_cached_input_tokens_total` |
| Output tokens | One for each alias and pool | `rhi_gateway_student_output_tokens_total` |
| Tokens of token counts | One for each alias and pool | `rhi_gateway_student_counted_tokens_total` |
| Errors by cause | One for each alias, pool and cause | `rhi_gateway_student_errors_total` |

**A student is an alias** (section 6.6). The gateway's per-student series exist in Prometheus
only, labelled with the roster alias, and no series carries another name of a student. Every query
here groups by `alias` and by nothing else that names a student. A request the gateway answered
before it accepted a key (a refused key, the hello, a path outside the table) has an empty alias:
it is no student's, and every query keeps only the series whose alias is not empty.

**Summed over the gateway's tasks.** The gateway runs two tasks, and the collector beside each
labels every series with its task (`instance`), so the counters of two tasks never merge
(`services/gateway/collector/config.yaml`). A task's counters start from zero when it starts, so
each query takes the rate of each series first, then sums by pool and alias, which drops the task
(section 7.2, first rule). The sum is the rate, a second, over `rules.WINDOW`.

**By pool too.** A student may use more than one pool, and a pool is one entry of the
configuration (section 4.2): each series is one alias on one pool, never a pool's numbers mixed
into another's. The panels need no row per pool, and nothing here is read from an engine, so there
is no "asleep" series: a student who sent nothing draws nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.builders import timeseries as timeseries_builders

from rhi_dashboards import rules

if TYPE_CHECKING:
    from rhi.config import Config

UID = "students"
TITLE = "Students"

# The file that emits the gateway's per-student series.
GATEWAY_METRICS = "services/gateway/rhi_gateway/metrics.py"
# The families, as `rhi_gateway.metrics` names them; a counter's samples end in `_total`.
REQUESTS = "rhi_gateway_student_requests"
INPUT_TOKENS = "rhi_gateway_student_input_tokens"
CACHED_INPUT_TOKENS = "rhi_gateway_student_cached_input_tokens"
OUTPUT_TOKENS = "rhi_gateway_student_output_tokens"
COUNTED_TOKENS = "rhi_gateway_student_counted_tokens"
ERRORS = "rhi_gateway_student_errors"

# The label of the roster alias, the one name of a student a series carries, and of an error's cause.
ALIAS = "alias"
CAUSE = "cause"
# An alias that is not empty: the series of a student.
A_STUDENT = ".+"

METRICS: tuple[rules.Declared, ...] = tuple(
    rules.EmittedBy(family, GATEWAY_METRICS)
    for family in (REQUESTS, INPUT_TOKENS, CACHED_INPUT_TOKENS, OUTPUT_TOKENS, COUNTED_TOKENS, ERRORS)
)

# Each panel, in the dashboard's order: its title, its family, the labels it groups by beside the
# pool, and its unit, by Grafana's unit identifiers.
PANELS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("Requests, a second", REQUESTS, (ALIAS,), "reqps"),
    ("Input tokens, a second, cached or not", INPUT_TOKENS, (ALIAS,), "short"),
    ("Cached input tokens, a second", CACHED_INPUT_TOKENS, (ALIAS,), "short"),
    ("Output tokens, a second", OUTPUT_TOKENS, (ALIAS,), "short"),
    ("Tokens of token counts, a second", COUNTED_TOKENS, (ALIAS,), "short"),
    ("Errors by cause, a second", ERRORS, (ALIAS, CAUSE), "reqps"),
)

HALF = 12


def per_student(family: str, by: tuple[str, ...]) -> str:
    """The rate of the family's counter for each series, every task's apart, then summed by pool and
    `by`: the students' series only, those whose alias is not empty."""
    series = rules.selector(family + rules.COUNTER_SAMPLE, matching={ALIAS: A_STUDENT})
    return f"sum by ({', '.join((rules.POOL, *by))}) (rate({series}[{rules.WINDOW}]))"


def panel(title: str, family: str, by: tuple[str, ...], unit: str) -> timeseries_builders.Panel:
    """One quantity, one series for each alias and pool (and cause, for the errors)."""
    legend = " ".join(f"{{{{{label}}}}}" for label in (*by, rules.POOL))
    return (
        timeseries_builders.Panel()
        .title(title)
        .datasource(rules.PROMETHEUS)
        .with_target(rules.query(per_student(family, by), legend, "A"))
        .unit(unit)
        .min(0)
    )


def dashboard(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard: the six quantities, two panels a line. The build gives every module the
    configuration; no panel here is one pool's, so it reads none of it."""
    built = dashboard_builders.Dashboard(TITLE).uid(UID)
    for one in PANELS:
        built.with_panel(panel(*one).span(HALF))
    return built
