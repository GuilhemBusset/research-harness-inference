"""Spike S7's job (`spikes/s7_drain.py`): each step writes its lines, on stand-ins and a clock the
test controls. The pool's session is an item of a table moto holds, so the conditions of
`scripts/pipeline/booking.py` hold as DynamoDB holds them; a scripted scaler follows that session
as the scaler does, once a minute, and answers for the note, its gauge, the instances' collectors
and what their lifecycle agents say of their launches; the gateway is a stub transport. No AWS,
no network.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import boto3
import botocore.exceptions
import httpx
import pytest
from moto import mock_aws

import common
import s7_drain as s7
from rhi import config
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_common.records import NOTE_RECORD, SESSION_RECORD, ScalerNote, Session
from rhi_common.tables import POOL_STATE
from rhi_gateway.errors import WireFormat, render_stream_event

T0 = 1_800_000_000.0
HOURS = 12
MINUTES = 120
UTC_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
# What an AWS error's text might hold: it must reach no line.
AWS_TEXT = "an AWS message naming arn:aws:dynamodb:us-east-1:123456789012:table/pool-state"
# For each instance the group launches, in order: the seconds after its launch at which its
# collector first writes, None when it never does, and those after which it is in service; then,
# when given, those after which its launch's hook is completed, ten seconds before it is in service
# otherwise.
PROMPT = (15.0, 70.0)


class FakeClock:
    """A clock the test moves: `sleep` advances it at once."""

    def __init__(self, now: float = T0) -> None:
        self.at = now

    def now(self) -> float:
        return self.at

    async def sleep(self, seconds: float) -> None:
        self.at += seconds
        await asyncio.sleep(0)


@pytest.fixture
def stored() -> Iterator[Any]:
    """The pool-state table, in moto, with the key schema of `rhi_common.tables.POOL_STATE` and no item."""
    with mock_aws():
        keys = [(POOL_STATE.partition_key, "HASH"), (POOL_STATE.sort_key, "RANGE")]
        yield boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="pool-state-under-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys if key is not None
            ],
            KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys if key is not None],
        )


def session_of(stored: Any) -> Session | None:
    item = stored.get_item(Key={"pool": "mock", "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
    return None if item is None else Session.from_item(item)


@dataclass
class Machine:
    """One instance of the scripted pool, under an identifier of the shape AWS gives: when it is in
    service, when its collector first writes, None if it never does, when its lifecycle agent
    completed its launch's hook, and when it is gone, once the group released it. Its collector
    writes every 15 seconds until then, and each write holds what the agent says of its launch:
    that it started, and that its hook is completed once it is."""

    in_service_at: float
    first_write: float | None
    hooked_at: float
    gone_at: float = math.inf
    name: str = field(default_factory=lambda: "i-" + secrets.token_hex(9)[:17])

    def last_write(self, at: float = math.inf) -> float | None:
        until = min(at, self.gone_at)
        if self.first_write is None or until < self.first_write:
            return None
        return self.first_write + 15 * ((until - self.first_write) // 15)


@dataclass
class Scaler:
    """The scaler, the group and the workspace. Every minute the scaler reads the session stored as
    of that run and the pool as it is, writes its note and its in-service gauge, then asks the
    group for what the session asks: the group launches `launches`, in order, and releases the
    instance that is still starting or the one in service first, as `leaves` says. An instance
    released is gone `drain` seconds later. With `follows_lowering` false it keeps what it asked
    for while the session is active, as before the scaler released the instances of a lowered
    session. It is also the pool-state table: the session's item is moto's, and every session
    written is kept with the time."""

    clock: FakeClock
    stored: Any
    launches: list[tuple[float | None, ...]]
    leaves: s7.Left = s7.Left.STARTING
    follows_lowering: bool = True
    drain: float = 30.0
    failing: bool = False
    machines: list[Machine] = field(default_factory=list)
    notes: list[ScalerNote] = field(default_factory=list)
    written: list[tuple[float, Session]] = field(default_factory=list)

    def _advance(self) -> None:
        while (run := T0 - 120 + 60 * len(self.notes)) <= self.clock.now():
            held = [session for at, session in self.written if at <= run]
            want = held[-1].instances if held and held[-1].starts_at <= run < held[-1].ends_at else 0
            asked = want if self.follows_lowering or want == 0 else max(want, self.notes[-1].capacity_asked)
            present = [machine for machine in self.machines if machine.gone_at == math.inf]
            serving = [machine for machine in present if machine.in_service_at <= run]
            starting = [machine for machine in present if machine.in_service_at > run]
            draining = [machine for machine in self.machines if run < machine.gone_at < math.inf]
            self.notes.append(
                ScalerNote(
                    "mock",
                    asked,
                    None,
                    int(run),
                    requested=want,
                    starting=len(starting),
                    in_service=len(serving),
                    draining=len(draining),
                )
            )
            # A run reads the pool as it was before it acted.
            for _ in range(asked - len(present)):
                first, in_service, *hooked = self.launches.pop(0)
                assert in_service is not None
                hooked_at = run + (hooked[0] if hooked and hooked[0] is not None else in_service - 10)
                self.machines.append(Machine(run + in_service, None if first is None else run + first, hooked_at))
            ordered = starting + serving if self.leaves is s7.Left.STARTING else serving + starting
            for machine in ordered[: max(0, len(present) - asked)]:
                machine.gone_at = run + self.drain

    def asked_first(self, asked: int) -> float:
        """When the scaler first asked for `asked` instances, once it had asked for two."""
        two = next(index for index, note in enumerate(self.notes) if note.capacity_asked == s7.INSTANCES)
        return next(float(note.written_at) for note in self.notes[two:] if note.capacity_asked == asked)

    # The pool-state table.
    def get_item(self, **arguments: Any) -> dict[str, Any]:
        if arguments["Key"]["record"] != NOTE_RECORD:
            return self.stored.get_item(**arguments)
        if self.failing:
            error = {"Error": {"Code": "ThrottlingException", "Message": AWS_TEXT}}
            raise botocore.exceptions.ClientError(error, "GetItem")
        self._advance()
        return {"Item": self.notes[-1].to_item()}

    def put_item(self, **arguments: Any) -> Any:
        answer = self.stored.put_item(**arguments)
        self.written.append((self.clock.now(), Session.from_item(arguments["Item"])))
        return answer

    # The workspace.
    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        self._advance()
        steps = [min(start + step * n, self.clock.now()) for n in range(int((end - start) // step) + 1)]
        if query.startswith("max by (instance)"):
            series = []
            for machine in self.machines:
                # The agent's series of the stage `hook` is in the writes from its completion on.
                since = machine.hooked_at if 'stage="hook"' in query else -math.inf
                last = [(at, machine.last_write(at)) for at in steps]
                samples = tuple((at, write) for at, write in last if write is not None and since <= write >= at - 300)
                series.append(common.Series({"instance": machine.name}, samples))
            return series
        assert s7.INSTANCES_IN_SERVICE in query
        samples = []
        for at in steps:
            runs = [note for note in self.notes if note.written_at + 0.2 <= at]
            if runs:
                stamp = runs[-1].written_at + 0.2
                samples.append((at, stamp if query.startswith("timestamp(") else float(runs[-1].in_service)))
        return [common.Series({"pool": "mock"}, tuple(samples))]


def sse(name: str, data: dict[str, Any] | None = None) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data or {'type': name})}\n\n".encode()


TURN = [
    sse("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 50}}}),
    sse("content_block_start"),
    sse("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}),
    sse("content_block_stop"),
    sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 2}}),
    sse("message_stop"),
]
ENGINE_ERROR = [render_stream_event(Cause.ENGINE_ERROR, WireFormat.MESSAGES)]


@dataclass(frozen=True)
class Reply:
    """What the gateway answers one request with: a status, the events, and whether the connection
    is cut after them."""

    events: list[bytes] = field(default_factory=lambda: list(TURN))
    status: int = 200
    cut: bool = False


class Events(httpx.AsyncByteStream):
    def __init__(self, reply: Reply) -> None:
        self.reply = reply

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.reply.events:
            yield event
        if self.reply.cut:
            raise httpx.RemoteProtocolError("the stub cut the connection")


@dataclass
class Gateway:
    """The gateway: each request gets the replies given, in order, then a whole turn at once. It
    reads a body whole before it answers, as the gateway does, and keeps every body it read whole,
    with when and from whom."""

    clock: FakeClock
    replies: list[Reply] = field(default_factory=list)
    received: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    senders: set[str] = field(default_factory=set)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        self.received.append((self.clock.now(), body))
        self.senders.add(request.headers["authorization"])
        reply = self.replies.pop(0) if self.replies else Reply()
        return httpx.Response(reply.status, headers={"content-type": "text/event-stream"}, stream=Events(reply))


async def inline(action: Callable[[], Any]) -> Any:
    """Calls the stand-ins on the event loop: the clock moves only when the job waits on it."""
    return action()


@dataclass
class Rig:
    """A run of the job on the stand-ins, and the lines it wrote."""

    run: s7.Run
    scaler: Scaler
    gateway: Gateway
    clock: FakeClock
    out: list[str]

    def booked(self, step: Callable[[], Any]) -> None:
        """Books the job's session, then runs `step` of the job, as `Run.in_session` would."""

        async def both() -> None:
            assert await self.run.book()
            await step()

        asyncio.run(both())


def a_rig(
    stored: Any,
    *launches: tuple[float | None, ...],
    replies: list[Reply] | None = None,
    max_instances: int = 2,
    **scripted: Any,
) -> Rig:
    clock = FakeClock()
    scaler = Scaler(clock, stored, list(launches), **scripted)
    gateway = Gateway(clock, replies or [])
    out: list[str] = []
    run = s7.Run(
        s7.Pool("mock", scaler, scaler),
        "http://gateway.example.com",
        generate_key(),
        s7.Booking(scaler, "mock", max_instances, HOURS),
        MINUTES,
        60.0,
        s7.Recorder(clock, out.append),
        clock,
        httpx.MockTransport(gateway.handle),
        inline,
    )
    run.ends_at = int(T0) + MINUTES * 60
    return Rig(run, scaler, gateway, clock, out)


def parsed(out: list[str], *hidden: str) -> list[dict[str, Any]]:
    """Every line as one JSON object with a UTC time and a kind, none holding any of `hidden`."""
    lines = []
    for line in out:
        record = json.loads(line)
        assert isinstance(record, dict)
        assert UTC_TIME.fullmatch(record["at"])
        assert isinstance(record["kind"], str)
        for text in hidden:
            assert text not in line
        lines.append(record)
    return lines


def of(lines: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [line for line in lines if line["kind"] == kind]


def identifiers(rig: Rig) -> list[str]:
    return [machine.name for machine in rig.scaler.machines]


# The session.


def test_the_job_books_two_instances_before_its_steps_and_leaves_the_pool_with_no_session(stored: Any) -> None:
    rig = a_rig(stored, PROMPT, PROMPT, PROMPT, (200.0, 400.0))
    assert asyncio.run(rig.run.in_session())

    lines = parsed(rig.out, *identifiers(rig), rig.run.key)
    # The booking is written before anything is sent or read: two instances, under the job's name,
    # for its minutes.
    at, first = rig.scaler.written[0]
    assert (at, first.instances, first.set_by) == (T0, 2, "spike/s7_drain")
    assert (first.starts_at, first.ends_at) == (int(T0), int(T0) + MINUTES * 60)
    assert lines[0]["kind"] == "session"
    # Every line of the four steps, in a run in which no wait ran out.
    kinds = [line["kind"] for line in lines]
    assert not of(lines, "timeout")
    assert kinds.index("hold_started") < kinds.index("scale_in_asked") < kinds.index("starting_instance")
    assert [line["stream"] for line in of(lines, "stream")] == ["gateway_deployment", "release"]
    assert len(of(lines, "scale_in")) == 2
    # The cycle of the starting instance reads its own two instances, not those that left before.
    (starting,) = of(lines, "starting_instance")
    assert (starting["left"], starting["starting_when_lowered"], starting["instance"]) == (
        "starting",
        ["instance-4"],
        "instance-4",
    )
    assert kinds[-2:] == ["run_ended", "session"]
    # The job sent its two held streams and nothing else: no request woke the pool.
    assert len(rig.gateway.received) == 2
    assert rig.gateway.senders == {f"Bearer {rig.run.key}"}
    # It moved the pool by its session alone, and stopped the last one it held.
    actions = [(line["action"], line["written"]) for line in of(lines, "session")]
    assert actions == [(action, True) for action in ("booked", "lowered", "stopped", "booked", "lowered", "stopped")]
    left = session_of(stored)
    assert left is not None and left.ends_at <= rig.clock.now()


@pytest.mark.parametrize("pool", ["booked", "active", "one_instance"])
def test_the_job_does_not_start_on_a_pool_it_may_not_book(stored: Any, pool: str) -> None:
    theirs = None
    if pool != "one_instance":
        starts = int(T0) + (600 if pool == "booked" else -600)
        theirs = Session("mock", 1, starts, starts + 3600, "an-admin", int(T0) - 900)
        stored.put_item(Item=theirs.to_item())
    rig = a_rig(stored, max_instances=1 if pool == "one_instance" else 2)

    assert not asyncio.run(rig.run.in_session())
    (line,) = parsed(rig.out)
    assert line["kind"] == "refused"
    # Nothing was written, the session that was there is left as it is, and nothing was sent.
    assert rig.scaler.written == []
    assert session_of(stored) == theirs
    assert rig.gateway.received == []


def test_the_session_is_stopped_after_a_step_that_raised(stored: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = a_rig(stored, PROMPT, PROMPT)

    async def raising() -> None:
        raise RuntimeError("a step of the job failed")

    monkeypatch.setattr(rig.run, "departures", raising)
    with pytest.raises(RuntimeError, match="a step of the job failed"):
        asyncio.run(rig.run.in_session())

    # The session the job booked is ended in the table, by the job.
    ended = session_of(stored)
    assert ended is not None
    assert (ended.set_by, ended.instances) == ("spike/s7_drain", 2)
    assert ended.ends_at == ended.set_at <= rig.clock.now()
    assert [(line["action"], line["written"]) for line in of(parsed(rig.out), "session")] == [
        ("booked", True),
        ("stopped", True),
    ]


def test_a_booking_written_while_the_job_is_cancelled_is_stopped(stored: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = a_rig(stored, PROMPT, PROMPT)
    rig.run.offload = s7._off_loop
    book = rig.run.booking.book
    started, finish = threading.Event(), threading.Event()

    def slow_book(minutes: int, now: int) -> bool:
        # The write is on its way to the table as the job is cancelled, and reaches it after.
        started.set()
        assert finish.wait(5)
        return book(minutes, now)

    monkeypatch.setattr(rig.run.booking, "book", slow_book)

    async def cancelled() -> None:
        job = asyncio.create_task(rig.run.in_session())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            job.cancel()
            await asyncio.sleep(0)
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await job

    asyncio.run(cancelled())
    # The booking was written, and the job stopped it before it ended.
    assert [session.instances for _, session in rig.scaler.written] == [2, 2]
    ended = session_of(stored)
    assert ended is not None and ended.ends_at <= rig.clock.now()
    assert rig.gateway.received == []


# The departures, and the stream across a release.


def test_a_session_lowered_to_one_and_then_stopped_gives_two_departures_in_order(stored: Any) -> None:
    # The instance launched first is the first to leave; its collector is the second to write.
    rig = a_rig(stored, (40.0, 70.0), (15.0, 70.0))
    rig.booked(rig.run.departures)

    lines = parsed(rig.out, *identifiers(rig))
    # The job lowered its session to one, then stopped it, and the scaler followed each.
    assert [session.instances for _, session in rig.scaler.written] == [2, 1, 1]
    ended = session_of(stored)
    assert ended is not None and ended.ends_at <= rig.clock.now()
    lowered, stopped = [line for line in of(lines, "session") if line["action"] != "booked"]
    asked, out_of_service, gone = of(lines, "scale_in_asked"), of(lines, "out_of_service"), of(lines, "gone")
    assert [(line["decision"], line["capacity_asked"]) for line in asked] == [(1, 1), (2, 0)]
    assert lowered["at"] <= asked[0]["written_at"] < stopped["at"] <= asked[1]["written_at"]
    assert [(line["decision"], line["in_service"]) for line in out_of_service] == [(1, 1), (2, 0)]
    # An instance is named by the order the job first read it, never by its identifier.
    assert [line["writing"] for line in of(lines, "instances")] == [["instance-1", "instance-2"]]
    assert [(line["decision"], line["instance"]) for line in gone] == [(1, "instance-2"), (2, "instance-1")]

    # Two departures, in order, each with its three times: asked, out of service, gone.
    first, second = of(lines, "scale_in")
    assert [(line["decision"], line["instance"]) for line in (first, second)] == [(1, "instance-2"), (2, "instance-1")]
    for line, machine in zip((first, second), rig.scaler.machines, strict=True):
        assert line["asked_at"] <= line["asked_read_at"] <= line["out_of_service_read_at"] <= line["gone_read_at"]
        assert line["asked_at"] <= line["last_write_at"] == s7.utc(machine.last_write()) < line["gone_read_at"]
    assert first["gone_read_at"] <= stopped["at"] <= second["asked_at"]
    # No line tells a path or reads a warm count.
    assert not [name for line in lines for name in line if "path" in name or "warm" in name or name == "unknown"]

    # One stream, held open at the gateway from before the lowering, sent whole as the job read the
    # note that asked for fewer, so forwarded before the departing instance stopped, and answered.
    (stream,) = of(lines, "stream")
    assert (stream["stream"], stream["decision"], stream["ended"], stream["events"]) == ("release", 1, "closing_event", 6)
    assert stream["sent_at"] <= lowered["at"] <= asked[0]["written_at"] <= asked[0]["read_at"] <= stream["body_sent_at"]
    assert stream["body_sent_at"] < first["last_write_at"]
    assert stream["body_sent_at"] <= stream["first_event_at"] <= stream["last_event_at"]
    ((received, body),) = rig.gateway.received
    assert s7.utc(received) == stream["body_sent_at"]
    assert body["model"] == "mock"


@pytest.mark.parametrize(
    ("reply", "ended", "cause", "error"),
    [
        (Reply(TURN[:2] + ENGINE_ERROR), "error_event", "engine_error", None),
        (Reply(TURN[:1], cut=True), "connection_closed", None, "protocol_error"),
    ],
)
def test_the_stream_across_a_release_records_an_error_event_and_a_cut(
    stored: Any, reply: Reply, ended: str, cause: str | None, error: str | None
) -> None:
    rig = a_rig(stored, PROMPT, PROMPT, replies=[reply])
    rig.booked(rig.run.departures)

    lines = parsed(rig.out)
    (stream,) = of(lines, "stream")
    assert (stream["stream"], stream["ended"], stream["cause"], stream["error"]) == ("release", ended, cause, error)
    assert stream["events"] == len(reply.events)
    assert of(lines, "scale_in_asked")[0]["read_at"] <= stream["body_sent_at"] <= stream["last_event_at"]
    # The departures are recorded all the same.
    assert len(of(lines, "scale_in")) == 2


def test_the_stream_across_a_release_keeps_its_last_byte_when_no_note_asks_for_fewer(stored: Any) -> None:
    # The scaler does not release the instances of a lowered session: only the stop makes them leave.
    rig = a_rig(stored, PROMPT, PROMPT, follows_lowering=False)
    rig.booked(rig.run.departures)

    lines = parsed(rig.out)
    lowered, stopped = [line for line in of(lines, "session") if line["action"] != "booked"]
    (timeout,) = of(lines, "timeout")
    assert timeout["waiting_for"] == "scale_in_asked"
    assert lowered["at"] < timeout["at"] <= stopped["at"]
    # The wait ran out with the last byte unsent, and the note that asked for fewer after the stop
    # did not send it: the gateway never read the body whole.
    (stream,) = of(lines, "stream")
    assert (stream["stream"], stream["decision"], stream["ended"]) == ("release", None, "body_unsent")
    assert stream["sent_at"] <= lowered["at"]
    assert stream["body_sent_at"] is None and stream["status"] is None
    assert rig.gateway.received == []
    # Both instances left on the stop, on one note, each recorded.
    (asked,) = of(lines, "scale_in_asked")
    assert (asked["capacity_asked"], asked["fewer"]) == (0, 2)
    assert stopped["at"] <= asked["written_at"]
    assert [(line["decision"], line["instance"]) for line in of(lines, "scale_in")] == [
        (1, "instance-1"),
        (1, "instance-2"),
    ]


def test_the_session_is_lowered_only_once_the_stream_is_held_open_at_the_gateway(
    stored: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = a_rig(stored, PROMPT, PROMPT)
    lower = rig.run.lower
    order: list[str] = []

    class Connecting(httpx.AsyncBaseTransport):
        """A gateway whose connection takes a turn of the event loop to make, before any byte."""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0)
            assert isinstance(request.stream, httpx.AsyncByteStream)
            async for _ in request.stream:
                order.append("part")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Events(Reply()))

    async def lowering() -> bool:
        order.append("lowered")
        return await lower()

    rig.run.transport = Connecting()
    monkeypatch.setattr(rig.run, "lower", lowering)
    rig.booked(rig.run.departures)

    # The stream's first part had reached the gateway when the job lowered its session.
    assert order.index("part") < order.index("lowered")
    (stream,) = of(parsed(rig.out), "stream")
    assert (stream["stream"], stream["decision"], stream["ended"]) == ("release", 1, "closing_event")


def test_the_last_byte_leaves_as_the_release_is_set_not_at_the_next_padding() -> None:
    clock = FakeClock()
    gateway = Gateway(clock)

    async def released_while_waiting() -> s7.Read:
        release, waiting, never = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Waiting:
            """A clock on which the wait for the next byte of padding never ends."""

            now = clock.now

            async def sleep(self, seconds: float) -> None:
                waiting.set()
                await never.wait()

        stream = asyncio.create_task(
            s7.slow_stream(
                "http://gateway.example.com",
                "key",
                s7._request("mock"),
                clock=Waiting(),
                release=release,
                transport=httpx.MockTransport(gateway.handle),
            )
        )
        await asyncio.wait_for(waiting.wait(), 5)
        assert gateway.received == []
        release.set()
        return await asyncio.wait_for(stream, 5)

    read = asyncio.run(released_while_waiting())
    # The gateway read the body whole, with no byte of padding in between, and answered.
    assert len(gateway.received) == 1
    assert (read.ended, read.body_sent_at) == ("closing_event", clock.now())


@pytest.mark.parametrize("gap", ["collectors", "in_service"])
def test_a_gap_in_the_workspace_loses_no_observation_it_can_still_read(stored: Any, gap: str) -> None:
    rig = a_rig(stored, PROMPT, PROMPT)
    read = rig.scaler.read_series
    # The workspace answers nothing for a while: the collectors as the scaler first asks for fewer,
    # or the in-service gauge after it asked for none.
    missing, asked, seconds = {"collectors": ("max by (instance)", 1, 40), "in_service": ("rhi_scaler", 0, 170)}[gap]

    def gapped(query: str, **window: float) -> list[common.Series]:
        now_asked = rig.scaler.notes[-1].capacity_asked
        if missing in query and now_asked == asked and rig.clock.now() < rig.scaler.asked_first(asked) + seconds:
            unanswered.append(rig.clock.now())
            return []
        return read(query, **window)

    unanswered: list[float] = []
    rig.scaler.read_series = gapped
    rig.booked(rig.run.departures)
    assert unanswered

    lines = parsed(rig.out, *identifiers(rig))
    stops = [s7.utc(machine.last_write()) for machine in rig.scaler.machines]
    assert [line["last_write_at"] for line in of(lines, "gone")] == stops
    assert [line["in_service"] for line in of(lines, "out_of_service")] == [1, 0]
    departures = of(lines, "scale_in")
    assert [line["last_write_at"] for line in departures] == stops
    assert all(line["out_of_service_read_at"] and line["gone_read_at"] for line in departures)


@pytest.mark.parametrize(
    ("shown_until", "late_for"),
    [("the_lowering", 52), ("the_lowering", 95), ("the_decision", 150)],
    ids=["withdrawn_before_the_count_drops", "withdrawn_after_the_count_dropped", "a_stop_after_the_decision"],
)
def test_a_collector_batch_shown_late_withdraws_the_stop_it_seemed_to_be_and_gives_no_departure(
    stored: Any, shown_until: str, late_for: float
) -> None:
    rig = a_rig(stored, PROMPT, PROMPT)
    read = rig.scaler.read_series
    seemed: list[float] = []

    def delayed(query: str, **window: float) -> list[common.Series]:
        # For a while, until `late_for` seconds after the scaler first asked for fewer, the
        # workspace shows the writes of the instance that stays only up to the job's lowering, or
        # up to some seconds after that decision: it seems to have stopped, before the decision or
        # after it.
        lowered = [at for at, session in rig.scaler.written if session.instances == 1]
        asked = [note.written_at + 20.0 for note in rig.scaler.notes if note.capacity_asked == 1]
        hidden = (lowered if shown_until == "the_lowering" else asked)[:1]
        over = bool(asked) and rig.clock.now() >= asked[0] - 20 + late_for
        if query.startswith("max by (instance)") and hidden and rig.clock.now() >= hidden[0] and not over:
            staying = rig.scaler.machines[1]
            staying.gone_at = hidden[0]
            seemed[:] = [staying.last_write() or 0.0]
            try:
                return read(query, **window)
            finally:
                staying.gone_at = math.inf
        return read(query, **window)

    rig.scaler.read_series = delayed
    rig.booked(rig.run.departures)

    lines = parsed(rig.out, *identifiers(rig))
    (withdrawn,) = of(lines, "gone_withdrawn")
    assert withdrawn["last_write_at"] == s7.utc(seemed[0])
    # A stop from before the decision is none of its departures.
    (seeming,) = [line for line in of(lines, "gone") if line["last_write_at"] == withdrawn["last_write_at"]]
    assert seeming["decision"] == (None if shown_until == "the_lowering" else 1)
    # Each instance that left has its summary, once, and the one that stayed has none.
    departures = [(line["decision"], line["instance"], line["last_write_at"]) for line in of(lines, "scale_in")]
    assert departures == [
        (n + 1, rig.run.names(machine.name), s7.utc(machine.last_write())) for n, machine in enumerate(rig.scaler.machines)
    ]


def test_a_time_with_no_write_of_an_instance_that_goes_on_writing_is_no_departure(stored: Any) -> None:
    # The workspace never shows two minutes of the writes of the instance that stays, from the
    # scaler's decision on; the instance that leaves is held by the group for 200 seconds.
    rig = a_rig(stored, PROMPT, (20.0, 70.0), drain=200.0)
    read = rig.scaler.read_series

    def holed(query: str, **window: float) -> list[common.Series]:
        series = read(query, **window)
        if not query.startswith("max by (instance)") or not any(note.capacity_asked == 1 for note in rig.scaler.notes):
            return series
        asked, staying = rig.scaler.asked_first(1), rig.scaler.machines[1].name
        return [
            common.Series(one.labels, tuple((at, write) for at, write in one.samples if not asked < write <= asked + 120))
            if one.labels["instance"] == staying
            else one
            for one in series
        ]

    rig.scaler.read_series = holed
    rig.booked(rig.run.departures)

    lines = parsed(rig.out, *identifiers(rig))
    leaving, staying = rig.scaler.machines
    # The instance that stays seemed to have stopped, until its later writes were read.
    (withdrawn,) = of(lines, "gone_withdrawn")
    assert withdrawn["instance"] == rig.run.names(staying.name)
    # Each step has the instance that left on it, and the session was not stopped on the seeming
    # stop: the note still read an instance draining.
    first, second = of(lines, "scale_in")
    assert [(line["decision"], line["instance"], line["last_write_at"]) for line in (first, second)] == [
        (1, rig.run.names(leaving.name), s7.utc(leaving.last_write())),
        (2, rig.run.names(staying.name), s7.utc(staying.last_write())),
    ]
    (stopped,) = [line for line in of(lines, "session") if line["action"] == "stopped"]
    assert withdrawn["at"] < first["gone_read_at"] <= stopped["at"] <= second["asked_at"]


def unshown(rig: Rig, staying: int, since: float, until: float) -> None:
    """The workspace shows nothing the collector of the instance that stays, the one launched at
    place `staying` of the two, wrote from `since` to `until` seconds after the run began."""
    read = rig.scaler.read_series

    def gapped(query: str, **window: float) -> list[common.Series]:
        series = read(query, **window)
        if not query.startswith("max by (instance)") or len(rig.scaler.machines) != s7.INSTANCES:
            return series
        name = rig.scaler.machines[staying].name
        return [
            common.Series(one.labels, tuple((at, write) for at, write in one.samples if not since <= write - T0 <= until))
            if one.labels["instance"] == name
            else one
            for one in series
        ]

    rig.scaler.read_series = gapped


@pytest.mark.parametrize("shown_again", [True, False])
def test_the_session_is_not_stopped_on_the_silence_of_the_instance_that_stays(stored: Any, shown_again: bool) -> None:
    # As the scaler releases one instance, the workspace stops showing the writes of the other for
    # two minutes, or for good: the one that stays seems to stop first, and the note reads one
    # instance out of the group.
    rig = a_rig(stored, PROMPT, (20.0, 70.0))
    unshown(rig, 1, 160.0, 275.0 if shown_again else math.inf)
    rig.booked(rig.run.departures)

    lines = parsed(rig.out, *identifiers(rig))
    leaving, staying = rig.scaler.machines
    (stopped,) = [line for line in of(lines, "session") if line["action"] == "stopped"]
    seemed = next(line for line in of(lines, "gone") if line["instance"] == rig.run.names(staying.name))
    assert seemed["decision"] == 1 and seemed["at"] < stopped["at"]
    if not shown_again:
        # The departure was never told from the silence: the wait ran out, and the line says so.
        timeout = of(lines, "timeout")[0]
        assert timeout["waiting_for"] == "departure_confirmed"
        assert seemed["at"] < timeout["at"] <= stopped["at"]
        return
    # The session was stopped once the instance that left was read gone and the other writing, and
    # each departure names its instance.
    assert not of(lines, "timeout")
    assert [line["instance"] for line in of(lines, "gone_withdrawn")] == [rig.run.names(staying.name)]
    first, second = of(lines, "scale_in")
    assert [(line["decision"], line["instance"], line["last_write_at"]) for line in (first, second)] == [
        (1, rig.run.names(leaving.name), s7.utc(leaving.last_write())),
        (2, rig.run.names(staying.name), s7.utc(staying.last_write())),
    ]
    assert first["gone_read_at"] <= stopped["at"] <= second["asked_at"]


def test_a_reading_that_shows_the_drops_of_two_decisions_gives_each_its_line(stored: Any) -> None:
    rig = a_rig(stored, PROMPT, PROMPT)
    read = rig.scaler.read_series

    def gapped(query: str, **window: float) -> list[common.Series]:
        # The in-service gauge is not answered from the job's lowering until the pool reads none.
        if s7.INSTANCES_IN_SERVICE in query and len(rig.scaler.written) >= 2 and rig.scaler.notes[-1].in_service > 0:
            return []
        return read(query, **window)

    rig.scaler.read_series = gapped
    rig.booked(rig.run.departures)

    lines = parsed(rig.out)
    assert [(line["decision"], line["capacity_asked"]) for line in of(lines, "scale_in_asked")] == [(1, 1), (2, 0)]
    # One reading, none in service, is the drop of both decisions: a line for each, at the same times.
    first, second = of(lines, "out_of_service")
    assert [(line["decision"], line["fewer"], line["in_service"]) for line in (first, second)] == [(1, 1, 0), (2, 1, 0)]
    assert (first["reading_at"], first["read_at"]) == (second["reading_at"], second["read_at"])
    departures = of(lines, "scale_in")
    assert [line["decision"] for line in departures] == [1, 2]
    assert [line["out_of_service_read_at"] for line in departures] == [first["read_at"], second["read_at"]]


def test_a_session_that_ends_before_the_scaler_acts_on_the_lowering_does_not_send_the_last_byte(stored: Any) -> None:
    # The job's minutes end between the lowering and the scaler's next run: the note goes from two
    # instances to none, and never asks for the one the job lowered to.
    rig = a_rig(stored, PROMPT, PROMPT)
    rig.run.run_minutes = 3
    assert asyncio.run(rig.run.in_session())

    lines = parsed(rig.out)
    lowered = next(line for line in of(lines, "session") if line["action"] == "lowered")
    (asked,) = of(lines, "scale_in_asked")
    assert lowered["written"] and lowered["at"] < asked["written_at"]
    assert (asked["capacity_asked"], asked["fewer"]) == (0, 2)
    # The stream across a release was given up with its body unsent: the gateway read whole only
    # the stream across the deployment.
    release = next(line for line in of(lines, "stream") if line["stream"] == "release")
    assert (release["decision"], release["body_sent_at"], release["ended"]) == (None, None, "body_unsent")
    assert len(rig.gateway.received) == 1
    # Both departures are recorded all the same, on the decision that asked for none.
    assert [line["decision"] for line in of(lines, "scale_in")] == [1, 1]


# The starting instance.


@pytest.mark.parametrize(
    ("left", "slow", "by_launches"),
    [("starting", (200.0, 400.0), True), ("in_service", (100.0, 400.0), True), ("in_service", (330.0, 400.0, 300.0), False)],
    ids=["the_starting_one", "the_one_in_service", "the_one_in_service_told_by_the_notes_alone"],
)
def test_the_starting_instance(stored: Any, left: str, slow: tuple[float, ...], by_launches: bool) -> None:
    # One instance is in service a minute after its launch; the other is still starting then, and
    # its collector writes only after the note read the state, its hook not completed; has written
    # by then; or writes only once its hook is completed, so that no scrape reads it starting. The
    # group holds the instance it released for a hundred seconds more.
    rig = a_rig(stored, PROMPT, slow, leaves=s7.Left(left), drain=100.0)
    asyncio.run(rig.run.starting_instance())

    lines = parsed(rig.out, *identifiers(rig))
    (line,) = of(lines, "starting_instance")
    assert not of(lines, "timeout")
    # From a pool at zero, a session of two, lowered to one at the first note that read one instance
    # in service and one starting, and not before: the note before it read none in service.
    (booked_at, booked), (lowered_at, lowered) = rig.scaler.written
    assert (booked.instances, lowered.instances) == (2, 1)
    state = next(note for note in rig.scaler.notes if (note.in_service, note.starting) == (1, 1))
    assert booked_at < state.written_at <= lowered_at < state.written_at + 2 * s7.NOTE_POLL_SECONDS
    assert line["state_written_at"] == s7.utc(state.written_at) <= line["state_read_at"] <= line["lowered_at"]
    assert line["lowered_at"] == s7.utc(lowered_at)

    # Which of the two left, and when: the note that first read it gone, and the one that read it
    # out of the group.
    assert (line["left"], line["reason"]) == (left, None)
    after = [note for note in rig.scaler.notes if note.written_at > lowered_at and note.in_service + note.starting < 2]
    assert (line["in_service_after"], line["starting_after"]) == ((1, 0) if left == "starting" else (0, 1))
    assert line["lowered_at"] < line["left_written_at"] == s7.utc(after[0].written_at) <= line["left_read_at"]
    out_of_the_group = next(note for note in after if note.draining == 0)
    assert line["left_written_at"] < line["gone_written_at"] == s7.utc(out_of_the_group.written_at) <= line["at"]
    # The notes tell it when the other is starting still; the launches when the starting one's
    # agent was read, once the job had lowered, with its hook not completed.
    assert line["left_by_notes"] == (left if left == "in_service" else None)
    assert line["left_by_launches"] == (left if by_launches else None)
    assert line["starting_when_lowered"] == (["instance-2"] if by_launches else [])
    # The instance that left is named, with its collector's last write.
    serving, starting = rig.scaler.machines
    gone = starting if left == "starting" else serving
    assert (line["instance"], line["last_write_at"]) == (rig.run.names(gone.name), s7.utc(gone.last_write()))
    assert line["lowered_at"] < line["last_write_at"] < line["at"]
    # The starting one left with its hook never completed; the one in service had completed its own.
    if left == "starting":
        assert line["hook_completed_at"] is None
    else:
        assert s7.utc(gone.hooked_at) <= line["hook_completed_at"] < line["state_written_at"]


@pytest.mark.parametrize("shown_again", [True, False])
def test_the_silence_of_the_instance_that_stays_is_not_taken_for_the_one_that_left(
    stored: Any, shown_again: bool
) -> None:
    # The starting instance leaves, and the workspace stops showing the writes of the one in
    # service for two minutes, or for good: it seems to stop first, as a note reads one instance
    # out of the group.
    rig = a_rig(stored, PROMPT, (20.0, 400.0))
    unshown(rig, 0, 208.0, 328.0 if shown_again else math.inf)
    asyncio.run(rig.run.starting_instance())

    lines = parsed(rig.out, *identifiers(rig))
    (line,) = of(lines, "starting_instance")
    serving, starting = rig.scaler.machines
    assert serving.gone_at == math.inf and starting.gone_at < math.inf
    assert line["gone_written_at"] is not None
    if shown_again:
        # The job read on until the instance in service was read writing again, then named the other.
        assert not of(lines, "timeout")
        assert (line["left"], line["instance"]) == ("starting", rig.run.names(starting.name))
        assert line["last_write_at"] == s7.utc(starting.last_write())
    else:
        # The wait ran out with the instance in service never read writing again: no instance is named.
        assert [timeout["waiting_for"] for timeout in of(lines, "timeout")] == ["starting_instance_gone"]
        assert (line["left"], line["reason"], line["instance"]) == (None, "role_not_told", None)


def test_the_role_of_an_instance_is_not_told_by_which_collector_wrote_first(stored: Any) -> None:
    # The collector of the instance in service starts late, and the instance leaves before it ever
    # writes; the starting one's wrote from the first seconds, and it enters service meanwhile.
    rig = a_rig(stored, (220.0, 65.0), (15.0, 195.0), leaves=s7.Left.IN_SERVICE)
    asyncio.run(rig.run.starting_instance())

    (line,) = of(parsed(rig.out, *identifiers(rig)), "starting_instance")
    serving, starting = rig.scaler.machines
    assert serving.last_write() is None and starting.gone_at == math.inf
    # The notes do not tell: one in service and none starting. The instance that writes still was
    # read with its hook not completed after the lowering: it was the starting one, so the one in
    # service left, with no write to name it by.
    assert (line["in_service_after"], line["starting_after"], line["left_by_notes"]) == (1, 0, None)
    assert line["starting_when_lowered"] == [rig.run.names(starting.name)]
    assert (line["left"], line["left_by_launches"], line["reason"]) == ("in_service", "in_service", None)
    assert (line["instance"], line["last_write_at"]) == (None, None)
    assert line["lowered_at"] < line["left_written_at"] <= line["gone_written_at"]


@pytest.mark.parametrize(
    ("launch", "leaves"),
    [((None, 400.0), "starting"), ((100.0, 400.0, 90.0), "starting"), ((100.0, 195.0, 90.0), "in_service")],
    ids=["the_starting_one_never_wrote", "the_starting_one_had_completed_its_hook", "the_one_in_service_left"],
)
def test_the_line_of_the_starting_instance_tells_no_role_the_readings_do_not_tell(
    stored: Any, launch: tuple[float | None, ...], leaves: str
) -> None:
    # In each run the note after the lowering reads one instance in service and none starting: the
    # starting one left, or it entered service between two notes and the one in service left. The
    # notes read the same, and no agent was read with its hook not completed once the job had
    # lowered: the starting one's collector never wrote, or its hook was completed before, the
    # instance not yet in service.
    rig = a_rig(stored, PROMPT, launch, leaves=s7.Left(leaves))
    asyncio.run(rig.run.starting_instance())

    (line,) = of(parsed(rig.out, *identifiers(rig)), "starting_instance")
    assert (line["in_service_after"], line["starting_after"], line["left_by_notes"]) == (1, 0, None)
    assert (line["left"], line["reason"], line["starting_when_lowered"]) == (None, "role_not_told", [])
    # Whatever the role, the line holds the times, and names the instance that left if it wrote.
    serving, starting = rig.scaler.machines
    gone = starting if leaves == "starting" else serving
    last = gone.last_write()
    assert (line["instance"], line["last_write_at"]) == (None if last is None else rig.run.names(gone.name), s7.utc(last))
    assert line["lowered_at"] < line["left_written_at"] <= line["gone_written_at"]


def test_the_line_of_the_starting_instance_says_why_it_tells_neither(stored: Any) -> None:
    # Both instances enter service between two notes: none reads one in service and one starting.
    rig = a_rig(stored, PROMPT, PROMPT)
    asyncio.run(rig.run.starting_instance())
    (line,) = of(parsed(rig.out), "starting_instance")
    assert (line["left"], line["reason"], line["lowered_at"]) == (None, "state_not_read", None)
    assert [session.instances for _, session in rig.scaler.written] == [2]

    # The notes and the launches disagree: neither is believed. A single reading stands alone.
    starting, in_service = s7.Left.STARTING, s7.Left.IN_SERVICE
    assert s7.which_left((in_service, None), starting) == (None, s7.Untold.READINGS_DISAGREE)
    assert s7.which_left((None, s7.Untold.ROLE_NOT_TOLD), starting) == (starting, None)
    assert s7.which_left((None, s7.Untold.NOTHING_LEFT), None) == (None, s7.Untold.NOTHING_LEFT)

    def notes(*counts: tuple[int, int]) -> list[ScalerNote]:
        return [ScalerNote("mock", 1, None, 60 * n, in_service=one, starting=two) for n, (one, two) in enumerate(counts)]

    # The starting one entered service before one left: the notes do not say which of the two.
    assert s7.left_by_notes(notes((1, 1), (2, 0), (1, 0))) == (None, s7.Untold.BOTH_IN_SERVICE)
    assert s7.left_by_notes(notes((1, 1), (1, 1))) == (None, s7.Untold.NOTHING_LEFT)
    assert s7.left_by_notes(notes((1, 1), (0, 0))) == (None, s7.Untold.BOTH_LEFT)

    # The launches tell nothing when both instances were read still starting, when both stopped, or
    # when none stopped and the other instance wrote too: it is there still, by what was read.
    assert s7.left_by_launches({"a", "b"}, {"a", "b"}, ["a"], waited=True) is None
    assert s7.left_by_launches({"a"}, {"a", "b"}, ["a", "b"], waited=True) is None
    assert s7.left_by_launches({"a"}, {"a", "b"}, [], waited=True) is None
    assert s7.left_by_launches({"a"}, {"a"}, [], waited=False) is None


# The streams, the errors and the job's start.


def test_a_stream_the_gateway_cuts_is_recorded_as_ended_by_the_connection_closing_with_its_events_counted(
    stored: Any,
) -> None:
    rig = a_rig(stored, replies=[Reply(TURN[:3], cut=True)])
    asyncio.run(rig.run.across_deployment())

    hold, stream = parsed(rig.out)
    assert (hold["kind"], hold["stream"], hold["seconds"]) == ("hold_started", "gateway_deployment", 60.0)
    assert (stream["kind"], stream["stream"]) == ("stream", "gateway_deployment")
    assert (stream["ended"], stream["events"], stream["error"]) == ("connection_closed", 3, "protocol_error")
    assert stream["last_event_at"] is not None
    # The body was held open for the time given, then sent whole: the gateway read it as JSON.
    assert stream["body_sent_at"] == hold["until"]
    assert rig.gateway.received[0][1]["model"] == "mock"


@pytest.mark.parametrize(
    ("reply", "ended", "events"),
    [
        (Reply(), "closing_event", 6),
        (Reply(TURN[:2] + ENGINE_ERROR), "error_event", 3),
        (Reply(TURN[:2]), "connection_closed", 2),
        (Reply([], status=429), "http_status", 0),
    ],
)
def test_a_stream_records_how_it_ended(reply: Reply, ended: str, events: int) -> None:
    clock = FakeClock()
    transport = httpx.MockTransport(Gateway(clock, [reply]).handle)
    request = s7._request("mock")
    read = asyncio.run(s7.send("http://gateway.example.com", "key", request, clock=clock, transport=transport))
    assert (read.ended, read.events, read.status) == (ended, events, reply.status)


def test_a_failing_read_of_the_pool_is_recorded_by_its_kind_without_its_text(stored: Any) -> None:
    rig = a_rig(stored, failing=True)
    assert asyncio.run(rig.run.note()) is None
    (line,) = parsed(rig.out, AWS_TEXT, "ThrottlingException")
    assert (line["kind"], line["source"], line["error"]) == ("read_failed", "pool_state", "aws_error")


ENVIRON = {
    s7.ENDPOINT_VARIABLE: "gateway.example.com",
    s7.WORKSPACE_URL_VARIABLE: "https://workspace.example.com",
    s7.POOL_STATE_TABLE_VARIABLE: "pool-state",
    s7.DEPLOYMENT_VARIABLE: "1",
    common.RUN_MINUTES_VARIABLE: "90",
    "AWS_REGION": "us-east-1",
}


def test_the_job_ends_with_a_line_and_no_traceback_when_aws_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused() -> config.Config:
        raise botocore.exceptions.ClientError({"Error": {"Code": "AccessDenied", "Message": AWS_TEXT}}, "GetParameters")

    monkeypatch.setattr(s7.config, "read_ssm", refused)
    assert s7.main(ENVIRON, FakeClock()) == 1
    captured = capsys.readouterr()
    (line,) = parsed(captured.out.splitlines(), AWS_TEXT, "AccessDenied")
    assert (line["kind"], line["step"], line["error"]) == ("failed", "configuration", "aws_error")
    assert captured.err == ""


def test_the_job_does_not_start_without_its_minutes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    read: list[str] = []
    monkeypatch.setattr(s7.config, "read_ssm", lambda: read.append("configuration"))
    monkeypatch.setattr(s7.boto3, "Session", lambda: read.append("clients"))
    environ = {name: value for name, value in ENVIRON.items() if name != common.RUN_MINUTES_VARIABLE}
    assert s7.main(environ, FakeClock()) == 1
    (line,) = parsed(capsys.readouterr().out.splitlines())
    assert (line["kind"], line["step"]) == ("failed", "configuration")
    assert read == []


def test_the_job_reads_of_the_pool_only_what_its_session_needs_and_reaches_the_gateway_over_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pool's configuration, as far as the job reads it: neither a hold nor an idle time.
    pool = SimpleNamespace(name="mock", engine="mock", max_instances=2)
    session = SimpleNamespace(get_credentials=lambda: None, resource=lambda _: SimpleNamespace(Table=lambda _: None))
    monkeypatch.setattr(s7.boto3, "Session", lambda: session)
    monkeypatch.setattr(s7.config, "read_ssm", lambda: SimpleNamespace(pools=[pool], session_max_hours=HOURS))
    monkeypatch.setattr(s7.common, "canary_keys", lambda: SimpleNamespace(students=[generate_key()]))
    reached: list[httpx.URL] = []
    given: list[tuple[int, int, int]] = []

    async def deployment(run: s7.Run) -> bool:
        given.append((run.run_minutes, run.booking.max_instances, run.booking.session_max_hours))
        run.transport = httpx.MockTransport(lambda request: reached.append(request.url) or httpx.Response(200))
        await run.across_deployment()
        return True

    monkeypatch.setattr(s7.Run, "in_session", deployment)
    assert s7.main(ENVIRON, FakeClock()) == 0
    assert given == [(90, 2, HOURS)]
    assert [(url.scheme, url.host, url.path) for url in reached] == [("https", "gateway.example.com", "/v1/messages")]
