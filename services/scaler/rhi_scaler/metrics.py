"""The scaler's metrics, sent to Managed Prometheus by signed remote write (system design, 7.1).

What the fleet dashboard shows of the scaler (section 7.2), per pool, on every run:

| Series | Kind | Labels | Value |
| --- | --- | --- | --- |
| `rhi_scaler_desired_capacity` | gauge | `pool` | The desired capacity the run decided |
| `rhi_scaler_instances_in_service` | gauge | `pool` | The instances in service the run read |
| `rhi_scaler_decisions_total` | counter | `pool`, `reason` | The decisions made with that reason |
| `rhi_scaler_session_active` | gauge | `pool` | 1 while the pool's session is active; 0 otherwise |
| `rhi_scaler_instances_requested` | gauge | `pool` | What the pool's session asks for now, 0 while none is active |
| `rhi_scaler_instances_waiting` | gauge | `pool` | The instances asked of AWS that have not come: the desired capacity less those starting and in service |
| `rhi_scaler_waiting_seconds` | gauge | `pool` | How long the pool has waited for them, 0 while nothing waits |
| `rhi_scaler_instances_starting` | gauge | `pool` | The instances starting the run read |
| `rhi_scaler_instances_draining` | gauge | `pool` | The instances draining the run read |
| `rhi_scaler_session_starts_at_seconds` | gauge | `pool` | The start of the pool's session, in seconds since the epoch |
| `rhi_scaler_session_ends_at_seconds` | gauge | `pool` | Its end (`rhi_common.sessions.end`), in seconds since the epoch |
| `rhi_scaler_launch_failure` | gauge | `pool`, `cause` | 1 for the cause in force, 0 for the other three |

What waits, since when and why (section 5.4) is the pool's note, as the run wrote it
(`rhi_scaler.aws_io.supply_note`): the series and the note hold the same numbers. The two series
of the session's window are sent while the pool's session has not ended, booked or active, and not
for a pool whose session has ended or that has none. The cause has one series for each of the four
words of `rhi_common.records.LaunchFailure`, on every run, so that a cause that ends reads 0
rather than stays at its last 1: at most one of the four is 1, and none is while nothing waits or
no launch has failed.

Every series carries the pool's label: the scaler sends none for the whole system. The only labels
are the pool's name, the decision's reason and the cause of a failed launch, the last two from
fixed vocabularies (`rhi_scaler.model.Reason`, `rhi_common.records.LaunchFailure`): no metric can
carry a student, or text from AWS.

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
from typing import Any

import cramjam
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from rhi_common import sessions
from rhi_common.records import LaunchFailure, ScalerNote
from rhi_scaler.model import Decision, Inputs, PoolInputs

DESIRED_CAPACITY = "rhi_scaler_desired_capacity"
INSTANCES_IN_SERVICE = "rhi_scaler_instances_in_service"
DECISIONS = "rhi_scaler_decisions_total"
GAUGES = (DESIRED_CAPACITY, INSTANCES_IN_SERVICE)
SESSION_ACTIVE = "rhi_scaler_session_active"
INSTANCES_REQUESTED = "rhi_scaler_instances_requested"
INSTANCES_WAITING = "rhi_scaler_instances_waiting"
WAITING_SECONDS = "rhi_scaler_waiting_seconds"
INSTANCES_STARTING = "rhi_scaler_instances_starting"
INSTANCES_DRAINING = "rhi_scaler_instances_draining"
# What each pool's note gives on every run, with the pool's label.
SUPPLY_GAUGES = (
    SESSION_ACTIVE,
    INSTANCES_REQUESTED,
    INSTANCES_WAITING,
    WAITING_SECONDS,
    INSTANCES_STARTING,
    INSTANCES_DRAINING,
)
# The window of a pool's session, sent while the session has not ended.
SESSION_STARTS_AT = "rhi_scaler_session_starts_at_seconds"
SESSION_ENDS_AT = "rhi_scaler_session_ends_at_seconds"
# Why the pool's last launch failed: one series for each cause, with the labels pool and cause.
LAUNCH_FAILURE = "rhi_scaler_launch_failure"

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


def supply_gauges(pool: PoolInputs, note: ScalerNote, now: int, session_max_hours: int) -> dict[str, int]:
    """What one pool's note and session give at the time `now`, by series: what the pool was asked
    and what waits, and the session's window while the session has not ended."""
    session = pool.session
    gauges = {
        SESSION_ACTIVE: int(sessions.is_active(session, now, session_max_hours=session_max_hours)),
        INSTANCES_REQUESTED: note.requested,
        INSTANCES_WAITING: note.waiting,
        WAITING_SECONDS: 0 if note.waiting_since is None else max(0, now - note.waiting_since),
        INSTANCES_STARTING: note.starting,
        INSTANCES_DRAINING: note.draining,
    }
    if session is not None and now < sessions.end(session, session_max_hours=session_max_hours):
        gauges[SESSION_STARTS_AT] = session.starts_at
        gauges[SESSION_ENDS_AT] = sessions.end(session, session_max_hours=session_max_hours)
    return gauges


def fleet_series(
    inputs: Inputs,
    decision: Decision,
    counts: DecisionCounts,
    milliseconds: int | None = None,
    *,
    notes: Mapping[str, ScalerNote],
) -> list[Series]:
    """Counts this run's decisions, and returns every series to send for it, pool by pool.

    `milliseconds` is the run's time; without it, the decision clock's second. `notes` holds each
    pool's note as the run wrote it (`rhi_scaler.aws_io.apply`): what the pool was asked, what
    waits and why.
    """
    milliseconds = counts.stamp(inputs.now * 1000 if milliseconds is None else milliseconds)
    read_pools = {pool.config.name: pool for pool in inputs.pools}
    out: list[Series] = []
    for pool in decision.pools:
        read, note = read_pools[pool.pool], notes[pool.pool]
        gauges = {
            DESIRED_CAPACITY: pool.desired,
            INSTANCES_IN_SERVICE: len(read.supply.in_service),
            **supply_gauges(read, note, inputs.now, inputs.session_max_hours),
        }
        out.extend(
            Series(name, (("pool", pool.pool),), ((float(value), milliseconds),)) for name, value in gauges.items()
        )
        out.extend(
            Series(
                LAUNCH_FAILURE,
                (("pool", pool.pool), ("cause", cause.value)),
                ((1.0 if note.launch_failure is cause else 0.0, milliseconds),),
            )
            for cause in LaunchFailure
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
