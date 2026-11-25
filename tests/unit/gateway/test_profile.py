"""A pool's request profile (`rhi_gateway.profile`; system design, section 6.2, step 4).

The package holds no entry of its own yet, so the tests write theirs: each is a module file in a
temporary directory that is added to the package's path, which is where the gateway looks an entry
up by its name. The gateway is built as its entry point builds it, from an environment whose pools
name those entries, and a body is sent through the filter of each forwarded route. The ARNs carry
the placeholder account, and the pools' secret is drawn when the tests run.
"""

from __future__ import annotations

import importlib
import json
import secrets
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rhi_gateway import profile, routes
from rhi_gateway.body import BodyFilter
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.main import Clients, build
from rhi_gateway.profile import ProfileError
from rhi_gateway.settings import read_settings

MOCK = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.02},
    "wake": "on-demand",
    "max_instances": 2,
    "warm_instances": 1,
    "target_concurrency": 8,
    "context_length": 262144,
    "largest_output": 32000,
}
ACCOUNT = "123456789012"
FORWARDED = tuple(route for route in routes.TABLE if route.kind is routes.Kind.FORWARDED)
EVERY_ROUTE = "routes.MESSAGES, routes.COUNT_TOKENS, routes.CHAT_COMPLETIONS, routes.RESPONSES"
MESSAGES_ROUTES = "routes.MESSAGES, routes.COUNT_TOKENS"

# An entry's module. Its rewrite keeps what it was given and adds its name after those of the
# entries that ran before it, then whatever a test has it overwrite.
MODULE = """
import copy

from rhi_gateway import routes
from rhi_gateway.errors import WireFormat
from rhi_gateway.profile import Entry

SEEN = []


def rewrite(body, pool, route):
    SEEN.append((copy.deepcopy(body), pool.name, route))
    return {{**body, "applied": [*body.get("applied", []), {name!r}], **{overwrites!r}}}


{attribute} = Entry(
    name={declared!r},
    wire_formats=frozenset({{{formats}}}),
    routes=frozenset({{{on}}}),
    defect={defect!r},
    rewrite=rewrite,
)
"""

type Write = Callable[..., None]
type Filter = BodyFilter | ChatBodyFilter


@pytest.fixture
def write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Write]:
    """Writes an entry's module where the gateway finds its entries; every keyword replaces a part of it."""
    monkeypatch.setattr(profile, "__path__", [*profile.__path__, str(tmp_path)])
    written: list[str] = []

    def entry(name: str, *, on: str = EVERY_ROUTE, formats: str | None = None, **parts: Any) -> None:
        # Left to itself, an entry declares the wire formats of its routes.
        formats = formats or f"route.wire_format for route in ({on},)"
        source = {"declared": name, "attribute": "ENTRY", "defect": "https://example.com/engine/issues/1"}
        text = MODULE.format(name=name, on=on, formats=formats, **{"overwrites": {}, **source, **parts})
        (tmp_path / f"{name}.py").write_text(text)
        written.append(name)
        importlib.invalidate_caches()

    yield entry
    for name in written:
        sys.modules.pop(f"{profile.__name__}.{name}", None)
        vars(profile).pop(name, None)


def seen(name: str) -> list[tuple[dict[str, Any], str, routes.Route]]:
    """What the rewrite of a test's entry was given, call by call."""
    return importlib.import_module(f"{profile.__name__}.{name}").SEEN


def environment(**profiles: list[str]) -> dict[str, str]:
    """The environment of a gateway with one pool for each name, which names these entries; the first is the default."""
    pools = {name: {**MOCK, "request_profile": entries} if entries else MOCK for name, entries in profiles.items()}
    arn = f"arn:aws:{{service}}:us-east-1:{ACCOUNT}:{{resource}}"
    groups = {
        name: arn.format(service="elasticloadbalancing", resource=f"targetgroup/{name}/0123456789abcdef")
        for name in pools
    }
    return {
        "RHI_POOLS": json.dumps(pools),
        "RHI_DEFAULT_POOL": next(iter(pools)),
        "RHI_UPSTREAM_URL": "http://internal.example.com:8000",
        "RHI_TARGET_GROUPS": json.dumps(groups),
        "RHI_POOL_SECRETS": json.dumps(
            {name: arn.format(service="secretsmanager", resource=f"secret:pool-{name}-AbCdEf") for name in pools}
        ),
        "RHI_KEYS_TABLE": "keys-of-the-test",
        "RHI_USAGE_TABLE": "usage-of-the-test",
        "RHI_HEARTBEAT_TABLE": "heartbeats-of-the-test",
        "RHI_POOL_STATE_TABLE": "pool-state-of-the-test",
        "RHI_SCALER_FUNCTION": "scaler-of-the-test",
        "RHI_COURSE_END": "2027-01-15",
    }


def filters(**profiles: list[str]) -> dict[routes.Route, Filter]:
    """The body filters of the gateway its entry point builds for these pools, over clients that reach nothing."""
    aws = Clients(*(MagicMock() for _ in range(4)))
    aws.secretsmanager.get_secret_value.return_value = {"SecretString": secrets.token_urlsafe(24)}
    built = build(read_settings(environment(**profiles)), aws).forwarding.filters
    # Each is a format's own filter, which a test gives a body with no server around it.
    own = {route: found for route, found in built.items() if isinstance(found, (BodyFilter, ChatBodyFilter))}
    assert set(own) == set(built) == set(FORWARDED)
    return own


def request(model: str) -> bytes:
    """A body every forwarded route takes: each allow-list keeps its own fields of it and drops `unlisted`."""
    conversation = [{"role": "user", "content": "Hello"}]
    return json.dumps(
        {"model": model, "messages": conversation, "input": "Hello", "stream": True, "unlisted": 1}
    ).encode()


def forwarded(route_filter: Filter, model: str) -> dict[str, Any]:
    return json.loads(route_filter.apply(request(model), student="student-1", request_id="gateway-id").body)


def test_two_entries_apply_in_the_order_the_pools_profile_names_them(write: Write) -> None:
    write("first")
    write("second")
    on_messages = filters(coursework=["first", "second"], upgrade=["second", "first"])[routes.MESSAGES]

    assert forwarded(on_messages, "coursework")["applied"] == ["first", "second"]
    assert forwarded(on_messages, "upgrade")["applied"] == ["second", "first"]
    # Each entry was given what the one before it returned.
    assert [body.get("applied") for body, pool, _ in seen("second") if pool == "coursework"] == [["first"]]


def test_a_pool_with_an_empty_profile_forwards_its_filtered_body_unchanged(write: Write) -> None:
    write("first")
    with_profiles = filters(coursework=["first"], plain=[])
    pools = read_settings(environment(coursework=[], plain=[])).pools
    # The filters as they were before a pool could have a profile.
    without = {
        routes.MESSAGES: BodyFilter(messages.MESSAGES, pools, pools[0]),
        routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, pools, pools[0]),
        routes.CHAT_COMPLETIONS: ChatBodyFilter(pools, pools[0]),
        routes.RESPONSES: BodyFilter(responses.RESPONSES, pools, pools[0]),
    }

    assert set(with_profiles) == set(without) == set(FORWARDED)
    for route in FORWARDED:
        sent = with_profiles[route].apply(request("plain"), student="student-1", request_id="gateway-id")
        assert sent.body == without[route].apply(request("plain"), student="student-1", request_id="gateway-id").body
        assert sent.pool.name == "plain"
    assert seen("first") == []


def test_a_profile_that_names_an_unknown_entry_stops_the_gateways_start(write: Write) -> None:
    write("first")
    settings = read_settings(environment(mock=[], coursework=["first", "missing"]))
    # A client that is asked for anything fails the test: the start stops before a secret is read.
    untouched = MagicMock(spec_set=[])

    with pytest.raises(ProfileError) as refusal:
        build(settings, Clients(*[untouched] * 4))

    assert (refusal.value.pool, refusal.value.entry) == ("coursework", "missing")
    assert "'coursework'" in str(refusal.value)
    assert "'missing'" in str(refusal.value)
    # A module anywhere else is no entry: the name is looked up in the profile package only.
    with pytest.raises(ProfileError):
        build(read_settings(environment(coursework=["json"])), Clients(*[untouched] * 4))


@pytest.mark.parametrize("route", FORWARDED, ids=lambda route: route.path)
def test_the_profile_runs_on_every_forwarded_route_after_the_shared_rewrites(write: Write, route: routes.Route) -> None:
    write("everywhere")
    write("messages_only", on=MESSAGES_ROUTES)
    route_filter = filters(coursework=["everywhere", "messages_only"])[route]

    sent = forwarded(route_filter, "claude-opus-5-5")

    # The entry got the body after the allow-list and the gateway's own fields, with its pool and its route.
    ((body, pool, on),) = seen("everywhere")
    assert (pool, on) == ("coursework", route)
    assert (body["model"], body["request_id"], "unlisted" in body) == ("coursework", "gateway-id", False)
    assert len(body["cache_salt"]) == 64
    if route is routes.CHAT_COMPLETIONS:
        assert body["stream_options"] == {"include_usage": True}
    # What the profile returned is what is serialized: an entry runs on the routes it declares, and no other.
    applied = ["everywhere", "messages_only"] if route.wire_format is routes.MESSAGES.wire_format else ["everywhere"]
    assert sent == {**body, "applied": applied}


def test_an_entry_changes_neither_the_pool_nor_the_gateways_own_fields(write: Write) -> None:
    write(
        "grasping", overwrites={"model": "upgrade", "request_id": "its-own", "cache_salt": "its-own", "stream": False}
    )
    for route, route_filter in filters(coursework=["grasping"], upgrade=[]).items():
        plain = forwarded(filters(coursework=[], upgrade=[])[route], "coursework")
        sent = route_filter.apply(request("coursework"), student="student-1", request_id="gateway-id")

        assert sent.pool.name == "coursework"
        assert json.loads(sent.body) == {**plain, "applied": ["grasping"], "stream": False}


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param({"declared": "another_name"}, id="another-name-than-its-modules"),
        pytest.param({"on": "", "formats": "WireFormat.MESSAGES"}, id="no-route"),
        pytest.param({"on": "routes.MESSAGES, routes.STATUS", "formats": "WireFormat.MESSAGES"}, id="a-local-route"),
        pytest.param({"on": MESSAGES_ROUTES, "formats": "WireFormat.CHAT_COMPLETIONS"}, id="a-format-not-its-routes"),
        pytest.param({"formats": "WireFormat.MESSAGES"}, id="a-route-of-a-format-it-does-not-declare"),
        pytest.param({"defect": "an engine defect"}, id="no-address-of-the-defect"),
        pytest.param({"defect": "http://example.com/engine/issues/1"}, id="an-address-that-is-not-https"),
        pytest.param({"attribute": "DECLARED"}, id="no-declaration"),
    ],
)
def test_an_entry_that_does_not_declare_what_it_must_stops_the_start(write: Write, parts: dict[str, Any]) -> None:
    write("declared", **parts)

    with pytest.raises(ProfileError) as refusal:
        profile.build(read_settings(environment(coursework=["declared"])).pools)

    assert (refusal.value.pool, refusal.value.entry) == ("coursework", "declared")
