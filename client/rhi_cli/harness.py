"""What `rhi login` writes for the harnesses: where the endpoint is, and how to ask for the key.

The key is in neither file. Each harness is told to run `rhi token` (`rhi_cli.token`) when it needs
the key, so a settings file that ends up in a commit gives nothing away (system design, section
6.4, step 5).

**Claude Code**, in its user settings (`settings.json` in `~/.claude`, or in the folder
`CLAUDE_CONFIG_DIR` names), three members:

- `apiKeyHelper`: `rhi token`;
- `env.ANTHROPIC_BASE_URL`: the endpoint. Claude Code sends `claude-` model names, which the
  gateway maps to the default pool, so no model is named here.
- `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`: `AUTO_COMPACT_WINDOW`, 200,000 tokens. Claude Code
  recognises its `claude-` names and, behind a custom base URL, gives a model that has one its 1M
  window, so it would compact a session only near 967,000 tokens and the engine would refuse the
  turn first. At 200,000, with the 32,000 tokens of output Claude Code asks for, a session stays
  inside the 262,144 the coursework pool serves (system design, section 4.2). This is the setting
  for every model; `CLAUDE_CODE_MAX_CONTEXT_TOKENS` applies to a recognised name only with
  compaction off. The value is a plain number: a `k` suffix needs Claude Code 2.1.304 or later.

**Codex**, in the profile `rhi`: the file `rhi.config.toml` in `~/.codex`, or in the folder
`CODEX_HOME` names. `codex --profile rhi` reads it over `config.toml`, which a login never touches.
A profile is a file of its own since Codex 0.134.0, which refuses `--profile` when `config.toml`
holds a table `[profiles.rhi]`. The profile holds:

- `model_provider = "rhi"`, and `model`, the default pool's model name as the gateway's
  `GET /v1/models` gives it (`default_model`): Codex sends the name its profile holds;
- `[model_providers.rhi]`: `name`, `base_url` (the endpoint's `/v1`) and `wire_api = "responses"`,
  the only format Codex accepts;
- `[model_providers.rhi.auth]`: `command = "rhi"`, `args = ["token"]`.

The profile sets no stream timeout: the gateway keeps no stream waiting (section 5.3), and Codex
keeps its own default. An earlier `rhi login` wrote `stream_idle_timeout_ms = 900000` in the
provider's table, for a gateway that did; a login removes that line (`WITHDRAWN`), and leaves any
other value, which is the student's own.

Those settings are the ones a login owns. Every other one in the two files keeps its value, and a
file whose owned settings are already right is not written: each function returns the file's path when it wrote it
and None when it did not. A file that cannot be read as what it must be, or changed without
touching another setting, is left as it is and `HarnessError` says so.

Codex's profile is edited as text, a line per owned setting, so the comments and the layout of what
a student added to it stay. The result is then read back: it is written only if the owned settings
are right, the withdrawn one is gone and every other setting is what it was.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

HELPER = ("rhi", "token")
NAME = "rhi"
# How full, in tokens, Claude Code lets a session's context get before it compacts it.
AUTO_COMPACT_WINDOW = 200_000

type Table = tuple[str, ...]

# The tables of Codex's profile that hold owned settings; the first is the file's top level.
TOP: Table = ()
PROVIDER: Table = ("model_providers", NAME)
AUTH: Table = (*PROVIDER, "auth")

# What an earlier `rhi login` wrote in Codex's profile and a login now removes, at that value alone.
WITHDRAWN: dict[Table, dict[str, Any]] = {PROVIDER: {"stream_idle_timeout_ms": 900_000}}


class HarnessError(Exception):
    """A harness's settings file cannot be read or changed; the message is for the student."""


def claude_code_settings() -> Path:
    """Claude Code's user settings file."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def codex_profile() -> Path:
    """The file of Codex's profile `rhi`."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / f"{NAME}.config.toml"


def default_model(models: Any) -> str | None:
    """The model name that the body of the gateway's `GET /v1/models` marks as the default, None if none is."""
    entries = models.get("data") if isinstance(models, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("default") is True and isinstance(entry.get("id"), str):
            return entry["id"] or None
    return None


def _read(path: Path) -> str:
    """The file's text; empty when there is no file."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as error:
        raise HarnessError(f"{path} cannot be read: it is left as it is.") from error


def _write(path: Path, text: str) -> None:
    # In place, so that a file a student keeps as a link, or with a mode of their own, stays so.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        raise HarnessError(f"{path} cannot be written.") from error


def configure_claude_code(endpoint: str) -> Path | None:
    """Point Claude Code at the endpoint, with `rhi token` as its key helper."""
    path = claude_code_settings()
    text = _read(path)
    try:
        settings = json.loads(text) if text.strip() else {}
    except ValueError as error:
        raise HarnessError(f"{path} is not valid JSON: it is left as it is.") from error
    environment = settings.get("env", {}) if isinstance(settings, dict) else None
    if not isinstance(environment, dict):
        raise HarnessError(f"{path} is not the JSON object Claude Code reads: it is left as it is.")
    owned = {"ANTHROPIC_BASE_URL": endpoint, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(AUTO_COMPACT_WINDOW)}
    wanted = {**settings, "apiKeyHelper": " ".join(HELPER), "env": {**environment, **owned}}
    if wanted == settings:
        return None
    _write(path, json.dumps(wanted, indent=2, ensure_ascii=False) + "\n")
    return path


def configure_codex(endpoint: str, model: str) -> Path | None:
    """Give Codex the profile `rhi`: the endpoint as its provider, `rhi token` and the model, and no stream timeout."""
    path = codex_profile()
    text = _read(path)
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise HarnessError(f"{path} is not valid TOML: it is left as it is.") from error
    wanted: dict[Table, dict[str, Any]] = {
        TOP: {"model_provider": NAME, "model": model},
        PROVIDER: {"name": NAME, "base_url": endpoint + "/v1", "wire_api": "responses"},
        AUTH: {"command": HELPER[0], "args": list(HELPER[1:])},
    }
    # The withdrawn settings this file holds, at the value an earlier login wrote.
    stale = {table: values for table, values in _owned(before, WITHDRAWN).items() if values == WITHDRAWN[table]}
    if _owned(before, wanted) == wanted and not stale:
        return None

    lines = text.removesuffix("\n").split("\n") if text else []
    for table, values in wanted.items():
        held = _table(before, table)
        _set(lines, table, {key: value for key, value in values.items() if key not in held or held[key] != value})
    for table, values in stale.items():
        _remove(lines, table, values)
    edited = "\n".join(lines) + "\n"
    try:
        after = tomllib.loads(edited)
    except tomllib.TOMLDecodeError:
        after = None
    # What the login may change: its own settings, and the withdrawn ones it removes.
    changed = {table: {**values, **stale.get(table, {})} for table, values in wanted.items()}
    if (
        after is None
        or _owned(after, wanted) != wanted
        or any(_owned(after, stale).values())
        or _others(after, changed) != _others(before, changed)
    ):
        raise HarnessError(
            f"{path} holds its settings in a form that rhi login cannot change: it is left as it is. "
            f"Remove its settings model and model_provider and its table {'.'.join(PROVIDER)}, "
            "then run rhi login again."
        )
    _write(path, edited)
    return path


def _table(document: Mapping[str, Any], table: Table) -> Mapping[str, Any]:
    """The table at that path of a parsed file; an empty one when the file has none."""
    at: Any = document
    for name in table:
        at = at.get(name) if isinstance(at, dict) else None
    return at if isinstance(at, dict) else {}


def _owned(document: Mapping[str, Any], wanted: Mapping[Table, Mapping[str, Any]]) -> dict[Table, dict[str, Any]]:
    """What a parsed file holds for the settings a login owns."""
    return {
        table: {key: held[key] for key in values if key in held}
        for table, values in wanted.items()
        for held in [_table(document, table)]
    }


def _others(document: Mapping[str, Any], wanted: Mapping[Table, Mapping[str, Any]]) -> dict[str, Any]:
    """A parsed file without the settings a login owns, and without the tables that leaves empty."""
    rest = copy.deepcopy(dict(document))
    for table in sorted(wanted, key=len, reverse=True):
        path = [rest]
        for name in table:
            inner = path[-1].get(name)
            if not isinstance(inner, dict):
                break
            path.append(inner)
        else:
            for key in wanted[table]:
                path[-1].pop(key, None)
        for depth in range(len(path) - 1, 0, -1):
            if not path[depth]:
                del path[depth - 1][table[depth - 1]]
    return rest


def _value(value: Any) -> str:
    """A string, an integer or a list of them as TOML writes it."""
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _key(name: str) -> str:
    """A pattern for one key as TOML lets a file write it: bare, or between quotes of either kind."""
    name = re.escape(name)
    return rf"""(?:{name}|"{name}"|'{name}')"""


def _setting(key: str) -> re.Pattern[str]:
    """A pattern for the start of a line that sets that key."""
    return re.compile(rf"\s*{_key(key)}\s*=")


def _section(lines: list[str], table: Table) -> tuple[int, int] | None:
    """Where one table's section is in the file's lines: its header's line (-1 for the top level,
    whose section is the lines before the first table) and the line after its last; None when the
    file has no such section."""
    name = r"\s*\.\s*".join(map(_key, table))
    header = re.compile(rf"\s*\[\s*{name}\s*\]\s*(?:#.*)?")
    start = next((index for index, line in enumerate(lines) if header.fullmatch(line)), None) if table else -1
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")), len(lines))
    return start, end


def _remove(lines: list[str], table: Table, keys: Iterable[str]) -> None:
    """Take the lines that set those keys out of one table's section."""
    start, end = _section(lines, table) or (0, 0)
    settings = [_setting(key) for key in keys]
    lines[start + 1 : end] = [
        line for line in lines[start + 1 : end] if not any(setting.match(line) for setting in settings)
    ]


def _set(lines: list[str], table: Table, values: Mapping[str, Any]) -> None:
    """Set the keys of one table in the file's lines: in its section when it has one, in a new one at the end."""
    if not values:
        return
    start, end = _section(lines, table) or (None, 0)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{'.'.join(table)}]")
        lines.extend(f"{key} = {_value(value)}" for key, value in values.items())
        return
    added = []
    for key, value in values.items():
        setting = _setting(key)
        at = next((index for index in range(start + 1, end) if setting.match(lines[index])), None)
        if at is None:
            added.append(f"{key} = {_value(value)}")
        else:
            lines[at] = f"{key} = {_value(value)}"
    # After the section's last line that holds something, so above the blank lines that end it.
    below = max((index for index in range(start + 1, end) if lines[index].strip()), default=start) + 1
    lines[below:below] = added
