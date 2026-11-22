"""The five objectives of section 7.3, measured while a session is on, each with its target.

Defined once, here: the fleet dashboard shows them (`rhi_dashboards.fleet`) and the reading of the
dry-run class is judged against them (section 14).

| Objective | Measured as | Target | From |
| --- | --- | --- | --- |
| Gateway answers | The share of requests that are not a 5xx answer of the gateway's own fault | At least 99.5%, the design's | The gateway's requests and its own faults: an answer of 500 or above that names no cause of section 6.2 (`rhi_gateway.metrics`) |
| Time to first token, warm pool | The 95th percentile, first turns and later turns apart, of the requests that were not held | Under `target_ttft_p95_seconds` | The gateway's histogram of what students wait (`rhi_gateway.experience`) |
| Output speed per stream, warm pool | For each pool whose engine is not the mock: the median stream, and the slowest tenth, of the requests that were not held | `target_stream_median_tokens_per_second` and `target_stream_p10_tokens_per_second` or more | The gateway's histogram of each stream's speed |
| Cached share of input | For each pool: its engine's cached input tokens over its input tokens | Above `cached_share_flag_percent` | The engine, by the per-engine table |
| Cold start from the warm pool | The 90th percentile, over the launches promoted from the warm pool, of the seconds their stages took | Under 5 minutes, the design's | The lifecycle agent's seconds per stage (`rhi_lifecycle.stages`) |

The provisional targets are the configuration's (`rhi.config.Config`), until spike S5 has measured
the pool; 99.5% and 5 minutes are fixed by the design.

**Only the samples taken while a session is on.** Every query is built with
`rules.over_sessions`: each minute's contribution (a counter's increase over that minute, a
histogram's buckets' increase, a launch's seconds) is kept only where the scaler's session gauge is
1, at every minute of the dashboard's range, and then summed over those minutes (or, for a launch,
its largest value taken). A ratio or a percentile is taken of the sums, never of each minute's: the
share of the session's requests, the percentile of the session's streams. So the same query is right
for any session, and over a range with no session it reads no data.

**Gateway answers.** The faults are summed over the session's minutes, and where there are requests
and no fault was counted (the gateway creates the counter at its first fault), zero faults are read,
so a session without a fault reads 100%.

**Cold start.** A launch is an instance's series: the lifecycle agent's gauge holds what each stage
of the instance's last launch took. Its stages sum to the start, and its largest sum while a session
was on is the launch's. An instance promoted twice in one range counts as its slower start.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from rhi_common.engine_metrics import Quantity
from rhi_dashboards import rules

if TYPE_CHECKING:
    from rhi.config import Config
    from rhi_common.pool_config import Pool

GATEWAY_METRICS = "services/gateway/rhi_gateway/metrics.py"
GATEWAY_EXPERIENCE = "services/gateway/rhi_gateway/experience.py"
LIFECYCLE = "services/engine/lifecycle/rhi_lifecycle/stages.py"

# The gateway's counts of requests, and of its own faults (section 6.2: a 5xx that names no cause).
REQUESTS = "rhi_gateway_student_requests_total"
OWN_FAULTS = "rhi_gateway_own_faults_total"
# The gateway's histograms of what students experience, and the labels that select a warm request.
FIRST_TOKEN = "rhi_gateway_time_to_first_token_seconds"
SPEED = "rhi_gateway_stream_output_tokens_per_second"
START, WARM = "start", "warm"
TURN, FIRST_TURN, LATER_TURN = "turn", "first", "later"
# What each stage of an instance's last launch took, and the transition of a start from the warm pool.
STAGE_SECONDS = "rhi_lifecycle_stage_seconds"
PROMOTION = "promotion"
INSTANCE = "instance"
# The engine of the mock pool, which the speed objective leaves out: it runs no GPU.
MOCK_ENGINE = "mock"

# The design's two fixed targets (section 7.3).
GATEWAY_TARGET = 0.995
COLD_START_TARGET_SECONDS = 300.0

METRICS: tuple[rules.Declared, ...] = (
    rules.EmittedBy(REQUESTS.removesuffix("_total"), GATEWAY_METRICS),
    rules.EmittedBy(OWN_FAULTS.removesuffix("_total"), GATEWAY_METRICS),
    rules.EmittedBy(FIRST_TOKEN, GATEWAY_EXPERIENCE),
    rules.EmittedBy(SPEED, GATEWAY_EXPERIENCE),
    rules.EmittedBy(STAGE_SECONDS, LIFECYCLE),
    rules.FromEngineTable(Quantity.INPUT_TOKENS),
    rules.FromEngineTable(Quantity.CACHED_INPUT_TOKENS),
)


class Comparison(StrEnum):
    """How a measured value meets its target."""

    AT_LEAST = "at least"
    ABOVE = "above"
    UNDER = "under"

    def met(self, value: float, target: float) -> bool:
        """Whether `value` meets `target`."""
        if self is Comparison.AT_LEAST:
            return value >= target
        if self is Comparison.ABOVE:
            return value > target
        return value < target


@dataclass(frozen=True)
class Measure:
    """One query of an objective and the target it is compared with.

    `unit` is Grafana's unit of the value. `count` is the query of the observations behind a
    percentile, over the same minutes; None for a measure that is no histogram's percentile.
    """

    legend: str
    expression: str
    target: float
    comparison: Comparison
    unit: str
    count: str | None = None

    def met(self, value: float) -> bool:
        return self.comparison.met(value, self.target)


@dataclass(frozen=True)
class Objective:
    """One objective of section 7.3: its name and its measures, one query each."""

    name: str
    measures: tuple[Measure, ...]

    @property
    def queries(self) -> tuple[str, ...]:
        return tuple(measure.expression for measure in self.measures)


def _percentile(q: float, histogram: str, matchers: dict[str, str]) -> tuple[str, str]:
    """The quantile `q` of the histogram's observations while a session is on, and their count."""
    buckets = rules.over_sessions(rules.minute_increase(f"{histogram}_bucket", matchers, by=("le",)))
    count = rules.over_sessions(rules.minute_increase(f"{histogram}_count", matchers))
    return f"histogram_quantile({q:g}, {buckets})", count


def gateway_answers() -> Objective:
    """The share of the session's requests that were not a 5xx of the gateway's own fault."""
    requests = rules.over_sessions(rules.minute_increase(REQUESTS))
    faults = rules.over_sessions(rules.minute_increase(OWN_FAULTS))
    share = f"1 - (({faults}) or on () (0 * {requests})) / ({requests})"
    return Objective(
        "Gateway answers", (Measure("not the gateway's fault", share, GATEWAY_TARGET, Comparison.AT_LEAST, "percentunit"),)
    )


def first_token(config: Config) -> Objective:
    """The 95th percentile of the time to first token of warm requests, first and later turns apart."""
    measures = []
    for turn in (FIRST_TURN, LATER_TURN):
        expression, count = _percentile(0.95, FIRST_TOKEN, {START: WARM, TURN: turn})
        legend = f"p95, {turn} turns"
        measures.append(Measure(legend, expression, config.target_ttft_p95_seconds, Comparison.UNDER, "s", count))
    return Objective("Time to first token, warm pool", tuple(measures))


def stream_speed(config: Config) -> Objective:
    """The median and the slowest tenth of warm streams' speed, for each pool whose engine is not the mock."""
    measures = []
    for pool in speed_pools(config):
        matchers = {rules.POOL: pool.name, START: WARM}
        for q, legend, target in (
            (0.5, "median", config.target_stream_median_tokens_per_second),
            (0.1, "slowest tenth", config.target_stream_p10_tokens_per_second),
        ):
            expression, count = _percentile(q, SPEED, matchers)
            measures.append(Measure(f"{pool.name} {legend}", expression, target, Comparison.AT_LEAST, "short", count))
    return Objective("Output speed per stream, warm pool", tuple(measures))


def speed_pools(config: Config) -> tuple[Pool, ...]:
    """The pools the speed objective is measured for: those whose engine is not the mock."""
    return tuple(pool for pool in config.pools if pool.engine != MOCK_ENGINE)


def cached_share(config: Config) -> Objective:
    """For each pool, its engine's cached input tokens over its input tokens, while a session is on."""
    measures = []
    for pool in config.pools:
        cached, total = (
            rules.over_sessions(rules.awake(pool.name, rules.engine_increase(pool, quantity, window=rules.SESSION_STEP)))
            for quantity in (Quantity.CACHED_INPUT_TOKENS, Quantity.INPUT_TOKENS)
        )
        target = config.cached_share_flag_percent / 100
        measures.append(Measure(pool.name, f"{cached} / {total}", target, Comparison.ABOVE, "percentunit"))
    return Objective("Cached share of input", tuple(measures))


def cold_start() -> Objective:
    """The 90th percentile of the seconds a start from the warm pool took, over the session's launches."""
    launches = rules.selector(STAGE_SECONDS, {"transition": PROMOTION})
    per_launch = f"sum by ({rules.POOL}, {INSTANCE}) ({launches})"
    expression = f"quantile(0.9, {rules.over_sessions(per_launch, 'max_over_time')})"
    measure = Measure("p90", expression, COLD_START_TARGET_SECONDS, Comparison.UNDER, "s")
    return Objective("Cold start from the warm pool", (measure,))


def objectives(config: Config) -> tuple[Objective, ...]:
    """The five objectives, in the order of section 7.3, with the configuration's targets."""
    return (gateway_answers(), first_token(config), stream_speed(config), cached_share(config), cold_start())
