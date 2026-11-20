"""A pool's request profile: a configured list of named rewrites (system design, sections 6.1 and 6.2, step 4).

No open model works in Claude Code through vLLM unchanged: for each model family the engine has
defects that decide whether a session works (evidence E28). The gateway works around a pool's
defects with its **request profile**, a short table of rewrites that stay inside one wire format.
The pool's configuration names the entries, in the order they apply
(`rhi_common.pool_config`, `request_profile`), and the body filter runs them on the filtered body,
after its shared rewrites and before it serializes (`rhi_gateway.body`, step 7). A pool that names
none has an empty profile, and its filtered body is forwarded unchanged.

**An entry** is one module of this package, named as the configuration names the entry. The module
holds `ENTRY`, an `Entry`, which declares:

- `name`, the module's own name;
- `wire_formats` and `routes`, the wire formats and the forwarded routes it applies to: the
  formats are exactly those of the routes, and on any other route the entry is not run;
- `defect`, the public address of the engine defect it works around, an `https` URL. An entry is
  deleted when the pinned engine no longer has the defect;
- `rewrite`, which is given the filtered body, the pool and the route, and returns the body to
  send. The pool is there to be read: a rewrite that is also configuration, such as the mapping of
  reasoning effort, reads its values from it (`reasoning_effort`).

**An entry rewrites inside the request's wire format and never changes its pool or its format.**
It is given a body and returns a body: the pool a request goes to and the route it is sent on were
decided before it ran, and are not its to return. The body filter writes the gateway's own fields
again over what a profile returned, so the `model` the engine reads stays the pool's.

**The start.** `build` makes the profile of every configured pool, and the gateway's entry point
calls it before anything else (`rhi_gateway.main.build`). An entry is looked up by its name among
the modules of this package and nowhere else. A name the package has no module for, a module that
declares no entry, or a declaration that does not hold to the rules above raises `ProfileError`,
which names the pool and the entry: the gateway does not start, where it would otherwise have
failed a student's request.

Nothing here reads configuration, the environment or the network. Each entry is written with the
capture that covers it. The package holds two: `reasoning_effort`, which maps the effort a request
sets through the pool's mapping, and `system_in_place`, which sends a system message placed
mid-conversation as a user message in its place.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from rhi_common.pool_config import Pool
from rhi_gateway.body import Rewrite
from rhi_gateway.errors import WireFormat
from rhi_gateway.routes import Kind, Route

# What an entry's module holds its declaration under.
ENTRY_ATTRIBUTE = "ENTRY"

# An entry's rewrite: the filtered body, the pool it is for and the route it came on, to the body to send.
type EntryRewrite = Callable[[dict[str, Any], Pool, Route], dict[str, Any]]


class ProfileError(ValueError):
    """A pool's request profile cannot be built. The message names the pool and the entry, and nothing else."""

    def __init__(self, pool: str, entry: str, problem: str) -> None:
        super().__init__(f"pool {pool!r}, request profile entry {entry!r}: {problem}")
        self.pool = pool
        self.entry = entry


@dataclass(frozen=True)
class Entry:
    """One rewrite of a request profile, and what it declares of itself."""

    name: str
    wire_formats: frozenset[WireFormat]
    routes: frozenset[Route]
    # The public address of the engine defect the entry works around.
    defect: str
    rewrite: EntryRewrite


def _problem(entry: Entry, module: str) -> str | None:
    """What a declaration gets wrong, or None when it holds to the rules."""
    if entry.name != module:
        return "the entry is not declared under its module's name"
    if not entry.routes or any(route.kind is not Kind.FORWARDED for route in entry.routes):
        return "the entry must declare the forwarded routes it applies to"
    if entry.wire_formats != {route.wire_format for route in entry.routes}:
        return "the entry must declare the wire formats of its routes, and no other"
    address = urlsplit(entry.defect)
    if address.scheme != "https" or not address.netloc:
        return "the entry must declare the public address of the engine defect it works around"
    return None


def _entry(pool: str, name: str) -> Entry:
    """The entry of this name, from the module of this package that has the name."""
    if name not in {module.name for module in pkgutil.iter_modules(__path__)}:
        raise ProfileError(pool, name, "the gateway has no entry of this name")
    entry = getattr(importlib.import_module(f"{__name__}.{name}"), ENTRY_ATTRIBUTE, None)
    if not isinstance(entry, Entry):
        raise ProfileError(pool, name, "the module of this name declares no entry")
    problem = _problem(entry, name)
    if problem is not None:
        raise ProfileError(pool, name, problem)
    return entry


@dataclass(frozen=True)
class Profile:
    """A pool's request profile: its entries, in the order the pool's configuration names them."""

    pool: Pool
    entries: tuple[Entry, ...]

    def apply(self, body: dict[str, Any], route: Route) -> dict[str, Any]:
        """The body to send for a filtered body that came on `route`: each entry of that route, in order."""
        for entry in self.entries:
            if route in entry.routes:
                body = entry.rewrite(body, self.pool, route)
        return body


def build(pools: Iterable[Pool]) -> dict[str, Profile]:
    """The profile of every pool, under the pool's name. An entry the gateway does not have raises `ProfileError`."""
    return {pool.name: Profile(pool, tuple(_entry(pool.name, name) for name in pool.request_profile)) for pool in pools}


def rewrites(profiles: Mapping[str, Profile], route: Route) -> dict[str, Rewrite]:
    """What the body filter of `route` runs in its per-pool step, under each pool's name.

    A pool whose profile has no entry for the route is left out: its filtered body is sent unchanged.
    """

    def on_route(profile: Profile) -> Rewrite:
        return lambda body: profile.apply(body, route)

    return {
        name: on_route(profile)
        for name, profile in profiles.items()
        if any(route in entry.routes for entry in profile.entries)
    }
