"""The four objectives of section 7.3, each pool's measured while its session is active, each with its target.

Defined once, here: the fleet dashboard shows them (`rhi_dashboards.fleet`) and the reading of the
dry-run class is judged against them (section 14).

| Objective | Target | Measured as | From |
| --- | --- | --- | --- |
| Gateway answers | At least 99.5%, the design's | The share of requests that are not a 5xx answer of the gateway's own fault | The gateway's requests and its own faults: an answer of 500 or above that names no cause of section 6.2 (`rhi_gateway.metrics`) |
| Time to first token | The 95th percentile under `target_ttft_p95_seconds` | For each pool, the share of its streamed requests whose first token came under the target, first turns and later turns apart: at least 95% | The gateway's histogram of what students wait (`rhi_gateway.experience`) |
| Output speed per stream | On the coursework pool, the median at `target_stream_median_tokens_per_second` or more, the slowest tenth at `target_stream_p10_tokens_per_second` or more | The share of the pool's streams at each floor or more: above 50%, above 90% | The gateway's histogram of each stream's speed |
| Cached share of input | Above `cached_share_flag_percent` | For each pool: its engine's cached input tokens over its input tokens | The engine, by the per-engine table |

The provisional targets are the configuration's (`rhi.config.Config`), until spike S5 has measured
the pool; 99.5% is fixed by the design. There is no objective on the time a launch takes: nobody
waits for a launch behind a request, since a session's instances are asked for ahead of it.

**Only what happened while the session was active.** Sessions are per pool, and so are three of the
objectives: a measure of a pool counts that pool's series only, and only the samples taken while
that pool's session was active (`Measure.pool`). The gateway's answers are about no pool: they are
counted while any pool's session was active. Every query is built with `rules.over_sessions`: each
sample of a counter is counted by what it adds (`rules.session_increment`), and only where it was
taken inside the dashboard's range while the latest reading of the session's gauge was 1, by the
timestamps of both. A share is taken of the sums, never of each step's: the share of the session's
requests, of the session's streams. So the same query is right for any session, and over a range
with no session it reads no data.

**The coursework pool** is the pool of that name (`COURSEWORK`; section 4.2), and the speed
objective is measured on it alone: its floors are that pool's. It is not the pool the gateway maps
`claude-*` names to (`default_pool`), which the owner may point at another pool. A configuration
with no pool of that name has no measure of speed.

**Every stream the gateway recorded in the session counts**, whether its request waited for its
pool or not: the gateway labels each with `start`, `held` or `warm`, and the objectives select
neither, so both are summed. Only the turn is told apart, for the time to first token.

**A percentile is judged by a share.** The q-th percentile is the smallest value with at least q
of the observations at or under it, so it is under a target exactly when at least q of the
observations are under it, and at a floor or more exactly when more than 1 - q are at the floor or
more. That share is what each measure shows and is judged on, with the observations behind it, and
`target` and `percentile` say which percentile it stands for. The gateway's buckets count the
values strictly under their boundary (`rhi_gateway.experience`). Each target is a boundary of its
histogram: the gateway takes the configured floors among its boundaries (`rhi_gateway.settings`).
A bucket is selected by its `le` label as the workspace holds it: the collectors (release
0.165.0) write a boundary in the fewest digits that give it back, with no exponent and no ".0"
(`5`, `12.5`), not as the client library serves it (`5.0`).

**A revised floor gives a verdict only where the buckets settle it.** A gateway task started
before the owner revised a floor to a value that is not one of the gateway's own boundaries
(`rhi_common.boundaries`) has no bucket at it. Where the floor is one of them, every task counts it
and the share is read exactly. Otherwise the share is bounded both ways, for each task's series, by
its buckets nearest the floor among the floor and the gateway's boundaries: the largest at or under
it, whose observations are all under the floor (`max` of the cumulative buckets; none, where a
series has none), and the smallest at or over it, whose bucket holds every observation under the
floor (`min`, `+Inf` among them). The measure shows the bound that settles the objective: the share
proven to meet it where that meets the threshold, the most the share can be where even that misses
it, and `INCONCLUSIVE` where the bounds straddle the threshold, which the panel reads
"inconclusive". A task that counts the floor makes both bounds the same; an older one never decides
a verdict its buckets do not prove.

**Gateway answers.** Where there are requests and no fault was counted, zero faults are read, so a
session without a fault reads 100%.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from rhi_common.boundaries import SPEED_BUCKETS, WAIT_BUCKETS
from rhi_common.engine_metrics import Quantity
from rhi_dashboards import rules

if TYPE_CHECKING:
    from rhi.config import Config
    from rhi_common.pool_config import Pool

GATEWAY_METRICS = "services/gateway/rhi_gateway/metrics.py"
GATEWAY_EXPERIENCE = "services/gateway/rhi_gateway/experience.py"

# The gateway's counts of requests, and of its own faults (section 6.2: a 5xx that names no cause).
REQUESTS = "rhi_gateway_student_requests_total"
OWN_FAULTS = "rhi_gateway_own_faults_total"
# The gateway's histograms of what students experience, and the label that tells a first turn from
# a later one.
FIRST_TOKEN = "rhi_gateway_time_to_first_token_seconds"
SPEED = "rhi_gateway_stream_output_tokens_per_second"
TURN, FIRST_TURN, LATER_TURN = "turn", "first", "later"
BOUNDARY, INFINITY = "le", "+Inf"

# The pool the speed objective is measured on (sections 4.2 and 7.3), by its name.
COURSEWORK = "coursework"

# The design's fixed target (section 7.3).
GATEWAY_TARGET = 0.995

METRICS: tuple[rules.Declared, ...] = (
    rules.EmittedBy(REQUESTS.removesuffix("_total"), GATEWAY_METRICS),
    rules.EmittedBy(OWN_FAULTS.removesuffix("_total"), GATEWAY_METRICS),
    rules.EmittedBy(FIRST_TOKEN, GATEWAY_EXPERIENCE),
    rules.EmittedBy(SPEED, GATEWAY_EXPERIENCE),
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


# What a measure shows where the buckets do not settle its objective (`Measure.expression`).
INCONCLUSIVE = -1.0
# The operators of a share that meets its threshold, and of one that misses it.
_OPERATORS = {Comparison.AT_LEAST: (">=", "<"), Comparison.ABOVE: (">", "<=")}


@dataclass(frozen=True)
class Measure:
    """One query of an objective, and what its value is judged against.

    `target` is the objective's target, as section 7.3 states it (99.5%, 5 seconds, 15 tokens per
    second, 85%), and `percentile`, from 0 to 1, the percentile it bounds, if any. The
    value meets the objective when it is `comparison` `threshold`: the target itself, or, for a
    percentile, the share of the observations it needs. `share` is the query of that value, or of
    the least it can be when `ceiling`, the most it can be, is given too: a share an older gateway
    task's buckets only bound. `unit` is Grafana's unit of the value, and `count` the query of the
    observations behind a share, over the same steps. `pool` is the pool the measure is of, by its
    name: it counts that pool's series, while that pool's session is active. It is None
    (`rules.ANY_POOL`) for the gateway's answers, counted while any pool's session is active.
    """

    legend: str
    share: str
    target: float
    comparison: Comparison
    threshold: float
    unit: str
    goal: str
    percentile: float | None = None
    count: str | None = None
    ceiling: str | None = None
    pool: rules.Session = rules.ANY_POOL

    @property
    def expression(self) -> str:
        """The query the panel shows: the share, where it is read exactly; otherwise the bound that
        settles the objective, the least where it meets the threshold, the most where it misses
        it, and `INCONCLUSIVE` where neither does and there were observations."""
        if self.ceiling is None:
            return self.share
        meets, misses = _OPERATORS[self.comparison]
        threshold = repr(self.threshold)
        unsettled = f"(0 * (({self.count}) > 0)) - {-INCONCLUSIVE:g}"
        return f"(({self.share}) {meets} {threshold}) or (({self.ceiling}) {misses} {threshold}) or ({unsettled})"

    def met(self, value: float) -> bool | None:
        """Whether the value the measure showed meets its objective; `None` where it is inconclusive."""
        if self.ceiling is not None and value == INCONCLUSIVE:
            return None
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


def _count(histogram: str, matchers: dict[str, str]) -> str:
    """The observations of the histogram of a pool in the pool's session: `matchers` names the pool."""
    return rules.over_sessions(rules.step_increase(f"{histogram}_count", matchers, session=matchers[rules.POOL]))


def _nearest_bucket(histogram: str, matchers: dict[str, str], bounds: list[str], reduce: str) -> str:
    """The observations of a pool in its session, in each series' bucket nearest the floor among
    `bounds`: the one `reduce` (`max` or `min`) picks of the cumulative buckets the series has."""
    pattern = "|".join(re.escape(bound) for bound in bounds)
    series = rules.selector(f"{histogram}_bucket", matchers, matching={BOUNDARY: pattern})
    session = matchers[rules.POOL]
    return rules.over_sessions(
        lambda previous: (
            f"sum by () ({reduce} without ({BOUNDARY}) ({rules.session_increment(series, previous, session)}))"
        )
    )


def under_floor(histogram: str, matchers: dict[str, str], floor: float, buckets: tuple[float, ...]) -> str:
    """The session's observations proven under the floor: each series' largest boundary at or under it."""
    bounds = [boundary(bound) for bound in sorted({float(floor), *(b for b in buckets if b <= floor)})]
    return _nearest_bucket(histogram, matchers, bounds, "max")


def under_floor_at_most(histogram: str, matchers: dict[str, str], floor: float, buckets: tuple[float, ...]) -> str:
    """At most the session's observations under the floor: each series' smallest boundary at or over it."""
    bounds = [boundary(bound) for bound in sorted({float(floor), *(b for b in buckets if b >= floor)})]
    return _nearest_bucket(histogram, matchers, [*bounds, INFINITY], "min")


def _under_bounds(
    histogram: str, matchers: dict[str, str], floor: float, buckets: tuple[float, ...], count: str
) -> tuple[str, str]:
    """The least and the most the session's observations under the floor can be: the same query,
    where the floor is one of the gateway's own boundaries, which every task counts."""
    if float(floor) in map(float, buckets):
        under = under_floor(histogram, matchers, floor, buckets)
        return under, under
    least = f"({under_floor(histogram, matchers, floor, buckets)}) or on () (0 * ({count}))"
    return least, under_floor_at_most(histogram, matchers, floor, buckets)


def gateway_answers() -> Objective:
    """The share of the requests that were not a 5xx of the gateway's own fault, while any pool's
    session was active: the gateway's answers are about no pool."""
    requests = rules.over_sessions(rules.step_increase(REQUESTS, session=rules.ANY_POOL))
    faults = rules.over_sessions(rules.step_increase(OWN_FAULTS, session=rules.ANY_POOL))
    share = f"(({requests}) - (({faults}) or on () (0 * ({requests})))) / ({requests})"
    goal = f"at least {GATEWAY_TARGET:.1%} of requests not a 5xx of the gateway's own fault"
    measure = Measure(
        "not the gateway's fault", share, GATEWAY_TARGET, Comparison.AT_LEAST, GATEWAY_TARGET, "percentunit", goal
    )
    return Objective("Gateway answers", (measure,))


def first_token(config: Config) -> Objective:
    """The 95th percentile of the time to first token of each pool's streamed requests, first and
    later turns apart, in the pool's session."""
    target, q = config.target_ttft_p95_seconds, 0.95
    measures = []
    for pool in config.pools:
        for turn in (FIRST_TURN, LATER_TURN):
            matchers = {rules.POOL: pool.name, TURN: turn}
            count = _count(FIRST_TOKEN, matchers)
            least, most = _under_bounds(FIRST_TOKEN, matchers, target, WAIT_BUCKETS, count)
            goal = f"p95 under {target:g} s: at least {q:.0%} of the {turn} turns with a first token under {target:g} s"
            measures.append(
                Measure(
                    f"{pool.name} {turn} turns under {target:g} s",
                    f"({least}) / ({count})",
                    target,
                    Comparison.AT_LEAST,
                    q,
                    "percentunit",
                    goal,
                    q,
                    count,
                    None if least == most else f"({most}) / ({count})",
                    pool.name,
                )
            )
    return Objective("Time to first token", tuple(measures))


def stream_speed(config: Config) -> Objective:
    """The median and the slowest tenth of the speed of the coursework pool's streams, in the
    pool's session."""
    measures = []
    for pool in speed_pools(config):
        for q, name, floor in (
            (0.5, "median", config.target_stream_median_tokens_per_second),
            (0.1, "slowest tenth", config.target_stream_p10_tokens_per_second),
        ):
            matchers = {rules.POOL: pool.name}
            count = _count(SPEED, matchers)
            least, most = _under_bounds(SPEED, matchers, floor, SPEED_BUCKETS, count)
            needed = round(1 - q, 12)
            goal = f"{name} at {floor:g} tokens/s or more: above {needed:.0%} of the streams at {floor:g} or more"
            measures.append(
                Measure(
                    f"{pool.name} {name}, at {floor:g} tokens/s or more",
                    f"(({count}) - ({most})) / ({count})",
                    floor,
                    Comparison.ABOVE,
                    needed,
                    "percentunit",
                    goal,
                    q,
                    count,
                    None if least == most else f"(({count}) - ({least})) / ({count})",
                    pool.name,
                )
            )
    return Objective("Output speed per stream", tuple(measures))


def speed_pools(config: Config) -> tuple[Pool, ...]:
    """The pool the speed objective is measured for: the coursework pool, by its name, wherever
    the gateway's default route points."""
    return tuple(pool for pool in config.pools if pool.name == COURSEWORK)


def cached_share(config: Config) -> Objective:
    """For each pool, its engine's cached input tokens over its input tokens, while the pool's
    session is active: every sample an instance reported in the session, the scaler's count of
    instances in service aside, which may not have caught up with an instance that has just started
    reporting."""
    target = config.cached_share_flag_percent / 100
    measures = []
    for pool in config.pools:
        cached, total = (
            rules.over_sessions(rules.engine_step_increase(pool, quantity))
            for quantity in (Quantity.CACHED_INPUT_TOKENS, Quantity.INPUT_TOKENS)
        )
        goal = f"above {config.cached_share_flag_percent:g}% of input tokens cached"
        measures.append(
            Measure(
                pool.name, f"({cached}) / ({total})", target, Comparison.ABOVE, target, "percentunit", goal, pool=pool.name
            )
        )
    return Objective("Cached share of input", tuple(measures))


def objectives(config: Config) -> tuple[Objective, ...]:
    """The four objectives, in the order of section 7.3, with the configuration's targets."""
    return (gateway_answers(), first_token(config), stream_speed(config), cached_share(config))


def threshold_step(measure: Measure) -> float:
    """The least value a panel's threshold step may start at for the measure to be met: the
    threshold, or, for a value that must be above it, the next float."""
    if measure.comparison is Comparison.ABOVE:
        return math.nextafter(measure.threshold, math.inf)
    return measure.threshold
