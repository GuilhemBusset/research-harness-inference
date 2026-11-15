"""The hold: what the gateway does for a request while its pool starts (system design, sections 5.1 and 5.3).

Client retries cannot cover a GPU cold start, so the gateway holds the request. This module is the
rule alone, a state machine with no I/O: it reads no clock, no table and no socket. The caller
makes a `Hold` when the request arrives and calls `step` with the time and with what it knows of
the pool at that time: its mode, the scaler's note (`rhi_common.records`) and whether it is ready.
`step` returns what to do now, and `next_at` says when to call again if nothing changes before. The
time is in seconds on the clock of the scaler's note, since the epoch.

| Action | The caller |
| --- | --- |
| `KeepAlive` | writes its bytes on the stream |
| `Wake` | asks the scaler for capacity |
| `Forward` | sends the request to the engine: the pool is ready |
| `End` | ends the request with the cause, in its wire format |

The rules, in the order a step applies them while the request is held:

1. A pool in mode `disabled` or `budget-latched` takes no request, whatever is in service: the
   request ends with `pool_disabled` or `pool_ceiling_reached`.
2. A ready pool has the request forwarded. The hold's deadline no longer applies.
3. Students cannot wake a `scheduled-only` pool. With no capacity asked for by a schedule or the
   admin, the request ends with `pool_scheduled_only`; with some, it is held and no wake is asked.
4. At the deadline, the pool's `hold_seconds` for a stream and its `hold_seconds_non_streaming`
   for a non-streaming call, the request ends with `capacity_unavailable` if the note says a launch
   found no capacity in the past 10 minutes, and with `warming` otherwise.
5. A pool in mode `enabled` is asked to wake at once, and again every 15 seconds.

A stream gets a keep-alive every `keepalive_seconds`, counted from the start of the hold: a real
`ping` event on Messages, an SSE comment on the OpenAI formats. They do not stop when the request
is forwarded, because the engine is silent while it computes a long transcript: they stop when the
caller reports the engine's first event, with `first_event`. A non-streaming request gets none.

Keep-alives and wakes are due on a grid from the start of the hold. A step that comes late sends
one of each, not one for every time it missed, and the next is due on the grid again.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from rhi_common.causes import Cause
from rhi_common.pool_config import Pool
from rhi_common.records import Mode, ScalerNote
from rhi_gateway.errors import WireFormat

# Section 17. The configuration key that carries another value to the gateway is not this module's.
DEFAULT_KEEPALIVE_SECONDS = 10
# The gateway repeats the wake this often while it holds (section 5.1): the design's own constant.
WAKE_SECONDS = 15
# A launch that found no capacity counts for this long (section 5.3).
NO_CAPACITY_SECONDS = 600

_PING = b'event: ping\ndata: {"type": "ping"}\n\n'
_COMMENT = b": keep-alive\n\n"

# The modes of a pool that will not start for anyone, and the cause each gives.
_STOPPED = {Mode.DISABLED: Cause.POOL_DISABLED, Mode.BUDGET_LATCHED: Cause.POOL_CEILING_REACHED}


@dataclass(frozen=True)
class KeepAlive:
    """Write `data` on the stream: a `ping` event on Messages, an SSE comment on the OpenAI formats."""

    data: bytes


@dataclass(frozen=True)
class Wake:
    """Ask the scaler for capacity."""


@dataclass(frozen=True)
class Forward:
    """The pool is ready: send the request to the engine."""


@dataclass(frozen=True)
class End:
    """End the request with this cause."""

    cause: Cause


type Action = KeepAlive | Wake | Forward | End


def keep_alive(wire_format: WireFormat) -> KeepAlive:
    """The keep-alive of a held stream in one wire format (section 5.3)."""
    return KeepAlive(_PING if wire_format is WireFormat.MESSAGES else _COMMENT)


class _Phase(Enum):
    HELD = auto()
    # Forwarded, and the engine has not sent its first event: only a stream is ever here.
    FORWARDED = auto()
    OVER = auto()


class Hold:
    """One request's hold, from its arrival at `started_at` to the engine's first event or an end."""

    def __init__(
        self,
        pool: Pool,
        wire_format: WireFormat,
        *,
        streams: bool,
        started_at: float,
        keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
    ) -> None:
        if keepalive_seconds <= 0:
            raise ValueError("the keep-alive interval must be more than zero seconds")
        self._streams = streams
        self._started_at = started_at
        self._keepalive_seconds = keepalive_seconds
        self._keep_alive = keep_alive(wire_format)
        self._deadline = started_at + (pool.hold_seconds if streams else pool.hold_seconds_non_streaming)
        self._phase = _Phase.HELD
        # The last point of each grid that was acted on. The grids start with the hold: a wake is
        # due at once, and a keep-alive one interval later.
        self._keepalive_tick = 0
        self._wake_tick = -1
        # Whether the pool was one this request wakes, at the last step.
        self._wakes = True

    @property
    def next_at(self) -> float | None:
        """When the next step has something to do if the pool does not change before; None once over.

        A change of the pool, its readiness above all, is a reason to step sooner.
        """
        if self._phase is _Phase.OVER:
            return None
        due: list[float] = []
        if self._streams:
            due.append(self._started_at + (self._keepalive_tick + 1) * self._keepalive_seconds)
        if self._phase is _Phase.HELD:
            due.append(self._deadline)
            if self._wakes:
                due.append(self._started_at + (self._wake_tick + 1) * WAKE_SECONDS)
        return min(due)

    def first_event(self) -> None:
        """The engine sent its first event: no keep-alive after it. Means nothing before the request is forwarded."""
        if self._phase is _Phase.FORWARDED:
            self._phase = _Phase.OVER

    def step(self, now: float, *, mode: Mode, note: ScalerNote | None, ready: bool) -> tuple[Action, ...]:
        """What to do at the time `now`, the pool being as given. `note` is None if the scaler wrote none.

        Once the request is forwarded the pool no longer matters, and once the hold is over
        nothing is returned.
        """
        if self._phase is _Phase.OVER:
            return ()
        if self._phase is _Phase.FORWARDED:
            return self._keep_alive_due(now)

        if mode in _STOPPED:
            return self._end(_STOPPED[mode])
        if ready:
            # The keep-alive due at this very time is still sent: the cadence does not break.
            actions = (*self._keep_alive_due(now), Forward())
            self._phase = _Phase.FORWARDED if self._streams else _Phase.OVER
            return actions
        self._wakes = mode is Mode.ENABLED
        if not self._wakes and (note is None or note.capacity_asked == 0):
            return self._end(Cause.POOL_SCHEDULED_ONLY)
        if now >= self._deadline:
            found_none_at = None if note is None else note.no_capacity_at
            recently = found_none_at is not None and now - found_none_at <= NO_CAPACITY_SECONDS
            return self._end(Cause.CAPACITY_UNAVAILABLE if recently else Cause.WARMING)
        return (*self._keep_alive_due(now), *self._wake_due(now))

    def _end(self, cause: Cause) -> tuple[Action, ...]:
        self._phase = _Phase.OVER
        return (End(cause),)

    def _keep_alive_due(self, now: float) -> tuple[Action, ...]:
        tick = int((now - self._started_at) // self._keepalive_seconds)
        if not self._streams or tick <= self._keepalive_tick:
            return ()
        self._keepalive_tick = tick
        return (self._keep_alive,)

    def _wake_due(self, now: float) -> tuple[Action, ...]:
        tick = int((now - self._started_at) // WAKE_SECONDS)
        if not self._wakes or tick <= self._wake_tick:
            return ()
        self._wake_tick = tick
        return (Wake(),)
