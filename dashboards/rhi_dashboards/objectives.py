"""The five objectives of section 7.3, measured while a session is on, each with its target.

Defined once, here: the fleet dashboard shows them (`rhi_dashboards.fleet`) and the reading of the
dry-run class is judged against them (section 14).

| Objective | Target | Measured as | From |
| --- | --- | --- | --- |
| Gateway answers | At least 99.5%, the design's | The share of requests that are not a 5xx answer of the gateway's own fault | The gateway's requests and its own faults: an answer of 500 or above that names no cause of section 6.2 (`rhi_gateway.metrics`) |
| Time to first token, warm pool | The 95th percentile under `target_ttft_p95_seconds` | The share of the requests that were not held whose first token came under the target, first turns and later turns apart: at least 95% | The gateway's histogram of what students wait (`rhi_gateway.experience`) |
| Output speed per stream, warm pool | The median at `target_stream_median_tokens_per_second` or more, the slowest tenth at `target_stream_p10_tokens_per_second` or more | For each pool whose engine is not the mock, the share of the streams that were not held at each floor or more: above 50%, above 90% | The gateway's histogram of each stream's speed |
| Cached share of input | Above `cached_share_flag_percent` | For each pool: its engine's cached input tokens over its input tokens | The engine, by the per-engine table |
| Cold start from the warm pool | The 90th percentile under 5 minutes, the design's | The share of the promotions from the warm pool completed in the session that took under 5 minutes: at least 90% | The lifecycle agent's histogram of the seconds its stages took in each launch completed (`rhi_lifecycle.stages`) |

The provisional targets are the configuration's (`rhi.config.Config`), until spike S5 has measured
the pool; 99.5% and 5 minutes are fixed by the design.

**Only what happened while a session was on.** Every query is built with `rules.over_sessions`:
each step's contribution (what a counter rose by in that step, 5 seconds, which holds at most one
of its samples) is kept only where the session gauge read 1 for the whole step, at every step of
the dashboard's range, and summed over them. A share is taken of the sums, never of each step's:
the share of the session's requests, of the session's streams. So the same query is right for any
session, and over a range with no session it reads no data.

**A percentile is judged by a share.** The q-th percentile is the smallest value with at least q
of the observations at or under it, so it is under a target exactly when at least q of the
observations are under it, and at a floor or more exactly when more than 1 - q are at the floor or
more. That share is what each measure shows and is judged on, with the observations behind it, and
`target` and `percentile` say which percentile it stands for. The gateway's buckets count the
values strictly under their boundary (`rhi_gateway.experience`), and so do the lifecycle agent's
(`rhi_lifecycle.stages`), and each target is a boundary of its histogram, so the share is read
from that one bucket, exactly: the gateway takes the configured floors among its boundaries
(`rhi_gateway.settings`), whatever the owner sets them to, and 5 minutes is a boundary of the
agent's. A bucket is selected by its `le` label as the workspace holds it: the collectors (release
0.165.0) write a boundary in the fewest digits that give it back, with no exponent and no ".0"
(`5`, `12.5`), not as the client library serves it (`5.0`).

**Gateway answers.** Where there are requests and no fault was counted, zero faults are read, so a
session without a fault reads 100%.

**Cold start.** The lifecycle agent observes each launch whose hook Auto Scaling took with
`CONTINUE`, once, with the seconds its stages took in all: a launch abandoned, or whose completion
was refused, is not observed, and two launches are two observations whatever their seconds. Its
count is read like a counter, so each launch is counted at the step it was completed in, and one
completed before the session is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
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
BOUNDARY = "le"
# The seconds of each launch completed, and the transition of a start from the warm pool.
LAUNCH_SECONDS = "rhi_lifecycle_launch_seconds"
TRANSITION, PROMOTION = "transition", "promotion"
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
    rules.EmittedBy(LAUNCH_SECONDS, LIFECYCLE),
    rules.FromEngineTable(Quantity.INPUT_TOKENS),
    rules.FromEngineTable(Quantity.CACHED_INPUT_TOKENS),
)


class Comparison(StrEnum):
    """How a measured value meets its threshold."""

    AT_LEAST = "at least"
    ABOVE = "above"

    def met(self, value: float, threshold: float) -> bool:
        """Whether `value` meets `threshold`."""
        return value >= threshold if self is Comparison.AT_LEAST else value > threshold


@dataclass(frozen=True)
class Measure:
    """One query of an objective, and what its value is judged against.

    `target` is the objective's target, as section 7.3 states it (99.5%, 5 seconds, 15 tokens per
    second, 85%, 300 seconds), and `percentile`, from 0 to 1, the percentile it bounds, if any. The
    value of `expression` meets the objective when it is `comparison` `threshold`: the target itself,
    or, for a percentile, the share of the observations it needs. `unit` is Grafana's unit of the
    value, and `count` the query of the observations behind a share, over the same steps.
    """

    legend: str
    expression: str
    target: float
    comparison: Comparison
    threshold: float
    unit: str
    goal: str
    percentile: float | None = None
    count: str | None = None

    def met(self, value: float) -> bool:
        return self.comparison.met(value, self.threshold)


@dataclass(frozen=True)
class Objective:
    """One objective of section 7.3: its name and its measures, one query each."""

    name: str
    measures: tuple[Measure, ...]

    @property
    def queries(self) -> tuple[str, ...]:
        return tuple(measure.expression for measure in self.measures)


def boundary(value: float) -> str:
    """A bucket's boundary as the `le` label the workspace holds: as the collectors' remote write
    formats it, Go's `strconv.FormatFloat(value, 'f', -1, 64)`, the fewest digits that give the
    value back, with no exponent and no trailing zero."""
    text = format(Decimal(repr(float(value))), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _under(histogram: str, matchers: dict[str, str], floor: float) -> tuple[str, str]:
    """The session's observations of the histogram under the floor, one of its boundaries, and all of them."""
    under = rules.over_sessions(rules.step_increase(f"{histogram}_bucket", {**matchers, BOUNDARY: boundary(floor)}))
    return under, rules.over_sessions(rules.step_increase(f"{histogram}_count", matchers))


def gateway_answers() -> Objective:
    """The share of the session's requests that were not a 5xx of the gateway's own fault."""
    requests = rules.over_sessions(rules.step_increase(REQUESTS))
    faults = rules.over_sessions(rules.step_increase(OWN_FAULTS))
    share = f"(({requests}) - (({faults}) or on () (0 * ({requests})))) / ({requests})"
    goal = f"at least {GATEWAY_TARGET:.1%} of requests not a 5xx of the gateway's own fault"
    measure = Measure(
        "not the gateway's fault", share, GATEWAY_TARGET, Comparison.AT_LEAST, GATEWAY_TARGET, "percentunit", goal
    )
    return Objective("Gateway answers", (measure,))


def first_token(config: Config) -> Objective:
    """The 95th percentile of the time to first token of warm requests, first and later turns apart."""
    target, q = config.target_ttft_p95_seconds, 0.95
    measures = []
    for turn in (FIRST_TURN, LATER_TURN):
        under, count = _under(FIRST_TOKEN, {START: WARM, TURN: turn}, target)
        goal = f"p95 under {target:g} s: at least {q:.0%} of warm {turn} turns with a first token under {target:g} s"
        measures.append(
            Measure(
                f"{turn} turns under {target:g} s",
                f"({under}) / ({count})",
                target,
                Comparison.AT_LEAST,
                q,
                "percentunit",
                goal,
                q,
                count,
            )
        )
    return Objective("Time to first token, warm pool", tuple(measures))


def stream_speed(config: Config) -> Objective:
    """The median and the slowest tenth of warm streams' speed, for each pool whose engine is not the mock."""
    measures = []
    for pool in speed_pools(config):
        for q, name, floor in (
            (0.5, "median", config.target_stream_median_tokens_per_second),
            (0.1, "slowest tenth", config.target_stream_p10_tokens_per_second),
        ):
            under, count = _under(SPEED, {rules.POOL: pool.name, START: WARM}, floor)
            needed = round(1 - q, 12)
            goal = f"{name} at {floor:g} tokens/s or more: above {needed:.0%} of warm streams at {floor:g} or more"
            measures.append(
                Measure(
                    f"{pool.name} {name}, at {floor:g} tokens/s or more",
                    f"(({count}) - ({under})) / ({count})",
                    floor,
                    Comparison.ABOVE,
                    needed,
                    "percentunit",
                    goal,
                    q,
                    count,
                )
            )
    return Objective("Output speed per stream, warm pool", tuple(measures))


def speed_pools(config: Config) -> tuple[Pool, ...]:
    """The pools the speed objective is measured for: those whose engine is not the mock."""
    return tuple(pool for pool in config.pools if pool.engine != MOCK_ENGINE)


def cached_share(config: Config) -> Objective:
    """For each pool, its engine's cached input tokens over its input tokens, while a session is on."""
    target = config.cached_share_flag_percent / 100
    measures = []
    for pool in config.pools:
        cached, total = (
            rules.over_sessions(rules.awake(pool.name, rules.engine_step_increase(pool, quantity)))
            for quantity in (Quantity.CACHED_INPUT_TOKENS, Quantity.INPUT_TOKENS)
        )
        goal = f"above {config.cached_share_flag_percent:g}% of input tokens cached"
        measures.append(Measure(pool.name, f"({cached}) / ({total})", target, Comparison.ABOVE, target, "percentunit", goal))
    return Objective("Cached share of input", tuple(measures))


def cold_start() -> Objective:
    """The 90th percentile of the seconds a start from the warm pool took, over the session's launches."""
    q, target = 0.9, COLD_START_TARGET_SECONDS
    under, count = _under(LAUNCH_SECONDS, {TRANSITION: PROMOTION}, target)
    goal = f"p90 under {target / 60:g} minutes: at least {q:.0%} of the session's promotions under {target:g} s"
    measure = Measure(
        f"under {target / 60:g} minutes", f"({under}) / ({count})", target, Comparison.AT_LEAST, q, "percentunit", goal, q, count
    )
    return Objective("Cold start from the warm pool", (measure,))


def objectives(config: Config) -> tuple[Objective, ...]:
    """The five objectives, in the order of section 7.3, with the configuration's targets."""
    return (gateway_answers(), first_token(config), stream_speed(config), cached_share(config), cold_start())


def threshold_step(measure: Measure) -> float:
    """The least value a panel's threshold step may start at for the measure to be met: the
    threshold, or, for a value that must be above it, the next float."""
    if measure.comparison is Comparison.ABOVE:
        return math.nextafter(measure.threshold, math.inf)
    return measure.threshold
