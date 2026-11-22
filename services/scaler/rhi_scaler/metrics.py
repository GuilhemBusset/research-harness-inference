"""The scaler's metrics, sent to Managed Prometheus by signed remote write (system design, 7.1).

What the fleet dashboard shows of the scaler (section 7.2), per pool, on every run:

| Series | Kind | Labels | Value |
| --- | --- | --- | --- |
| `rhi_scaler_desired_capacity` | gauge | `pool` | The desired capacity the run decided |
| `rhi_scaler_instances_in_service` | gauge | `pool` | The instances in service the run read |
| `rhi_scaler_warm_instances` | gauge | `pool` | The instances in the warm pool the run read |
| `rhi_scaler_decisions_total` | counter | `pool`, `reason` | The decisions made with that reason |

And once for the whole system, on every run:

| Series | Kind | Labels | Value |
| --- | --- | --- | --- |
| `rhi_scaler_session_on` | gauge | none | 1 while the session switch reads on; 0 while it reads off or has no record |

The only labels are the pool's name and the decision's reason, a fixed vocabulary
(`rhi_scaler.model.Reason`): no metric can carry a student.

**The session gauge** (system design, sections 5.6 and 7.3). PromQL knows nothing of the session
switch, so the scaler, which runs every minute, says what it read: the objectives keep only the
samples taken while this gauge is 1 (`rhi_dashboards.objectives`). A run that could not read the
switch sends no sample of it, rather than a value it does not know.

A reading takes effect no earlier than the change it reads. A run reads the switch some seconds
after it starts, and the switch may have changed in between: its sample then carries the first
millisecond of the second after the switch's own time (`set_at`, a whole second, so the change
falls within it), not the run's time, and every scrape before that still reads the state before.
A reading, on or off, carries at the earliest the time its read returned: the admin's command
takes `set_at` before its write, which may commit later still, and until it does the switch still
reads as before, so a scrape taken before the read returned keeps the session's last reading (an
off stays off, and an on stays on).

The function keeps nothing between runs but what its process holds, so the counter counts from
the process's start, and Prometheus reads a smaller value as a restart (as it does for any
process). A series a process sends for the first time starts with a zero one millisecond before
its first value, so that the first decision of a process is counted by `increase()` too.

The samples carry the run's time in milliseconds, not the decision clock's whole second: two runs
in one second would otherwise send two values of a counter at one timestamp, and Prometheus keeps
only the first. Within a process the timestamps only grow, by at least a millisecond a run.

Remote write is a protobuf `WriteRequest`, compressed with snappy's block format and sent by an
HTTP POST signed with SigV4 for the service `aps`. The message types are declared here from a
descriptor, at import: nothing runs protoc and no generated code is checked in.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import cramjam
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from rhi_common.records import SessionSwitch, session_is_on
from rhi_scaler.model import Decision, Inputs

DESIRED_CAPACITY = "rhi_scaler_desired_capacity"
INSTANCES_IN_SERVICE = "rhi_scaler_instances_in_service"
WARM_INSTANCES = "rhi_scaler_warm_instances"
DECISIONS = "rhi_scaler_decisions_total"
GAUGES = (DESIRED_CAPACITY, INSTANCES_IN_SERVICE, WARM_INSTANCES)
SESSION_ON = "rhi_scaler_session_on"

SERVICE = "aps"
# How long one push may take: the function's own timeout bounds the run.
SEND_TIMEOUT_SECONDS = 10
HEADERS = {
    "Content-Type": "application/x-protobuf",
    "Content-Encoding": "snappy",
    "X-Prometheus-Remote-Write-Version": "0.1.0",
}


def _remote_write_types() -> tuple[Any, Any]:
    """`WriteRequest` and `TimeSeries` of Prometheus's remote write (`prompb`), from a descriptor."""
    # descriptor_pb2 builds its classes at import, from a serialized descriptor: ty cannot see them.
    file = descriptor_pb2.FileDescriptorProto(  # ty: ignore[missing-attribute]
        name="rhi_remote_write.proto", package="prometheus", syntax="proto3"
    )
    kinds = descriptor_pb2.FieldDescriptorProto  # ty: ignore[missing-attribute]

    def message(name: str, *fields: tuple[str, int, int, int, str]) -> None:
        declared = file.message_type.add(name=name)
        for field_name, number, kind, label, type_name in fields:
            added = declared.field.add(name=field_name, number=number, type=kind, label=label)
            if type_name:
                added.type_name = type_name

    one, many = kinds.LABEL_OPTIONAL, kinds.LABEL_REPEATED
    message("Label", ("name", 1, kinds.TYPE_STRING, one, ""), ("value", 2, kinds.TYPE_STRING, one, ""))
    message("Sample", ("value", 1, kinds.TYPE_DOUBLE, one, ""), ("timestamp", 2, kinds.TYPE_INT64, one, ""))
    message(
        "TimeSeries",
        ("labels", 1, kinds.TYPE_MESSAGE, many, ".prometheus.Label"),
        ("samples", 2, kinds.TYPE_MESSAGE, many, ".prometheus.Sample"),
    )
    message("WriteRequest", ("timeseries", 1, kinds.TYPE_MESSAGE, many, ".prometheus.TimeSeries"))
    # A pool of its own, so that nothing else declared under `prometheus` can clash. protobuf
    # defines the class's `__new__` only under a condition, which ty reads as possibly missing.
    pool = descriptor_pool.DescriptorPool()  # ty: ignore[possibly-missing-implicit-call]
    pool.Add(file)
    return (
        message_factory.GetMessageClass(pool.FindMessageTypeByName("prometheus.WriteRequest")),
        message_factory.GetMessageClass(pool.FindMessageTypeByName("prometheus.TimeSeries")),
    )


WriteRequest, TimeSeries = _remote_write_types()


@dataclass(frozen=True)
class Series:
    """One series: its name, its labels other than the name, and its samples (value, milliseconds)."""

    name: str
    labels: tuple[tuple[str, str], ...]
    samples: tuple[tuple[float, int], ...]


@dataclass
class DecisionCounts:
    """The decisions this process has made, by pool and reason."""

    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    # The timestamp of this process's last run, so that no two runs share one.
    last_milliseconds: int | None = None

    def add(self, pool: str, reason: str) -> None:
        self.counts[(pool, reason)] = self.counts.get((pool, reason), 0) + 1

    def stamp(self, milliseconds: int) -> int:
        """The timestamp for a run at `milliseconds`: later than every run before it."""
        if self.last_milliseconds is not None and milliseconds <= self.last_milliseconds:
            milliseconds = self.last_milliseconds + 1
        self.last_milliseconds = milliseconds
        return milliseconds

    def passed(self, milliseconds: int) -> None:
        """A sample of this run went out at `milliseconds`: no later run stamps one at or before it."""
        if self.last_milliseconds is None or milliseconds > self.last_milliseconds:
            self.last_milliseconds = milliseconds


class Unread(Enum):
    """A session switch the run could not read: neither on nor off."""

    UNREAD = "unread"


UNREAD = Unread.UNREAD


def session_series(switch: SessionSwitch | None, milliseconds: int, read_at: int | None = None) -> Series:
    """The session gauge's sample: 1 while the switch reads on, 0 while it reads off or has no record.

    It carries the run's time `milliseconds`, or, when the switch changed later than that, the
    first millisecond of the second after its change: never a time before the change it reads. A
    reading, on or off, carries no time before `read_at`, when the read returned, if it is given:
    the write it reads committed by then, and maybe only just.
    """
    on = session_is_on(switch)
    if switch is not None:
        milliseconds = max(milliseconds, (switch.set_at + 1) * 1000)
    if read_at is not None:
        milliseconds = max(milliseconds, read_at)
    return Series(SESSION_ON, (), ((1.0 if on else 0.0, milliseconds),))


def session_alone(
    switch: SessionSwitch | None, counts: DecisionCounts, milliseconds: int, *, read_at: int | None = None
) -> list[Series]:
    """The session gauge on its own, for a run whose fleet series are not sent, at the run's time
    `milliseconds` (or later, as `session_series` says)."""
    series = session_series(switch, counts.stamp(milliseconds), read_at)
    counts.passed(series.samples[-1][1])
    return [series]


def fleet_series(
    inputs: Inputs,
    decision: Decision,
    counts: DecisionCounts,
    milliseconds: int | None = None,
    *,
    session: SessionSwitch | None | Unread = UNREAD,
    read_at: int | None = None,
) -> list[Series]:
    """Counts this run's decisions, and returns every series to send for it, pool by pool.

    `milliseconds` is the run's time; without it, the decision clock's second. `session` is what
    the run read of the session switch (`None` for no record), and gives the session gauge;
    `UNREAD`, a switch the run could not read, gives no sample of it. `read_at` is when the read
    of the switch returned (`session_series`).
    """
    milliseconds = counts.stamp(inputs.now * 1000 if milliseconds is None else milliseconds)
    supplies = {pool.config.name: pool.supply for pool in inputs.pools}
    out: list[Series] = []
    if not isinstance(session, Unread):
        gauge = session_series(session, milliseconds, read_at)
        counts.passed(gauge.samples[-1][1])
        out.append(gauge)
    for pool in decision.pools:
        supply = supplies[pool.pool]
        gauges = {
            DESIRED_CAPACITY: pool.desired,
            INSTANCES_IN_SERVICE: len(supply.in_service),
            WARM_INSTANCES: len(supply.warm),
        }
        out.extend(
            Series(name, (("pool", pool.pool),), ((float(value), milliseconds),)) for name, value in gauges.items()
        )
        first = (pool.pool, pool.reason.value) not in counts.counts
        counts.add(pool.pool, pool.reason.value)
        for (name, reason), value in sorted(counts.counts.items()):
            if name != pool.pool:
                continue
            samples = ((float(value), milliseconds),)
            if first and reason == pool.reason.value:
                samples = ((0.0, milliseconds - 1), *samples)
            out.append(Series(DECISIONS, (("pool", name), ("reason", reason)), samples))
    return out


def encode(series: Iterable[Series]) -> bytes:
    """The remote-write body: a `WriteRequest`, compressed with snappy's block format."""
    request = WriteRequest()
    for one in series:
        timeseries = request.timeseries.add()
        # Remote write wants the labels sorted by name; `__name__` sorts first.
        for name, value in sorted((("__name__", one.name), *one.labels)):
            timeseries.labels.add(name=name, value=value)
        for value, timestamp in one.samples:
            timeseries.samples.add(value=value, timestamp=timestamp)
    return bytes(cramjam.snappy.compress_raw(request.SerializeToString()))


def decode(body: bytes) -> Any:
    """The `WriteRequest` of a remote-write body."""
    return WriteRequest.FromString(bytes(cramjam.snappy.decompress_raw(body)))


Sender = Callable[[str, Mapping[str, str], bytes], None]


def send_with_urllib(url: str, headers: Mapping[str, str], body: bytes) -> None:
    """POSTs the body; an answer that is not 2xx raises."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS):
        pass


@dataclass(frozen=True)
class RemoteWriter:
    """Sends series to one workspace, signed with the credentials given, for the region given."""

    url: str
    region: str
    credentials: Any
    send: Sender = send_with_urllib

    def signed_headers(self, body: bytes) -> dict[str, str]:
        request = AWSRequest(method="POST", url=self.url, data=body, headers=dict(HEADERS))
        SigV4Auth(self.credentials, SERVICE, self.region).add_auth(request)
        return dict(request.headers.items())

    def push(self, series: Iterable[Series]) -> None:
        body = encode(series)
        self.send(self.url, self.signed_headers(body), body)
