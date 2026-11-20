"""A student's load-balancer cookie, from one request to the next (`rhi_gateway.affinity`; section 5.5).

The gateway runs whole, over the in-memory key store and the in-memory ledger. The upstream is an
`httpx.MockTransport` handler written here, which plays the internal load balancer: it notes the
pool and the `Cookie` header of each request, and sets a new `AWSALB` cookie on each answer, as
duration-based stickiness does (evidence E13). The students' keys are generated when the tests run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.students import StudentKey, utc_day
from rhi_gateway import proxy, routes
from rhi_gateway.affinity import COOKIE_NAME, Affinity
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000
OTTER, HERON = 4242, 4343
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
POOLS = parse_pools({"coursework": MOCK, "upgrade": MOCK})
ATTRIBUTES = "; Expires=Fri, 15 Jan 2027 09:20:00 GMT; Path=/"


def sse(*events: tuple[str | None, dict[str, Any] | str]) -> bytes:
    lines = b""
    for name, data in events:
        text = data if isinstance(data, str) else json.dumps(data)
        lines += (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()
    return lines


# Each wire format: a request body, a whole stream, and a whole answer that is not a stream.
FORMATS: dict[routes.Route, tuple[dict[str, Any], bytes, dict[str, Any]]] = {
    routes.MESSAGES: (
        {"max_tokens": 100, "messages": []},
        sse(
            (
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 21, "output_tokens": 1}}},
            ),
            (
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
            ),
            ("message_stop", {"type": "message_stop"}),
        ),
        {"type": "message", "usage": {"input_tokens": 21, "output_tokens": 7}},
    ),
    routes.CHAT_COMPLETIONS: (
        {"stream_options": {"include_usage": True}, "messages": []},
        sse(
            (None, {"choices": [{"index": 0, "delta": {"content": "Hi"}}], "usage": None}),
            (None, {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 9}}),
            (None, "[DONE]"),
        ),
        {"choices": [{"index": 0, "finish_reason": "stop"}], "usage": {"prompt_tokens": 30, "completion_tokens": 9}},
    ),
    routes.RESPONSES: (
        {"input": "hello"},
        sse(
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hi"}),
            (
                "response.completed",
                {"type": "response.completed", "response": {"usage": {"input_tokens": 40, "output_tokens": 11}}},
            ),
        ),
        {"status": "completed", "usage": {"input_tokens": 40, "output_tokens": 11}},
    ),
}


class LoadBalancer:
    """The internal load balancer, in the test: what each request carried, and the cookie each answer set.

    Each answer sets a cookie no answer set before, with the attributes and the second cookie a
    load balancer sends, unless `sets` is false. `status` is the status it answers with. It
    answers nothing until `together` requests have reached it.
    """

    def __init__(self) -> None:
        self.carried: list[tuple[str, str | None]] = []  # The pool and the `Cookie` header of each request.
        self.issued: list[str] = []
        self.sets = True
        self.status = 200
        self.together = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.carried.append((request.headers[proxy.POOL_HEADER], request.headers.get("cookie")))
        while len(self.carried) < self.together:
            await asyncio.sleep(0.001)
        entry = next(entry for entry in FORMATS if entry.upstream_path == request.url.path)
        streams = json.loads(request.content).get("stream") is True
        headers = [("content-type", "text/event-stream" if streams else "application/json")]
        if self.sets:
            self.issued.append(f"target+{len(self.issued)}/opaque==")
            headers.append(("set-cookie", f"{COOKIE_NAME}={self.issued[-1]}{ATTRIBUTES}"))
            headers.append(("set-cookie", f"{COOKIE_NAME}CORS={self.issued[-1]}{ATTRIBUTES}; SameSite=None"))
        _, stream, answer = FORMATS[entry]
        return httpx.Response(self.status, headers=headers, content=stream if streams else json.dumps(answer).encode())


class Gateway:
    """The whole gateway in front of `LoadBalancer`, with two students and two pools, every pool ready."""

    def __init__(self, views: Callable[[], dict[str, PoolView]] | None = None) -> None:
        self.balancer = LoadBalancer()
        self.ledger = InMemoryLedger(clock=lambda: float(NOW))
        self.keys = {OTTER: generate_key(), HERON: generate_key()}
        students = [
            StudentKey(
                key_hash=key_hash(key),
                github_id=github_id,
                alias=f"student-{github_id}",
                installation=None,
                issued_at=NOW - 3600,
                expires_at=NOW + 90 * 86400,
                revoked_at=None,
                daily_token_budget=2_000_000,
                concurrency_limit=4,
            )
            for github_id, key in self.keys.items()
        ]
        forwarding = Forwarding(
            authenticator=Authenticator(InMemoryKeyStore(*students), wall_clock=lambda: float(NOW)),
            ledger=self.ledger,
            filters={
                routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
                routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
                routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
                routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
            },
            proxy=proxy.Proxy(
                proxy.upstream_client(httpx.MockTransport(self.balancer)),
                "http://pools.example.com:8080",
                {pool.name: f"{pool.name}-secret" for pool in POOLS},
            ),
            holding=Holding(
                views=views or (lambda: {pool.name: PoolView(ready=True) for pool in POOLS}),
                wake=lambda pool: None,
                sleep=lambda seconds: asyncio.sleep(0),
            ),
        )
        self.app: Starlette = build_app(forwarding_handlers(forwarding))

    def cookies(self, student: int) -> dict[str, str]:
        """The cookies the student's record holds, by pool."""
        day = self.ledger.day(student, utc_day(NOW))
        return {} if day is None else dict(day.affinity_cookies)


Ask = Callable[..., Awaitable[httpx.Response]]


def run(gateway: Gateway, scenario: Callable[[Ask], Awaitable[None]]) -> None:
    """Run `scenario` with `ask`, which sends one student's request to a pool and returns the client's answer."""

    async def main() -> None:
        transport = httpx.ASGITransport(app=gateway.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:

            async def ask(
                student: int = OTTER,
                pool: str = "coursework",
                *,
                entry: routes.Route = routes.MESSAGES,
                stream: bool = True,
                headers: dict[str, str] | None = None,
            ) -> httpx.Response:
                body = {**FORMATS[entry][0], "model": pool, "stream": stream}
                sent = {"x-api-key": gateway.keys[student], **(headers or {})}
                return await client.post(entry.path, json=body, headers=sent)

            await scenario(ask)

    asyncio.run(main())


def cookie(value: str) -> str:
    """The `Cookie` header that carries this cookie to the load balancer."""
    return f"{COOKIE_NAME}={value}"


def test_a_request_ends_with_the_cookie_its_answer_set_or_the_one_it_was_sent() -> None:
    assert Affinity(None).headers() == {}
    assert Affinity("held").headers() == {"Cookie": "AWSALB=held"}

    # The answer sets one: its value as set, without its attributes, and not the browsers' cookie beside it.
    affinity = Affinity("held")
    affinity.answered(["engine=x; Path=/", f"AWSALB=n+w/=={ATTRIBUTES}", f"AWSALBCORS=other{ATTRIBUTES}"])
    assert affinity.ended == "n+w/=="
    assert affinity.headers() == {"Cookie": "AWSALB=held"}

    # The answer sets none, or none that can be sent back: the request ends with the cookie it was sent.
    for set_cookie in ([], [f"AWSALBCORS=other{ATTRIBUTES}"], ["AWSALB=; Max-Age=0"], ["AWSALB=two words"]):
        affinity = Affinity("held")
        affinity.answered(set_cookie)
        assert affinity.ended == "held"
    first = Affinity(None)
    first.answered([])
    assert first.ended is None


def test_a_second_request_to_a_pool_carries_the_cookie_the_first_answer_set() -> None:
    gateway = Gateway()
    balancer = gateway.balancer

    async def scenario(ask: Ask) -> None:
        # The record holds none yet: none is sent, and a cookie of the client's own is not sent on.
        await ask(headers={"cookie": cookie("chosen-by-the-client")})
        assert balancer.carried == [("coursework", None)]
        assert gateway.cookies(OTTER) == {"coursework": balancer.issued[0]}

        await ask()
        assert balancer.carried[1] == ("coursework", cookie(balancer.issued[0]))
        assert gateway.cookies(OTTER) == {"coursework": balancer.issued[1]}

        # An answer that sets none: the request ends with the cookie it was sent, and the next one sends it again.
        balancer.sets = False
        await ask()
        await ask(stream=False)
        assert balancer.carried[2:] == [("coursework", cookie(balancer.issued[1]))] * 2
        assert gateway.cookies(OTTER) == {"coursework": balancer.issued[1]}

    run(gateway, scenario)


def test_a_request_to_another_pool_carries_none_of_it() -> None:
    gateway = Gateway()
    balancer = gateway.balancer

    async def scenario(ask: Ask) -> None:
        await ask(pool="coursework")
        await ask(pool="upgrade")
        assert balancer.carried[1] == ("upgrade", None)
        assert gateway.cookies(OTTER) == {"coursework": balancer.issued[0], "upgrade": balancer.issued[1]}

        # Each pool's requests then follow that pool's cookie.
        await ask(pool="upgrade")
        await ask(pool="coursework")
        assert balancer.carried[2:] == [
            ("upgrade", cookie(balancer.issued[1])),
            ("coursework", cookie(balancer.issued[0])),
        ]

    run(gateway, scenario)


def test_two_students_never_share_a_cookie() -> None:
    gateway = Gateway()
    balancer = gateway.balancer

    async def scenario(ask: Ask) -> None:
        await ask(OTTER)
        # The upstream client is shared: what it was just answered is not sent for another student.
        await ask(HERON)
        assert balancer.carried[1] == ("coursework", None)
        await ask(OTTER)
        await ask(HERON)
        assert balancer.carried[2:] == [
            ("coursework", cookie(balancer.issued[0])),
            ("coursework", cookie(balancer.issued[1])),
        ]
        assert gateway.cookies(OTTER) == {"coursework": balancer.issued[2]}
        assert gateway.cookies(HERON) == {"coursework": balancer.issued[3]}

    run(gateway, scenario)


def test_parallel_requests_of_a_student_follow_the_same_cookie() -> None:
    gateway = Gateway()
    balancer = gateway.balancer

    async def scenario(ask: Ask) -> None:
        await ask()
        # Three at once, as an agent's subagents send them: none is answered before all have been sent.
        balancer.together = 4
        await asyncio.gather(ask(), ask(stream=False), ask(entry=routes.RESPONSES))
        assert balancer.carried[1:] == [("coursework", cookie(balancer.issued[0]))] * 3
        assert gateway.cookies(OTTER)["coursework"] in balancer.issued[1:]

    run(gateway, scenario)


def test_a_held_stream_carries_the_cookie_and_stores_the_one_its_answer_set() -> None:
    views = [PoolView(ready=True), PoolView(ready=False), PoolView(ready=False)]
    gateway = Gateway(lambda: {"coursework": views.pop(0) if len(views) > 1 else views[0]})
    balancer = gateway.balancer

    async def scenario(ask: Ask) -> None:
        await ask()
        # The pool is not ready when the second request comes: it is answered at once, held, then forwarded.
        views[:] = [PoolView(ready=False), PoolView(ready=False), PoolView(ready=True)]
        held = await ask()
        assert held.status_code == 200
        assert balancer.carried[1] == ("coursework", cookie(balancer.issued[0]))
        assert gateway.cookies(OTTER) == {"coursework": balancer.issued[1]}
        assert "set-cookie" not in held.headers
        assert COOKIE_NAME.encode() not in held.content

    run(gateway, scenario)


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "not streamed"])
@pytest.mark.parametrize("entry", list(FORMATS), ids=lambda entry: entry.path)
def test_the_cookie_never_reaches_the_student(entry: routes.Route, stream: bool) -> None:
    gateway = Gateway()
    balancer = gateway.balancer

    async def scenario(ask: Ask) -> None:
        # The first answer sets a cookie; the second request carries it and its answer sets another;
        # the third is answered with an error status, and a cookie still.
        answers = [await ask(entry=entry, stream=stream), await ask(entry=entry, stream=stream)]
        balancer.status = 500
        answers.append(await ask(entry=entry, stream=stream))
        assert [answer.status_code for answer in answers] == [200, 200, 502]
        assert len(balancer.issued) == 3
        assert balancer.carried[1:] == [("coursework", cookie(value)) for value in balancer.issued[:2]]
        assert gateway.cookies(OTTER) == {"coursework": balancer.issued[2]}

        for answer in answers:
            assert "set-cookie" not in answer.headers
            received = answer.content + b"".join(name + b": " + value for name, value in answer.headers.raw)
            assert COOKIE_NAME.encode() not in received
            assert not any(value.encode() in received for value in balancer.issued)
        # The answers themselves came through: the stream, or the whole answer, of the format.
        _, whole_stream, whole_answer = FORMATS[entry]
        for answer in answers[:2]:
            assert answer.content == whole_stream if stream else json.loads(answer.content) == whole_answer

    run(gateway, scenario)
