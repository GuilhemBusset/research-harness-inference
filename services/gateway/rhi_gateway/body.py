"""The body filter: what the three wire formats share (system design, section 6.2, step 4).

vLLM gives no isolation between the tenants of one server, and many of its advisories are one
request field that stalls the engine for everyone. The gateway therefore never forwards a
client's bytes. It reads the body up to a maximum, parses it, keeps what an allow-list names,
rewrites a few fields and sends its own serialization of the result. The order is the design's:

1. **Read**, never past the maximum (`read_body`; 8 MB by default, section 17).
2. **Parse** (`parse`): strict UTF-8, one JSON object, no key twice in any object at any depth,
   no number JSON cannot write (`NaN`, an infinity).
3. **Find the pool** the `model` names. A configured pool's name selects that pool; a name that
   starts with `claude-` selects the default pool when the filter was given one; any other name,
   and a `claude-` name with no default pool, is refused.
4. **Keep the allow-list**: at each level it describes, a field it does not name is dropped. A
   number it marks with a `Limit` is clamped to that limit of the pool.
5. **Refuse a media reference that is not inline data** (below).
6. **Write the gateway's own fields**, whatever the client sent and whatever the allow-list
   names: `model` is the pool's name, the name the engine serves the model under (section 4.2);
   `request_id` is the gateway's ID for the request; `cache_salt` is derived from the student, so
   that one student's cached prefixes cannot be probed by another (section 6.6). A pool whose
   `cache_salt_scope` is `class` asked for the other side of that trade (section 4.7): its salt is
   derived from the pool's name alone, so that its students share the prefixes they have in common.
7. **Apply the pool's request profile**: the rewrite given under the pool's name gets the
   filtered body and returns the body to send. It is the pool's profile on this filter's route
   (`rhi_gateway.profile`): the entries the pool's configuration names, in that order, built when
   the gateway starts. The gateway's own fields of step 6 are then written again, so that no
   entry changes the pool a request is for. A pool whose profile is empty is given no rewrite, and
   is sent the filtered body unchanged.
8. **Serialize** the result. What `Forwarded.body` holds was written by this module.

Every refusal is a `GatewayError` with the cause `request_refused`, which the renderers of
`rhi_gateway.errors` turn into the 400 of step 8. Its message is one of this module's constants,
for the gateway's own use: nothing the client sent is in it.

**The allow-list** is given by the caller: each wire format has its own, written elsewhere. It is
a `Fields` (an object: the fields kept, and what describes each), which may hold `Items` (an
array: what describes each item), `KEEP` (a value kept whole) and `Limit` (a number clamped).
Where it describes an object, an object is filtered, a string, a number, a boolean or a null is
kept as it is (it holds no field to drop), and an array is refused; the same holds, turned round,
where it describes an array. A name the format gives both shapes is a `Shapes`, which describes
the object and the array, each as strictly as it would be alone. `KEEP` is for a value that is data to the engine and holds no media:
a tool's schema, a tool call's arguments. Nothing below a `KEEP` is looked at.

**Media.** The engine can be made to fetch an address it is given, so a reference is forwarded
only as inline data. At every level the allow-list describes, a field named `url`, `image_url`,
`file_url`, `audio_url` or `video_url` must hold a `data:` URL, or an object or an array whose own
fields of those names do; a `file_id` that is not null is refused. The rule goes by the field's
name and not by the block's `type`, so that a block of a type this module has never heard of
carries no reference past it. An image or a document given as base64, or as a `data:` URL, has no
such field with another value and is kept whole, for the request profile to rewrite. A format's
allow-list therefore describes a media block down to its reference, and never puts `KEEP` above
one.

**The engine's field names** are from vLLM's API reference, read on 2026-11-15
(https://docs.vllm.ai/en/latest/api/vllm/entrypoints/openai/chat_completion/protocol/ and the
`responses` and `anthropic` pages beside it). `cache_salt` is a field of the Chat Completions, the
Responses and the Messages request, a string of 1 to 1,024 characters. `request_id` is a field of
the Chat Completions and the Responses request. The Messages request has no such field, and its
model ignores a field it does not declare: there the ID written here is of no use to the engine.

Nothing here reads configuration, the environment or the network.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any, Final, Protocol

from rhi_common.causes import Cause
from rhi_common.pool_config import Pool
from rhi_gateway.errors import GatewayError

# 8 MB (section 17). The configuration key that replaces it is read by whoever builds the filter.
MAX_BODY_BYTES = 8_388_608

MODEL_FIELD = "model"
REQUEST_ID_FIELD = "request_id"
CACHE_SALT_FIELD = "cache_salt"
# Claude Code and the Anthropic SDK name Anthropic's models: such a name means the default pool.
CLAUDE_PREFIX = "claude-"

_REFERENCE_FIELDS = frozenset({"url", "image_url", "file_url", "audio_url", "video_url"})
_STORED_FILE_FIELD = "file_id"
_INLINE_SCHEME = "data:"
# Separates this use of SHA-256 from any other use of a student's identifier.
_SALT_LABEL = b"rhi-gateway cache salt v1\x00"
# Another label, which is no start of the first: no student's identifier gives a class's salt.
_CLASS_SALT_LABEL = b"rhi-gateway class cache salt v1\x00"


class Limit(StrEnum):
    """In an allow-list: a number that may not exceed this limit of the pool (section 17)."""

    CONTEXT_LENGTH = "context_length"
    LARGEST_OUTPUT = "largest_output"

    def of(self, pool: Pool) -> int:
        return pool.context_length if self is Limit.CONTEXT_LENGTH else pool.largest_output


class _Keep(Enum):
    KEEP = "keep"


# In an allow-list: a value kept whole. Nothing below it is described, dropped or checked.
KEEP: Final = _Keep.KEEP


@dataclass(frozen=True)
class Fields:
    """In an allow-list: an object. It keeps the fields named here, each as its own entry describes."""

    fields: Mapping[str, Spec]


@dataclass(frozen=True)
class Items:
    """In an allow-list: an array, each of whose items `item` describes."""

    item: Spec


@dataclass(frozen=True)
class Shapes:
    """In an allow-list: a name the format gives two shapes, an object and an array.

    An object is kept as `fields` describes it, and an array as `items` does.
    """

    fields: Fields
    items: Items

    def of(self, value: Any) -> Fields | Items:
        return self.items if isinstance(value, list) else self.fields


type Spec = Fields | Items | Shapes | Limit | _Keep

# A pool's request profile on one route: it gets the filtered body and returns the body to serialize.
type Rewrite = Callable[[dict[str, Any]], dict[str, Any]]


class BodySource(Protocol):
    """A request's body, read in bounded steps.

    `read_body` never asks for more than the maximum leaves room for, so a source that holds to
    `size` hands the filter no byte past the maximum. Whether a body goes on after that is asked
    with `at_end`, which hands out nothing: HTTP frames a body, so its end is known without
    reading what follows. Over an ASGI server, `read` hands out part of a message the server has
    produced and keeps the rest, and `at_end` is the last message's `more_body`.
    """

    async def read(self, size: int) -> bytes:
        """The next bytes of the body, `size` of them at most; none only when the body has ended."""
        ...

    async def at_end(self) -> bool:
        """Whether the body has no byte left. It hands out none."""
        ...


@dataclass(frozen=True)
class Forwarded:
    """What the gateway sends: the pool the request is for, and the body, in the gateway's own bytes."""

    pool: Pool
    body: bytes


def _refused(reason: str) -> GatewayError:
    return GatewayError(Cause.REQUEST_REFUSED, reason)


class _Unacceptable(ValueError):
    """Raised inside `json.loads` for JSON that parses and that the filter does not take."""


async def read_body(source: BodySource, maximum: int = MAX_BODY_BYTES) -> bytes:
    """The whole body, or a refusal once `maximum` bytes are read and the body goes on.

    No more than `maximum` bytes are ever asked of the source, in total.
    """
    parts: list[bytes] = []
    room = maximum
    while room > 0:
        data = await source.read(room)
        if not data:
            return b"".join(parts)
        if len(data) > room:
            raise _refused("the source of the body gave more than it was asked for")
        parts.append(data)
        room -= len(data)
    if not await source.at_end():
        raise _refused("the body is above the maximum size")
    return b"".join(parts)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise _Unacceptable("the body has a key twice in one object")
    return found


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise _Unacceptable("the body has a number that JSON cannot write")
    return number


def _no_constant(text: str) -> Any:
    raise _Unacceptable("the body has a number that JSON cannot write")


def parse(raw: bytes) -> dict[str, Any]:
    """The JSON object a body holds, or a refusal."""
    reason = None
    body: Any = None
    try:
        body = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_float=_finite, parse_constant=_no_constant
        )
    except UnicodeDecodeError:
        reason = "the body is not UTF-8"
    except _Unacceptable as unacceptable:
        reason = str(unacceptable)
    except (ValueError, RecursionError):
        reason = "the body is not JSON"
    # Raised out here, so that the refusal does not carry the parser's error, which holds the body.
    if reason is not None:
        raise _refused(reason)
    if not isinstance(body, dict):
        raise _refused("the body is not a JSON object")
    return body


def cache_salt(student: str) -> str:
    """The cache salt of a student: the same for one student every time, another for another.

    The client cannot choose it (the filter writes it over whatever the body held), so it need not
    be secret, only different for each student. A digest keeps the student's identifier out of the
    engine and is 64 characters, inside the length the engine accepts.
    """
    return hashlib.sha256(_SALT_LABEL + student.encode("utf-8")).hexdigest()


def class_cache_salt(pool_name: str) -> str:
    """The cache salt of a pool's class: one value for every student of the pool.

    It is derived from the pool's name and from nothing else: no student, no request, and nothing
    of the process that computes it. Every gateway task therefore writes the same salt, and the
    salt says nothing about any student. Another pool has another.
    """
    return hashlib.sha256(_CLASS_SALT_LABEL + pool_name.encode("utf-8")).hexdigest()


def _salt(pool: Pool, student: str) -> str:
    """The salt of a request: the pool's own when its scope is the class, the student's otherwise."""
    return class_cache_salt(pool.name) if pool.cache_salt_scope == "class" else cache_salt(student)


def _clamp(value: Any, limit: int) -> Any:
    if value is None:
        return None
    # A boolean is an `int` to Python, and a string of digits is a number to the engine's parser.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _refused("a field with a limit does not hold a number")
    return min(value, limit)


def _keep(value: Any, spec: Spec, pool: Pool) -> Any:
    """What the allow-list keeps of a value, with its numbers clamped to the pool's limits."""
    if isinstance(spec, _Keep):
        return value
    if isinstance(spec, Limit):
        return _clamp(value, spec.of(pool))
    if isinstance(spec, Shapes):
        spec = spec.of(value)
    if isinstance(spec, Fields):
        if isinstance(value, dict):
            return {name: _keep(inner, spec.fields[name], pool) for name, inner in value.items() if name in spec.fields}
        if isinstance(value, list):
            raise _refused("an array stands where the allow-list describes an object")
        return value
    if isinstance(value, list):
        return [_keep(inner, spec.item, pool) for inner in value]
    if isinstance(value, dict):
        raise _refused("an object stands where the allow-list describes an array")
    return value


def _refuse_references(value: Any, spec: Spec) -> None:
    """Refuse a media reference that is not inline data, at every level the allow-list describes."""
    if isinstance(spec, Shapes):
        spec = spec.of(value)
    if isinstance(value, list) and isinstance(spec, Items):
        for inner in value:
            _refuse_references(inner, spec.item)
    elif isinstance(value, dict) and isinstance(spec, Fields):
        for name, inner in value.items():
            if name in _REFERENCE_FIELDS:
                _refuse_what_is_not_inline(inner)
            elif name == _STORED_FILE_FIELD:
                _refuse_stored_file(inner)
            else:
                _refuse_references(inner, spec.fields[name])


def _refuse_what_is_not_inline(value: Any) -> None:
    """What a reference field holds, however the allow-list describes it."""
    if isinstance(value, str):
        if not value.startswith(_INLINE_SCHEME):
            raise _refused("a media reference is not inline data")
    elif isinstance(value, list):
        for inner in value:
            _refuse_what_is_not_inline(inner)
    elif isinstance(value, dict):
        for name, inner in value.items():
            if name in _REFERENCE_FIELDS:
                _refuse_what_is_not_inline(inner)
            elif name == _STORED_FILE_FIELD:
                _refuse_stored_file(inner)


def _refuse_stored_file(value: Any) -> None:
    if value is not None:
        raise _refused("a media reference names a stored file")


def _serialize(body: Mapping[str, Any]) -> bytes:
    # ASCII: every other character is written as an escape, so the bytes are UTF-8 whatever the
    # strings hold.
    return json.dumps(body, separators=(",", ":"), allow_nan=False).encode("ascii")


def pool_named(model: Any, pools: tuple[Pool, ...], default_pool: Pool | None) -> Pool:
    """The pool a `model` value names (step 3), or a refusal: the wake finds its pool the same way."""
    if isinstance(model, str):
        for pool in pools:
            if pool.name == model:
                return pool
        if default_pool is not None and model.startswith(CLAUDE_PREFIX):
            return default_pool
    raise _refused("the model names no pool")


@dataclass(frozen=True)
class BodyFilter:
    """The filter of one wire format: its allow-list, the pools, and what each pool's requests go through.

    `default_pool` is the pool of the `claude-` names; a deployment may have none, and the filter
    is then given none. `profiles` holds the request profile of each pool that has one, under the
    pool's name: what `rhi_gateway.profile.rewrites` gives for the filter's route. `maximum` is the
    largest body `read` takes, in bytes.
    """

    allow_list: Fields
    pools: tuple[Pool, ...]
    default_pool: Pool | None = None
    profiles: Mapping[str, Rewrite] = field(default_factory=dict)
    maximum: int = MAX_BODY_BYTES

    async def read(self, source: BodySource, *, student: str, request_id: str) -> Forwarded:
        """Read a request's body, never past the maximum, and give what to forward for it."""
        return self.apply(await read_body(source, self.maximum), student=student, request_id=request_id)

    def apply(self, raw: bytes, *, student: str, request_id: str) -> Forwarded:
        """What to forward for a body: the pool it names and the gateway's serialization.

        `student` identifies who sent the request, and `request_id` is the gateway's own ID for
        it: neither comes from the body.
        """
        if not student or not request_id:
            raise ValueError("the filter needs the student and the gateway's request ID")
        if len(raw) > self.maximum:
            raise _refused("the body is above the maximum size")
        body = parse(raw)
        pool = pool_named(body.get(MODEL_FIELD), self.pools, self.default_pool)
        try:
            kept = _keep(body, self.allow_list, pool)
            _refuse_references(kept, self.allow_list)
            own = {MODEL_FIELD: pool.name, REQUEST_ID_FIELD: request_id, CACHE_SALT_FIELD: _salt(pool, student)}
            kept.update(own)
            profile = self.profiles.get(pool.name)
            if profile is not None:
                # A profile rewrites the request and never whose it is, or which pool's.
                kept = {**profile(kept), **own}
            return Forwarded(pool, _serialize(kept))
        except RecursionError:
            # The parser takes deeper nesting than a walk in Python can follow.
            pass
        raise _refused("the body is nested too deep")
