"""The SDK tool loop: the fifth student harness, a script as a student writes one
(docs/design/agent-session-evaluation.md, "Course quality and the grader").

It is the SDK script of spike S3 (`spikes/s3_harness_task.py`, `sdk_script`): Chat Completions
calls with the OpenAI SDK and four tools on the working folder (list, read and write a file, run a
Python file), until the model answers without a tool call. As the student guide's script does, it
asks `rhi token` for its key when it starts and names the pool's model; the SDK reads the
endpoint's address from `OPENAI_BASE_URL`.

    python sdk_tool_loop.py MODEL PROMPT

`evaluate.adapters` copies this file into a trial's home and runs it there, so it imports the
standard library and the SDK only.

**A tool that fails is the model's to handle.** A file that is not there, a path outside the
folder, a program that exits in failure: the model is given what happened as the tool's result and
goes on. **A call that cannot be carried out at all** (a call of another kind than a function's, a
tool the loop does not have, arguments that are no JSON object or that lack a text the tool
requires) is answered the same way, so that the model can write it again; after `MALFORMED_CALLS`
such turns in a row the loop stops, and exits `MALFORMED_EXIT`: the trial did not complete because
of the calls. It exits 0 with the model's answer, and 1 when the SDK raised or `TURNS` calls were
made.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import openai

# The most model calls one run makes, the seconds a Python file it runs may take, and the most of
# what that file printed the model is given.
TURNS = 60
RUN_SECONDS = 5 * 60
TOOL_OUTPUT = 20_000
# How many turns in a row may hold a call that cannot be carried out, and the exit status then.
MALFORMED_CALLS = 3
MALFORMED_EXIT = 3

_PATH = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
_WRITE = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["path", "content"],
}
_DESCRIPTIONS = {
    "list_files": ("The files of the working folder.", {"type": "object", "properties": {}}),
    "read_file": ("The text of a file of the working folder.", _PATH),
    "write_file": ("Writes a file of the working folder, replacing its text.", _WRITE),
    "run_python": ("Runs a Python file of the working folder; its exit status and what it printed.", _PATH),
}
TOOLS: list[Any] = [
    {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}
    for name, (description, parameters) in _DESCRIPTIONS.items()
]
# What the model is given for a call that cannot be carried out.
REFUSAL = "error: not a call of one of the tools: an unknown tool, or arguments that are not the tool's JSON object"


class Malformed(Exception):
    """A call the loop cannot carry out: it is no function's call, it names no tool of the loop,
    or its arguments are no JSON object that holds each text the tool requires."""


def use_tool(folder: Path, name: str, arguments: str) -> str:
    """What one of the tools gives back, as text for the model. `Malformed` for a call that is none."""
    try:
        given = json.loads(arguments or "{}")
    except ValueError:
        raise Malformed from None
    if name not in _DESCRIPTIONS or not isinstance(given, dict):
        raise Malformed
    # Every argument a tool requires is a text.
    if any(not isinstance(given.get(required), str) for required in _DESCRIPTIONS[name][1].get("required", [])):
        raise Malformed
    try:
        if name == "list_files":
            return "\n".join(sorted(one.relative_to(folder).as_posix() for one in folder.rglob("*") if one.is_file()))
        path = (folder / given["path"]).resolve()
        if not path.is_relative_to(folder.resolve()):
            return "refused: the path is outside the working folder"
        if name == "read_file":
            return path.read_text(encoding="utf-8")
        if name == "write_file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(given["content"], encoding="utf-8")
            return "written"
        ran = subprocess.run(
            [sys.executable, str(path)], cwd=folder, capture_output=True, text=True, timeout=RUN_SECONDS, check=False
        )
        return f"exit status {ran.returncode}\n{(ran.stdout + ran.stderr)[-TOOL_OUTPUT:]}"
    except Exception as error:  # noqa: BLE001 - what went wrong is told to the model, as a tool's error
        return f"error: {type(error).__name__}"


def key() -> str:
    """The key, from `rhi token`, as the student guide's script reads it."""
    return subprocess.run(["rhi", "token"], capture_output=True, text=True, check=True).stdout.strip()


def loop(client: Any, model: str, prompt: str, folder: Path) -> int:
    """Calls the model with the tools until it answers without a tool call; the exit status."""
    messages: list[Any] = [{"role": "user", "content": prompt}]
    malformed = 0
    for _ in range(TURNS):
        message = client.chat.completions.create(model=model, messages=messages, tools=TOOLS).choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        calls = message.tool_calls or []
        if not calls:
            print(message.content or "")
            return 0
        refused = False
        for call in calls:
            # A call of another kind (a custom tool's) has no function: the loop declared none such.
            function = getattr(call, "function", None)
            try:
                if function is None:
                    raise Malformed
                result = use_tool(folder, function.name, function.arguments)
            except Malformed:
                refused, result = True, REFUSAL
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        malformed = malformed + 1 if refused else 0
        if malformed >= MALFORMED_CALLS:
            return MALFORMED_EXIT
    return 1


def main(arguments: list[str]) -> int:
    model, prompt = arguments
    try:
        return loop(openai.OpenAI(api_key=key()), model, prompt, Path.cwd())
    except Exception as error:  # noqa: BLE001 - what the SDK raised is what a student's program shows
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
