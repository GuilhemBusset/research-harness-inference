"""What students experience, per pool, as the gateway sees it (system design, sections 4.7, 7.2 and 7.3).

The engine measures its own work. The gateway measures what the student got, which adds the wait in
front of the engine, the hold and the network. These are the gateway's series for the quantities of
section 4.7 that it can see, beside the per-student ones (`rhi_gateway.metrics`) and on the same
registry, so the one endpoint serves both:

| Series | Kind | Labels | Value |
| --- | --- | --- | --- |
| `rhi_gateway_time_to_first_token_seconds` | histogram | `pool`, `wire_format`, `start`, `turn` | The seconds from a streamed request accepted to its first token sent |
| `rhi_gateway_time_to_first_answer_seconds` | histogram | `pool`, `wire_format`, `start`, `turn` | The same, to its first token that is not reasoning |
| `rhi_gateway_stream_output_tokens_per_second` | histogram | `pool`, `wire_format`, `start` | The output speed of each streamed request |
| `rhi_gateway_request_input_tokens` | histogram | `pool`, `wire_format` | The input tokens of each request settled: the transcript's size |
| `rhi_gateway_request_output_tokens` | histogram | `pool`, `wire_format` | The output tokens of each request settled: the output of a turn |
| `rhi_gateway_requests_in_flight` | gauge | `pool` | The requests this task has forwarded and not yet ended |
| `rhi_gateway_requests_held` | gauge | `pool` | The requests this task holds while their pool starts |

**Never by student** (section 7.2). No series here carries a student: `Experience.request` is given
a pool's name, a wire format and two facts of the request (whether it was held, whether it is a
first turn) and nothing else, so no alias, key or GitHub ID can become a label. Every value is the
gateway's own: a configured pool's name, one of the three formats, `warm` or `held`, `first` or
`later`.

**The two waits of a request** (sections 4.2 and 7.2). What a student waits for is the first token,
and then the first token that is not reasoning, which is when they see an answer begin: the model
reasons before it answers. Both are counted in seconds from the request accepted, which is the
moment `Experience.request` is called: its key, its body and its budget have passed, and its wait
for its pool, if it has one, has not begun. The first is observed at the first `Turn.token_sent` of the request,
the second at the first one that reports answer text or a tool call (`rhi_gateway.proxy` tells
which, as each format's reference marks its events). Like the speed, both only mean something on a
stream: a request that is not one observes neither, and a stream that only reasoned observes no
answer. Each carries two more labels:

- `start` is `held` for a request that waited for its pool, whose wait is then in both times, and
  `warm` otherwise;
- `turn` is `first` for a request whose conversation carries no earlier output of the model, and
  `later` otherwise. A first turn computes its whole opening, which no cache holds yet (section
  4.7), so the objective counts the first turns of sessions apart.

The objective of section 7.3 is a time to first token under 5 seconds at the 95th percentile on a
warm pool (section 17). 5 seconds is a boundary of both histograms, so that it is read exactly.

**A bucket counts the values strictly under its boundary.** Every wait and every speed is observed
one step of the float above itself (`_strictly`): a wait of exactly 5 seconds lands above the
bucket of 5, a stream of exactly 15 tokens per second above the bucket of 15. So, at a floor, the
bucket reads exactly both what an objective asks: the share under 5 seconds, and the share at 15 or
more (`rhi_dashboards.objectives`). The step is 10^-15 of the value, far below anything measured.

**The speed of a request** is its output tokens after the first, over the seconds from its first
token sent to its last: 401 tokens whose first is sent at 2 seconds and whose last at 22 ran at 20
tokens per second. It only means something on a stream, where tokens are sent as they come, so it
is observed for a request the proxy reported at least two token times for, at two different
moments, and that settled at least two output tokens. The proxy calls `Turn.token_sent` each time
an event that carries output has been written to the client (`rhi_gateway.proxy`); the first and
the last call are the two times, read on this module's clock. The output tokens are those the
settlement observed.

The objectives of section 7.3 are read from this histogram against floors held in configuration:
the median stream at 15 tokens per second or more, the slowest tenth at 10 or more (section 17), on
a warm pool. So the speed carries `start` too, `held` or `warm` as the waits do: a request that
waited for its pool is not one of a warm pool. A floor that falls between two buckets cannot be
read exactly, so each floor of section 17 is a bucket boundary (`rhi_common.boundaries`), and the
gateway adds the floors its configuration holds (`rhi_gateway.settings`), so that a revised one is
read exactly too.

**The tokens of a request** are observed once for every request that settles, whatever its end and
streamed or not, with the tokens its settlement observed: the ones the per-student counters add up
(`rhi_gateway.metrics`). A request that ended with nothing observed (its hold ran out, its engine
failed) is observed at zero, and so is the output of a token count; each of the two histograms has
a bucket at zero, so a panel can read the distribution above it.

**In flight and held** are not counted here. At every scrape they are read from the rows the
heartbeat would write at that moment (`rhi_gateway.heartbeat.Heartbeat.rows`), so they are the
counts the scaler is told, an explicit wake counted as held included. Like those rows they are
this task's own: the collector labels every series with the task's ID, and a sum over the tasks
is the pool's.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Histogram
from prometheus_client.core import GaugeMetricFamily

from rhi_common.boundaries import SPEED_BUCKETS, WAIT_BUCKETS

if TYPE_CHECKING:
    from prometheus_client import Metric

    from rhi_common.records import GatewayHeartbeat
    from rhi_gateway.errors import WireFormat

# The families' names.
SPEED = "rhi_gateway_stream_output_tokens_per_second"
INPUT_TOKENS = "rhi_gateway_request_input_tokens"
OUTPUT_TOKENS = "rhi_gateway_request_output_tokens"
IN_FLIGHT = "rhi_gateway_requests_in_flight"
HELD = "rhi_gateway_requests_held"
FIRST_TOKEN = "rhi_gateway_time_to_first_token_seconds"
FIRST_ANSWER = "rhi_gateway_time_to_first_answer_seconds"
SERIES = (SPEED, INPUT_TOKENS, OUTPUT_TOKENS, IN_FLIGHT, HELD, FIRST_TOKEN, FIRST_ANSWER)

# Tokens of one request. Zero is a request that settled with none observed. The last input boundary
# is the full context of the coursework model (section 4.2).
INPUT_BUCKETS = (0, 1_000, 2_000, 5_000, 10_000, 20_000, 30_000, 50_000, 75_000, 100_000, 150_000, 200_000, 262_144)
OUTPUT_BUCKETS = (0, 10, 25, 50, 100, 250, 500, 1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 64_000)

# The seconds of both waits, and the tokens per second of a stream, are counted in the buckets of
# `rhi_common.boundaries`, with the configured floors of the objectives among them (`Settings`).

_LABELS = ("pool", "wire_format")
_SPEED_LABELS = (*_LABELS, "start")
_WAIT_LABELS = (*_SPEED_LABELS, "turn")


def _strictly(value: float) -> float:
    """The value as observed: the next float above it, so that a bucket's boundary is not in it."""
    return math.nextafter(value, math.inf)


class Turn:
    """One request, from its reservation to its settlement: its token times, then its tokens."""

    def __init__(
        self, experience: Experience, pool: str, wire_format: WireFormat, *, held: bool, first_turn: bool
    ) -> None:
        self._experience = experience
        self._labels = (pool, wire_format.value)
        self._speed_labels = (*self._labels, "held" if held else "warm")
        self._wait_labels = (*self._speed_labels, "first" if first_turn else "later")
        self._accepted_at = experience.clock()
        self._first_token_at: float | None = None
        self._last_token_at: float | None = None
        self._answered = False

    def token_sent(self, answer: bool) -> None:
        """Output has just been written to the client's stream; `answer` when some of it is not reasoning."""
        series = self._experience
        now = series.clock()
        if self._first_token_at is None:
            self._first_token_at = now
            series.first_token.labels(*self._wait_labels).observe(_strictly(now - self._accepted_at))
        if answer and not self._answered:
            self._answered = True
            series.first_answer.labels(*self._wait_labels).observe(_strictly(now - self._accepted_at))
        self._last_token_at = now

    def settled(self, input_tokens: int, output_tokens: int) -> None:
        """The request has settled with these tokens observed. Called once."""
        series = self._experience
        series.input_tokens.labels(*self._labels).observe(input_tokens)
        series.output_tokens.labels(*self._labels).observe(output_tokens)
        first, last = self._first_token_at, self._last_token_at
        if first is None or last is None or last <= first or output_tokens < 2:
            return
        series.speed.labels(*self._speed_labels).observe(_strictly((output_tokens - 1) / (last - first)))


class _Demand:
    """The requests in flight and held, per pool, read from the heartbeat's rows at every scrape."""

    def __init__(self, rows: Callable[[], Iterable[GatewayHeartbeat]]) -> None:
        self._rows = rows

    def describe(self) -> Iterable[Metric]:
        # The registry asks the families' names when the collector is registered: no read for that.
        return self._families()

    def collect(self) -> Iterable[Metric]:
        in_flight, held = self._families()
        for row in self._rows():
            in_flight.add_metric([row.pool], row.in_flight)
            held.add_metric([row.pool], row.held)
        return [in_flight, held]

    @staticmethod
    def _families() -> tuple[GaugeMetricFamily, GaugeMetricFamily]:
        return (
            GaugeMetricFamily(IN_FLIGHT, "Requests forwarded to the pool and not yet ended.", labels=["pool"]),
            GaugeMetricFamily(HELD, "Requests held while the pool starts, explicit wakes included.", labels=["pool"]),
        )


class Experience:
    """The series of this module, on `registry`: the gateway's own, which its metrics endpoint serves.

    `demand` gives the heartbeat's rows as they would be written now (`Heartbeat.rows`): without it
    there are no series of requests in flight and held. `clock` gives the seconds a request's
    acceptance and its token times are read in, on a clock that never goes back; a test gives its own.
    `wait_buckets` and `speed_buckets` are the boundaries of the two waits and of the speed: the
    gateway's settings give them with the configured floors among them.
    """

    def __init__(
        self,
        registry: CollectorRegistry,
        demand: Callable[[], Iterable[GatewayHeartbeat]] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait_buckets: tuple[float, ...] = WAIT_BUCKETS,
        speed_buckets: tuple[float, ...] = SPEED_BUCKETS,
    ) -> None:
        self.clock = clock
        self.first_token = Histogram(
            FIRST_TOKEN,
            "Seconds from a streamed request accepted to its first token sent, reasoning included.",
            _WAIT_LABELS,
            buckets=wait_buckets,
            registry=registry,
        )
        self.first_answer = Histogram(
            FIRST_ANSWER,
            "Seconds from a streamed request accepted to its first answer text or tool call sent.",
            _WAIT_LABELS,
            buckets=wait_buckets,
            registry=registry,
        )
        self.speed = Histogram(
            SPEED,
            "Output tokens after the first, over the seconds from the first token sent to the last, per stream.",
            _SPEED_LABELS,
            buckets=speed_buckets,
            registry=registry,
        )
        self.input_tokens = Histogram(
            INPUT_TOKENS,
            "Input tokens of a request settled, cached or not.",
            _LABELS,
            buckets=INPUT_BUCKETS,
            registry=registry,
        )
        self.output_tokens = Histogram(
            OUTPUT_TOKENS, "Output tokens of a request settled.", _LABELS, buckets=OUTPUT_BUCKETS, registry=registry
        )
        if demand is not None:
            registry.register(_Demand(demand))

    def request(self, pool: str, wire_format: WireFormat, *, held: bool, first_turn: bool) -> Turn:
        """A request for `pool` in `wire_format`, accepted now, which will settle once.

        `held` is whether it waits for its pool, and `first_turn` whether its conversation carries
        no earlier output of the model. Nothing of its student is taken.
        """
        return Turn(self, pool, wire_format, held=held, first_turn=first_turn)
