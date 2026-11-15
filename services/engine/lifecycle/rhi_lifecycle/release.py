"""A pool's release: what its parameter says, what the instance holds, and whether they are equal.

AWS leaves an existing instance alone when a launch template changes, and user data runs only on
an instance's first boot (system design, section 4.3). So at every start, including a restart from
the warm pool, the lifecycle agent reads its pool's current release from the pool's one parameter
and compares it with the release the instance holds on disk. `check` gives the verdict: the agent
abandons the instance unless the two are equal.

**The parameter.** Its value is one JSON object, which the pipeline writes and the boot program
also reads, so its keys are fixed:

- `engine_image`: the engine image, by repository and digest (`<repository>@sha256:<digest>`);
- `model_revision`: the pinned commit of the model, or null for a pool that serves no model;
- `engine_flags`: the engine's flags, a list of strings, in order;
- `files`: `bucket`, `key` and `sha256`, where the boot unit fetches the release's files (the unit
  files and this agent) and the digest it checks them against;
- `measured_start_seconds`: the pool's measured start, absent (or null) until one is measured;
- `settings`: `pool`, `group`, `launch_hook`, `termination_hook`, `log_group`, `workspace_url`,
  `heartbeat_table` and `secret_id`.

A release is the first three and `files.sha256`: what the instance serves. The measured start and
the settings change how the instance runs, not what it serves, so they are read and left out of
the comparison. A key this module does not know is ignored.

**The disk.** The record of what the instance holds is a JSON object with the parameter's release
keys (`engine_image`, `model_revision`, `engine_flags`, `files.sha256`); the parameter's own value,
kept as it was when the instance was prepared, is one.

Nothing here raises for a parameter or a record that cannot be read or is not a release: each
counts as not equal. `NotAReleaseError` names a key, never a value.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

# Where the record of the release the instance holds is kept.
HELD_RELEASE_FILE = Path("/var/lib/rhi/release.json")

# A tag would move under a deployed pool: the image is named by its digest.
_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


class NotAReleaseError(ValueError):
    """A value is not in the parameter's form. The message names the key, never a value."""

    def __init__(self, key: str, problem: str) -> None:
        super().__init__(f"{key}: {problem}")
        self.key = key


@dataclass(frozen=True)
class Release:
    """What an instance serves. Two releases are equal when every field is."""

    # `<repository>@sha256:<digest>`: the reference the host pulls.
    engine_image: str
    model_revision: str | None
    engine_flags: tuple[str, ...]
    # The SHA-256 digest of the release's files: the unit files and the lifecycle agent.
    files_sha256: str


@dataclass(frozen=True)
class Settings:
    """What the programs on the instance need and cannot know otherwise. Not part of a release."""

    pool: str
    group: str
    launch_hook: str
    termination_hook: str
    log_group: str
    workspace_url: str
    heartbeat_table: str
    secret_id: str


@dataclass(frozen=True)
class Current:
    """What the pool's parameter holds: the current release, and what is outside the comparison."""

    release: Release
    # None until a start has been measured (spike S1).
    measured_start_seconds: int | None
    settings: Settings


@dataclass(frozen=True)
class Verdict:
    """`equal` is true only when the instance holds the pool's current release: the agent abandons
    the instance otherwise. `current` is None when the parameter cannot be read or is not a
    release, `held` when the disk has no record of one."""

    equal: bool
    current: Current | None
    held: Release | None


def _table(value: object, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NotAReleaseError(key, "must be an object")
    return value


def _text(table: dict[str, Any], key: str, where: str = "") -> str:
    value = table.get(key)
    if not isinstance(value, str) or value == "":
        raise NotAReleaseError(where + key, "must be a string that is not empty")
    return value


def _release(document: object) -> tuple[Release, dict[str, Any]]:
    """The release a JSON document holds, and the document, now known to be an object."""
    table = _table(document, "the value")
    image = _text(table, "engine_image")
    if _IMAGE.fullmatch(image) is None:
        raise NotAReleaseError("engine_image", "must name the image by repository and digest")
    # The key is always there: a pool that serves no model says so with null.
    if "model_revision" not in table:
        raise NotAReleaseError("model_revision", "is missing")
    revision = None if table["model_revision"] is None else _text(table, "model_revision")
    flags = table.get("engine_flags")
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise NotAReleaseError("engine_flags", "must be a list of strings")
    digest = _text(_table(table.get("files"), "files"), "sha256", "files.")
    if _SHA256.fullmatch(digest) is None:
        raise NotAReleaseError("files.sha256", "must be a SHA-256 digest in lower-case hexadecimal")
    return Release(image, revision, tuple(flags), digest), table


def _load(text: str) -> object:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise NotAReleaseError("the value", "must be JSON") from None


def parse_current(text: str) -> Current:
    """What a parameter's value holds. A value that is not in the parameter's form raises
    `NotAReleaseError`."""
    release, table = _release(_load(text))
    for key in ("bucket", "key"):
        _text(table["files"], key, "files.")
    measured = table.get("measured_start_seconds")
    # A boolean is an `int` to Python, and not a number of seconds to whoever wrote the value.
    if measured is not None and (isinstance(measured, bool) or not isinstance(measured, int) or measured < 1):
        raise NotAReleaseError("measured_start_seconds", "must be a whole number of at least 1")
    settings = _table(table.get("settings"), "settings")
    return Current(
        release,
        measured,
        Settings(**{field.name: _text(settings, field.name, "settings.") for field in fields(Settings)}),
    )


def read_current(ssm: Any, name: str) -> Current | None:
    """What the parameter `name` holds, read with the SSM client `ssm`, or None when it cannot be
    read or does not hold a release."""
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except (BotoCoreError, ClientError, KeyError, TypeError):
        return None
    if not isinstance(value, str):
        return None
    try:
        return parse_current(value)
    except NotAReleaseError:
        return None


def read_held(path: Path = HELD_RELEASE_FILE) -> Release | None:
    """The release the instance holds, from its record on disk, or None when there is no record
    or it does not hold a release."""
    try:
        return _release(_load(path.read_text(encoding="utf-8")))[0]
    except (OSError, ValueError):  # a text that is not UTF-8 and NotAReleaseError are ValueErrors
        return None


def check(ssm: Any, name: str, path: Path = HELD_RELEASE_FILE) -> Verdict:
    """Whether the instance holds the current release of its pool, whose parameter is `name`."""
    current = read_current(ssm, name)
    held = read_held(path)
    return Verdict(current is not None and held is not None and current.release == held, current, held)
