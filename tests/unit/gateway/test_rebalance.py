"""For each new rebalance number of a pool, the cookie of a share of its students is dropped
(`rhi_gateway.affinity`, `rhi_gateway.budget`; section 5.5).

A gateway instance, here, is one task's part in a request: its own reader of the pools
(`rhi_gateway.readiness`), over the pool-state table the scaler writes its note in, and the three
steps `rhi_gateway.app` takes for a request, over the students' records, which the instances share:
the reservation, the judgement, the settlement. The load balancer is a double that sets a new
cookie on each answer, as duration-based stickiness does. The last test runs the whole gateway in
front of it, to show the application takes those steps. The students' keys, where a test needs
one, are generated when it runs.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import DEFAULT_REBALANCE_SHARE, parse_pools
from rhi_common.records import ScalerNote
from rhi_common.students import StudentKey, UsageDay, utc_day
from rhi_gateway import proxy, routes
from rhi_gateway.affinity import COOKIE_NAME, Affinity, selects
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger, Reservation
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import Readiness

NOW = 1_800_000_000
# The course end the ledger's rows expire by.
COURSE_END_DATE = datetime.date(2027, 6, 30)
MOCK = {
    "engine": "mock",
    "instance_types": ["t3.small"],
    "prices": {"t3.small": 0.0208},
    "max_instances": 2,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 8,
    "context_length": 131072,
    "largest_output": 8000,
}
# `coursework` has the default share. `everyone` moves every student at each number, so whatever
# is judged there is dropped.
POOLS = {pool.name: pool for pool in parse_pools({"coursework": MOCK, "everyone": {**MOCK, "rebalance_share": 1.0}})}


def student(github_id: int, key: str | None = None) -> StudentKey:
    """A student of the class. Only the whole gateway reads a key, so only its students are given one."""
    return StudentKey(
        key_hash=hashlib.sha256(b"not a key").hexdigest() if key is None else key_hash(key),
        github_id=github_id,
        alias=f"student-{github_id}",
        installation=None,
        issued_at=NOW - 3600,
        expires_at=NOW + 90 * 86400,
        revoked_at=None,
        concurrency_limit=4,
    )


class Scaler:
    """The pool-state table, and the scaler's note in it: `get_item`, the one call the reader makes."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def raises(self, number: int, pool: str = "coursework") -> None:
        """Write the pool's note with this rebalance number."""
        note = ScalerNote(pool, capacity_asked=2, no_capacity_at=None, written_at=NOW, rebalance_number=number)
        self.items[pool, "note"] = dict(note.to_item())

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
        item = self.items.get((Key["pool"], Key["record"]))
        return {} if item is None else {"Item": item}


class Healthy:
    """The load-balancing API: every target group has a healthy target."""

    def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}]}


class LoadBalancer:
    """The internal load balancer: each answer sets a cookie no answer set before, unless `sets` is false."""

    def __init__(self) -> None:
        self.issued = 0
        self.sets = True
        self.carried: list[str | None] = []  # The `Cookie` header of each request the whole gateway sent.

    def set_cookie(self) -> list[str]:
        """The `Set-Cookie` headers of one answer."""
        if not self.sets:
            return []
        self.issued += 1
        return [f"{COOKIE_NAME}=target+{self.issued}/opaque==; Path=/"]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.carried.append(request.headers.get("cookie"))
        headers = [("content-type", "application/json"), *(("set-cookie", value) for value in self.set_cookie())]
        return httpx.Response(200, headers=headers, json={"usage": {"input_tokens": 21, "output_tokens": 7}})


@dataclass
class InFlight:
    """A request an instance has reserved for and judged, and not yet settled."""

    reservation: Reservation
    affinity: Affinity

    @property
    def carried(self) -> str | None:
        """The cookie the request carries to the load balancer, `None` when it carries none."""
        return self.affinity.headers().get("Cookie", "").removeprefix(f"{COOKIE_NAME}=") or None


class Instance:
    """One gateway task: its own reader of the pools, and the students' records."""

    def __init__(self, scaler: Scaler, ledger: InMemoryLedger, balancer: LoadBalancer) -> None:
        groups = {name: f"group-{name}" for name in POOLS}
        self.readiness = Readiness(groups, elbv2=Healthy(), table=scaler, clock=lambda: float(NOW))
        self.ledger = ledger
        self.balancer = balancer

    def receives(self, who: StudentKey, pool: str = "coursework") -> InFlight:
        """The reservation of a request, and its judgement on the number this instance holds."""
        reservation = self.ledger.reserve(
            who,
            uuid.uuid4().hex,
            hold_seconds=POOLS[pool].hold_seconds,
            longest_request_seconds=1800,
            pool=pool,
        )
        affinity = Affinity.judged(
            who.github_id,
            reservation.cookie,
            applied=reservation.rebalance_applied,
            number=self.readiness.pool(pool).rebalance_number,
            share=POOLS[pool].rebalance_share,
        )
        return InFlight(reservation, affinity)

    def ends(self, request: InFlight) -> None:
        """The load balancer's answer, and the settlement."""
        request.affinity.answered(self.balancer.set_cookie())
        self.ledger.settle(
            request.reservation,
            cookie=request.affinity.ended,
            rebalance_number=request.affinity.number,
            cookie_dropped=request.affinity.dropped,
        )

    def serves(self, who: StudentKey, pool: str = "coursework") -> str | None:
        """One whole request: the cookie it carried to the load balancer, `None` when it carried none."""
        request = self.receives(who, pool)
        self.ends(request)
        return request.carried


class Gateways:
    """Two gateway instances over one scaler, one load balancer and the same records, and a class of students.

    Every student has sent one request while the pool's number was `number`, so each holds a
    cookie and a record that has applied that number.
    """

    def __init__(self, students: int, *, number: int = 3, pool: str = "coursework") -> None:
        self.scaler = Scaler()
        self.ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=lambda: float(NOW))
        self.balancer = LoadBalancer()
        self.instances = [Instance(self.scaler, self.ledger, self.balancer) for _ in range(2)]
        self.students = [student(github_id) for github_id in range(1, students + 1)]
        self.pool = pool
        self.raise_to(number)
        assert [self.instances[0].serves(who, pool) for who in self.students] == [None] * students

    def raise_to(self, number: int) -> None:
        """The scaler raises the pool's number, and every instance reads it on its next cycle."""
        self.scaler.raises(number, self.pool)
        for instance in self.instances:
            instance.readiness.refresh()

    def record(self, who: StudentKey) -> UsageDay:
        day = self.ledger.day(who.github_id, utc_day(NOW))
        assert day is not None
        return day

    def cookie(self, who: StudentKey) -> str | None:
        """The cookie the student's record holds for the pool."""
        return self.record(who).affinity_cookies.get(self.pool)


def test_one_new_number_drops_the_cookie_of_one_student_in_ten() -> None:
    assert POOLS["coursework"].rebalance_share == DEFAULT_REBALANCE_SHARE
    gateways = Gateways(10_000)
    instance = gateways.instances[0]
    held = {who.github_id: gateways.cookie(who) for who in gateways.students}
    assert None not in held.values()

    gateways.raise_to(4)
    carried = {who.github_id: instance.serves(who) for who in gateways.students}
    dropped = {github_id for github_id, cookie in carried.items() if cookie is None}
    assert 900 <= len(dropped) <= 1100
    # Every other student's request carried the cookie their record held.
    assert all(carried[github_id] == held[github_id] for github_id in held if github_id not in dropped)

    # The same number drops none on their next request: each carries the cookie the record now
    # holds, which for a student moved is the one the instance they were sent to has set.
    held = {who.github_id: gateways.cookie(who) for who in gateways.students}
    assert None not in held.values()
    assert {who.github_id: instance.serves(who) for who in gateways.students} == held


def test_the_choice_is_shared() -> None:
    gateways = Gateways(2000)
    first, second = gateways.instances

    def dropped_by_both(number: int) -> set[int]:
        """Raise the pool's number, then send two requests at once for each student, one to each instance."""
        gateways.raise_to(number)
        dropped = set()
        for who in gateways.students:
            requests = first.receives(who), second.receives(who)
            # The same choice, for the same student and number, on both instances.
            assert (requests[0].carried is None) == (requests[1].carried is None)
            if requests[0].carried is None:
                dropped.add(who.github_id)
            first.ends(requests[0])
            second.ends(requests[1])
        return dropped

    fourth, fifth = dropped_by_both(4), dropped_by_both(5)
    share = POOLS["coursework"].rebalance_share
    assert fourth == {who.github_id for who in gateways.students if selects(who.github_id, 4, share)}
    # Each number selects its own students: about a share of the class, and not the same ones.
    assert 150 <= len(fourth) <= 250
    assert 150 <= len(fifth) <= 250
    assert len(fourth & fifth) < 50


def test_an_unchanged_number_keeps_the_cookie() -> None:
    # In this pool every student a number is new to is dropped: the first new number drops them all.
    gateways = Gateways(200, pool="everyone")
    first, second = gateways.instances
    gateways.raise_to(4)
    assert [first.serves(who, "everyone") for who in gateways.students] == [None] * 200

    # While the pool's number equals the one their record has applied, every request of every
    # student carries the record's cookie, on either instance and however often the note is read.
    for instance in (first, second, first):
        gateways.raise_to(4)
        for who in gateways.students:
            assert gateways.record(who).rebalance_applied == {"everyone": 4}
            held = gateways.cookie(who)
            assert held is not None
            assert instance.serves(who, "everyone") == held


def test_a_student_away_while_the_number_rose_is_judged_once_on_the_last() -> None:
    gateways = Gateways(2000)
    share = POOLS["coursework"].rebalance_share
    for number in (4, 5, 6):
        gateways.raise_to(number)

    carried = {who.github_id: gateways.instances[0].serves(who) for who in gateways.students}
    dropped = {github_id for github_id, cookie in carried.items() if cookie is None}
    assert dropped == {who.github_id for who in gateways.students if selects(who.github_id, 6, share)}
    # The numbers in between act on no one: a student one of them selects, and the last does not, keeps their cookie.
    between = {who.github_id for who in gateways.students if selects(who.github_id, 4, share)}
    assert between - dropped
    assert all(gateways.record(who).rebalance_applied == {"coursework": 6} for who in gateways.students)


def test_the_record_keeps_the_number_a_request_was_judged_on() -> None:
    gateways = Gateways(300)
    instance = gateways.instances[0]
    share = POOLS["coursework"].rebalance_share
    assert all(gateways.record(who).rebalance_applied == {"coursework": 3} for who in gateways.students)

    # Whether or not the cookie was dropped, the settlement stores the number, with the cookie the request ended with.
    gateways.raise_to(4)
    before = gateways.balancer.issued
    for who in gateways.students:
        instance.serves(who)
        assert gateways.record(who).rebalance_applied == {"coursework": 4}
        assert gateways.cookie(who) == f"target+{gateways.balancer.issued}/opaque=="
    assert gateways.balancer.issued == before + 300

    # Another pool's number is another pool's: it moves no student of this one, and is kept beside this one's.
    gateways.scaler.raises(9, "everyone")
    instance.readiness.refresh()
    one = gateways.students[0]
    held = gateways.cookie(one)
    assert instance.serves(one) == held
    assert instance.serves(one, "everyone") is None
    assert gateways.record(one).rebalance_applied == {"coursework": 4, "everyone": 9}

    # A request whose cookie is dropped and that ends with none leaves none on the record: the
    # number has acted, and the student's next request carries no cookie.
    gateways.raise_to(5)
    gateways.balancer.sets = False
    moved = next(who for who in gateways.students if selects(who.github_id, 5, share))
    kept = next(who for who in gateways.students if not selects(who.github_id, 5, share))
    held = gateways.cookie(kept)
    assert (instance.serves(moved), instance.serves(kept)) == (None, held)
    assert (gateways.cookie(moved), gateways.cookie(kept)) == (None, held)
    assert instance.serves(moved) is None
    assert gateways.record(moved).rebalance_applied["coursework"] == 5

    # An instance that holds no note for the pool knows no number: it judges no request, and
    # leaves the record's number as it is.
    del gateways.scaler.items["coursework", "note"]
    unread = Instance(gateways.scaler, gateways.ledger, gateways.balancer)
    assert unread.readiness.pool("coursework").rebalance_number is None
    assert unread.serves(kept) == held
    assert gateways.record(kept).rebalance_applied["coursework"] == 5


def test_the_gateway_judges_each_request_on_the_number_its_reader_holds() -> None:
    share = POOLS["coursework"].rebalance_share
    # One student whom the numbers 1 and 2 both select, and one whom neither does.
    chosen = {github_id: (selects(github_id, 1, share), selects(github_id, 2, share)) for github_id in range(1, 5000)}
    moved = next(github_id for github_id, by in chosen.items() if by == (True, True))
    kept = next(github_id for github_id, by in chosen.items() if by == (False, False))
    keys = {moved: generate_key(), kept: generate_key()}
    scaler, balancer = Scaler(), LoadBalancer()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=lambda: float(NOW))
    groups = {name: f"group-{name}" for name in POOLS}
    readiness = Readiness(groups, elbv2=Healthy(), table=scaler, clock=lambda: float(NOW))
    pools = tuple(POOLS.values())
    forwarding = Forwarding(
        authenticator=Authenticator(
            InMemoryKeyStore(*(student(github_id, key) for github_id, key in keys.items())),
            wall_clock=lambda: float(NOW),
        ),
        ledger=ledger,
        filters={
            routes.MESSAGES: BodyFilter(messages.MESSAGES, pools),
            routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, pools),
            routes.CHAT_COMPLETIONS: ChatBodyFilter(pools),
            routes.RESPONSES: BodyFilter(responses.RESPONSES, pools),
        },
        proxy=proxy.Proxy(
            proxy.upstream_client(httpx.MockTransport(balancer)),
            "http://pools.example.com:8080",
            {name: f"{name}-secret" for name in POOLS},
        ),
        holding=Holding(views=readiness.views, wake=lambda pool: None),
    )
    app = build_app(forwarding_handlers(forwarding))

    def record(github_id: int) -> UsageDay:
        day = ledger.day(github_id, utc_day(NOW))
        assert day is not None
        return day

    def applied(github_id: int) -> dict[str, int]:
        return record(github_id).rebalance_applied

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:

            async def ask(github_id: int) -> None:
                body = {"model": "coursework", "max_tokens": 100, "messages": []}
                answer = await client.post(routes.MESSAGES.path, json=body, headers={"x-api-key": keys[github_id]})
                assert answer.status_code == 200

            def cookie(issued: int) -> str:
                return f"{COOKIE_NAME}=target+{issued}/opaque=="

            # The scaler has raised no number yet: each student's first answer sets a cookie, and
            # the record has applied the note's number, 0.
            scaler.raises(0)
            readiness.refresh()
            await ask(moved)
            await ask(kept)
            assert balancer.carried == [None, None]
            assert (applied(moved), applied(kept)) == ({"coursework": 0}, {"coursework": 0})

            # A number the reader has not read yet moves no one.
            scaler.raises(1)
            await ask(moved)
            assert balancer.carried[2:] == [cookie(1)]

            # Once read, it drops the cookie of the student it selects, and of no other; and once.
            readiness.refresh()
            await ask(moved)
            await ask(kept)
            assert balancer.carried[3:] == [None, cookie(2)]
            assert (applied(moved), applied(kept)) == ({"coursework": 1}, {"coursework": 1})
            await ask(moved)
            await ask(kept)
            assert balancer.carried[5:] == [cookie(4), cookie(5)]

            # A request whose cookie the next number drops, and whose answer sets none, leaves
            # none on the record.
            scaler.raises(2)
            readiness.refresh()
            balancer.sets = False
            await ask(moved)
            await ask(kept)
            assert balancer.carried[7:] == [None, cookie(7)]
            assert (record(moved).affinity_cookies, applied(moved)) == ({}, {"coursework": 2})
            assert (record(kept).affinity_cookies, applied(kept)) == ({"coursework": "target+7/opaque=="}, {"coursework": 2})

    asyncio.run(scenario())
