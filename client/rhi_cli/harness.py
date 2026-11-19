"""What `rhi login` writes for the harnesses: where the endpoint is, and how to ask for the key.

The key is in neither file. Each harness is told to run `rhi token` (`rhi_cli.token`) when it needs
the key, so a settings file that ends up in a commit gives nothing away (system design, section
6.4, step 5).

**Claude Code**, in its user settings (`settings.json` in `~/.claude`, or in the folder
`CLAUDE_CONFIG_DIR` names), two members:

- `apiKeyHelper`: `rhi token`;
- `env.ANTHROPIC_BASE_URL`: the endpoint. Claude Code sends `claude-` model names, which the
  gateway maps to the default pool, so no model is named here.

**Codex**, in the profile `rhi`: the file `rhi.config.toml` in `~/.codex`, or in the folder
`CODEX_HOME` names. `codex --profile rhi` reads it over `config.toml`, which a login never touches.
A profile is a file of its own since Codex 0.134.0, which refuses `--profile` when `config.toml`
holds a table `[profiles.rhi]`. The profile holds:

- `model_provider = "rhi"`, and `model`, the default pool's model name as the gateway's
  `GET /v1/models` gives it (`default_model`): Codex sends the name its profile holds;
- `[model_providers.rhi]`: `name`, `base_url` (the endpoint's `/v1`), `wire_api = "responses"`,
  the only format Codex accepts, and `stream_idle_timeout_ms = 900000`. Codex's idle timer ignores
  the gateway's keep-alive comments and stops a stream after 300 seconds by default, which is
  shorter than the hold of a cold pool (section 5.3);
- `[model_providers.rhi.auth]`: `command = "rhi"`, `args = ["token"]`.

Those settings are the ones a login owns. Every other one in the two files keeps its value, and a
file whose owned settings are already right is not written: each function returns the file's path when it wrote it
and None when it did not. A file that cannot be read as what it must be, or changed without
touching another setting, is left as it is and `HarnessError` says so.

Codex's profile is edited as text, a line per owned setting, so the comments and the layout of what
a student added to it stay. The result is then read back: it is written only if the owned settings
are right and every other setting is what it was.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HELPER = ("rhi", "token")
NAME = "rhi"
STREAM_IDLE_TIMEOUT_MS = 900_000

type Table = tuple[str, ...]

# The tables of Codex's profile that hold owned settings; the first is the file's top level.
TOP: Table = ()
PROVIDER: Table = ("model_providers", NAME)
AUTH: Table = (*PROVIDER, "auth")


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
    wanted = {**settings, "apiKeyHelper": " ".join(HELPER), "env": {**environment, "ANTHROPIC_BASE_URL": endpoint}}
    if wanted == settings:
        return None
    _write(path, json.dumps(wanted, indent=2, ensure_ascii=False) + "\n")
    return path


def configure_codex(endpoint: str, model: str) -> Path | None:
    """Give Codex the profile `rhi`: the endpoint as its provider, `rhi token`, the timeout and the model."""
    path = codex_profile()
    text = _read(path)
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise HarnessError(f"{path} is not valid TOML: it is left as it is.") from error
    wanted: dict[Table, dict[str, Any]] = {
        TOP: {"model_provider": NAME, "model": model},
        PROVIDER: {
            "name": NAME,
            "base_url": endpoint + "/v1",
            "wire_api": "responses",
            "stream_idle_timeout_ms": STREAM_IDLE_TIMEOUT_MS,
        },
        AUTH: {"command": HELPER[0], "args": list(HELPER[1:])},
    }
    if _owned(before, wanted) == wanted:
        return None

    lines = text.removesuffix("\n").split("\n") if text else []
    for table, values in wanted.items():
        held = _table(before, table)
        _set(lines, table, {key: value for key, value in values.items() if key not in held or held[key] != value})
    edited = "\n".join(lines) + "\n"
    try:
        after = tomllib.loads(edited)
    except tomllib.TOMLDecodeError:
        after = None
    if after is None or _owned(after, wanted) != wanted or _others(after, wanted) != _others(before, wanted):
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


def _set(lines: list[str], table: Table, values: Mapping[str, Any]) -> None:
    """Set the keys of one table in the file's lines: in its section when it has one, in a new one at the end.

    The top level's section is the lines before the first table.
    """
    if not values:
        return
    name = r"\s*\.\s*".join(rf"""(?:{part}|"{part}"|'{part}')""" for part in map(re.escape, table))
    header = re.compile(rf"\s*\[\s*{name}\s*\]\s*(?:#.*)?")
    start = next((index for index, line in enumerate(lines) if header.fullmatch(line)), None) if table else -1
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{'.'.join(table)}]")
        lines.extend(f"{key} = {_value(value)}" for key, value in values.items())
        return
    end = next((index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")), len(lines))
    added = []
    for key, value in values.items():
        setting = re.compile(rf"\s*{re.escape(key)}\s*=")
        at = next((index for index in range(start + 1, end) if setting.match(lines[index])), None)
        if at is None:
            added.append(f"{key} = {_value(value)}")
        else:
            lines[at] = f"{key} = {_value(value)}"
    # After the section's last line that holds something, so above the blank lines that end it.
    below = max((index for index in range(start + 1, end) if lines[index].strip()), default=start) + 1
    lines[below:below] = added
