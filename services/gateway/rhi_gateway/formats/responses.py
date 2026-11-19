"""The body allow-list for OpenAI Responses (system design, sections 6.1 and 6.2, step 4).

`RESPONSES` is what the gateway keeps of the body of `POST /v1/responses`. It is given to
`rhi_gateway.body.BodyFilter`, which drops every field the list does not name, clamps the numbers
the list marks and refuses a media reference that is not inline data. Codex CLI sends this format
and no other: Chat Completions is a configuration error there since February 2026 `[E15]`.

**Where the fields come from.** OpenAI's API reference, *Create a model response*, "Body
Parameters", read on 2026-11-15
(https://developers.openai.com/api/reference/resources/responses/methods/create). Every entry
below names the parameter, or the object of that page that holds the field: an input item is one
of the objects of `input`, a tool one of the objects of `tools`.

**What is listed** is the request a client can make of an engine: the conversation, the tools the
client runs itself, and how to generate. That is these input items with every field the reference
gives them: the three message objects (`EasyInputMessage`, `Message`, `ResponseOutputMessage`) with
their content parts, `Reasoning`, `FunctionCall`, `FunctionCallOutput`, `CustomToolCall`,
`CustomToolCallOutput`, and the calls and outputs of the tools OpenAI defines and the client runs
(`ComputerCall`, `ComputerCallOutput`, `LocalShellCall`, `LocalShellCallOutput`, `ShellCall`,
`ShellCallOutput`, `ApplyPatchCall`, `ApplyPatchCallOutput`); the tools a client runs (`Function`,
`Custom`, a `Namespace` of them, `Computer`, `ComputerUsePreview`, `LocalShell`, `Shell` and
`ApplyPatch`); the choice of a tool; and the parameters of sampling, length, reasoning, output
format and streaming. Codex sends its conversation, its turns of reasoning and its tool calls as
these items, its shell and its other tools as `Function`, and its patch tool as `Custom`. A
`CodeInterpreterCall` of an earlier turn keeps its code and its outputs, logs and images.

**What is not listed**, and is therefore dropped like a field nobody has heard of:

- what speaks to OpenAI's own service, which this endpoint does not have: its stored responses and
  conversations (`store`, `previous_response_id`, `conversation`, `background`, `metadata`, and
  the `ItemReference` item, an ID of something OpenAI stored), its stored prompts (`prompt`), its
  moderation (`moderation`), its processing tiers and modes (`service_tier`, `reasoning.mode`),
  its abuse detection and programs (`safety_identifier`, `user`, `access_programs`), its context
  management and image settings (`context_management`, `image`), its prompt cache
  (`prompt_cache_key`, `prompt_cache_options`, `prompt_cache_retention`, and a content part's
  `prompt_cache_breakpoint`; here the cache is the engine's, under the salt the gateway writes);
- the settings of the tools OpenAI runs itself (file search, web search, MCP, code interpreter,
  image generation, tool search, programmatic tool calling: such a `tools` entry keeps its `type`
  and nothing else), `max_tool_calls`, which counts their calls, and what serves only them on a
  client's tool: its programmatic callers and its tool search (`allowed_callers`,
  `defer_loading`, `output_schema`), and the container OpenAI creates for a `Shell`
  (`ContainerAuto` keeps its `type` and nothing else). The items that carry the calls and results of OpenAI's tools (web
  search, file search, MCP, tool search and the others) keep the fields they share with the items
  above and lose the rest;
- every field that is the engine's own and not the format's, and every field a client adds of its
  own (Codex's `client_metadata`, its `stream_options.reasoning_summary_delivery`).

**What the request profile rewrites is kept as it is sent** (sections 6.1 and 6.2, step 4): the
reasoning effort, `reasoning.effort`, with any value, and a `system` or a `developer` message
wherever it sits in `input`. One description serves every input item, so no item is moved, merged
or refused for its role, its type or its place. A `Reasoning` item of an earlier turn keeps its
summary, its content and its encrypted content.

**Media.** An image part is described down to `image_url` and `file_id`, and a file part down to
`file_url` and `file_id`, so the shared filter meets them: an image or a file given inline (a
`data:` URL, `file_data`) is kept whole, any other URL is refused, and so is a file that is stored
and not sent. The reference allows an image in a message's `content`, in the `output` of a
function call or of a custom tool call (an array of parts; one description of a part serves all
three), in the `output` of a computer call (one screenshot object, `image_url` and `file_id`) and
in the `outputs` of a code interpreter call (`url`). A citation of an earlier answer
(`annotations`) is described with its fields too, so one that names a web address or a stored file
is refused by the same rule; the engine writes no citation.

Nothing here reads configuration, the environment or the network.
"""

from __future__ import annotations

from typing import Final

from rhi_gateway.body import KEEP, Fields, Items, Limit, Shapes, Spec

# `Direct` or `Program`: who called a tool, on the calls of a client's tools and their outputs.
_CALLER: Final = Fields({"type": KEEP, "caller_id": KEEP})

# `ResponseOutputText.logprobs`, and `top_logprobs` inside each of them.
_LOGPROB: Final[dict[str, Spec]] = {"token": KEEP, "bytes": KEEP, "logprob": KEEP}

# The parts of an item's `content`, and of the `output` of `FunctionCallOutput` and
# `CustomToolCallOutput`. A message from the client takes text, image and file parts, an answer
# of the model's takes output text and refusal parts, and a reasoning item takes reasoning text.
_PART: Final[dict[str, Spec]] = {
    "type": KEEP,  # every part
    # `ResponseInputText`, `ResponseInputTextContent`, `ResponseOutputText`, and a reasoning
    # item's `content` ("reasoning_text")
    "text": KEEP,
    # `ResponseInputImage`, `ResponseInputImageContent`. The shared filter refuses an `image_url`
    # that is not a `data:` URL, and a `file_id` that is not null.
    "image_url": KEEP,
    "detail": KEEP,  # `ResponseInputImage`, and `ResponseInputFile`
    "file_id": KEEP,
    # `ResponseInputFile`, `ResponseInputFileContent`. The shared filter refuses a `file_url`
    # that is not a `data:` URL.
    "file_data": KEEP,
    "file_url": KEEP,
    "filename": KEEP,
    # `ResponseOutputText.annotations`: `FileCitation`, `URLCitation`, `ContainerFileCitation`
    # and `FilePath`
    "annotations": Items(
        Fields(
            {
                "type": KEEP,
                "file_id": KEEP,
                "filename": KEEP,
                "index": KEEP,
                "url": KEEP,
                "title": KEEP,
                "start_index": KEEP,
                "end_index": KEEP,
                "container_id": KEEP,
            }
        )
    ),
    "logprobs": Items(Fields({**_LOGPROB, "top_logprobs": Items(Fields(_LOGPROB))})),  # `ResponseOutputText`
    "refusal": KEEP,  # `ResponseOutputRefusal`
}

# `ShellCallOutput.output`: what a shell command printed, and how it ended (`Timeout` or `Exit`).
_SHELL_OUTPUT: Final[dict[str, Spec]] = {
    "stdout": KEEP,
    "stderr": KEEP,
    "outcome": Fields({"type": KEEP, "exit_code": KEEP}),
}

# `ComputerAction`: one action of `ComputerCall` (`Click`, `DoubleClick`, `Drag`, `Keypress`,
# `Move`, `Screenshot`, `Scroll`, `Type`, `Wait`).
_COMPUTER_ACTION: Final[dict[str, Spec]] = {
    "type": KEEP,
    "button": KEEP,
    "x": KEEP,
    "y": KEEP,
    "keys": KEEP,
    "path": Items(Fields({"x": KEEP, "y": KEEP})),  # `Drag`
    "scroll_x": KEEP,
    "scroll_y": KEEP,
    "text": KEEP,  # `Type`
}

# The `environment` of a `Shell` tool and of a `ShellCall`: `LocalEnvironment` (its skills, as
# `LocalSkill`) and `ContainerReference`. `ContainerAuto`, a container OpenAI creates, keeps its
# `type`. On `ComputerUsePreview` the environment is a string, kept as it is.
_ENVIRONMENT: Final = Fields(
    {
        "type": KEEP,
        "skills": Items(Fields({"description": KEEP, "name": KEEP, "path": KEEP})),
        "container_id": KEEP,
    }
)

# A safety check of `ComputerCall.pending_safety_checks` and `ComputerCallOutput.acknowledged_safety_checks`.
_SAFETY_CHECKS: Final = Items(Fields({"id": KEEP, "code": KEEP, "message": KEEP}))

# `input`, `ResponseInputItem`: the input items a client sends. A role is kept whatever it is, and
# an item is kept where it is.
_ITEM: Final = Fields(
    {
        "type": KEEP,  # every item
        "id": KEEP,  # every item but `EasyInputMessage`
        "status": KEEP,  # every item but `EasyInputMessage` and `CustomToolCallOutput`
        # `EasyInputMessage`, `Message`, `ResponseOutputMessage`: "user", "assistant", "system"
        # or "developer"
        "role": KEEP,
        # The three message objects: a string or an array of parts; `Reasoning`: its reasoning text
        "content": Items(Fields(_PART)),
        "phase": KEEP,  # `EasyInputMessage`, `ResponseOutputMessage`
        # `Reasoning`: an item of an earlier turn, as the model gave it
        "summary": Items(Fields({"type": KEEP, "text": KEEP})),
        "encrypted_content": KEEP,
        # every call of a client's tool, and its output
        "call_id": KEEP,
        "name": KEEP,  # `FunctionCall`, `CustomToolCall`, `FunctionCallOutput`
        "namespace": KEEP,
        "caller": _CALLER,
        "arguments": KEEP,  # `FunctionCall`: a string of JSON
        "input": KEEP,  # `CustomToolCall`: the text the model wrote for the tool
        "async": KEEP,  # `FunctionCall`, `CustomToolCall`: whether its result may come in a later request
        # `FunctionCallOutput`, `CustomToolCallOutput`: a string or an array of parts;
        # `ShellCallOutput`: an array of what each command printed; `LocalShellCallOutput`,
        # `ApplyPatchCallOutput`: a string; `ComputerCallOutput`: one
        # `ResponseComputerToolCallOutputScreenshot` (`type`, `image_url`, `file_id`)
        "output": Shapes(Fields(_PART), Items(Fields({**_PART, **_SHELL_OUTPUT}))),
        # `LocalShellCall.action` (`command`, `env`, `timeout_ms`, `user`, `working_directory`),
        # `ShellCall.action` (`commands`, `max_output_length`, `timeout_ms`), `ComputerCall.action`
        "action": Fields(
            {
                **_COMPUTER_ACTION,
                "command": KEEP,
                "env": KEEP,
                "timeout_ms": KEEP,
                "user": KEEP,
                "working_directory": KEEP,
                "commands": KEEP,
                "max_output_length": KEEP,
            }
        ),
        # `ComputerCall.actions`, `ComputerActionList`: its actions in one batch
        "actions": Shapes(Fields(_COMPUTER_ACTION), Items(Fields(_COMPUTER_ACTION))),
        "pending_safety_checks": _SAFETY_CHECKS,  # `ComputerCall`
        "acknowledged_safety_checks": _SAFETY_CHECKS,  # `ComputerCallOutput`
        "environment": _ENVIRONMENT,  # `ShellCall`
        "max_output_length": KEEP,  # `ShellCallOutput`
        # `ApplyPatchCall.operation`: `CreateFile`, `DeleteFile` or `UpdateFile`
        "operation": Fields({"type": KEEP, "path": KEEP, "diff": KEEP}),
        # `CodeInterpreterCall` of an earlier turn: its code and its outputs, `Logs` or `Image`.
        # The shared filter refuses an image `url` that is not a `data:` URL.
        "code": KEEP,
        "container_id": KEEP,
        "outputs": Items(Fields({"type": KEEP, "logs": KEEP, "url": KEEP})),
    }
)

# `Function` and `Custom`: the tools a client defines and runs, alone or in a `Namespace`.
_CLIENT_TOOL: Final[dict[str, Spec]] = {
    "type": KEEP,  # "function" or "custom"
    "name": KEEP,
    "description": KEEP,
    "parameters": KEEP,  # `Function`: a JSON Schema, data to the engine
    "strict": KEEP,  # `Function`
    "async": KEEP,  # `Function`, `Custom`: whether the tool's result may come in a later request
    # `Custom`, `CustomToolInputFormat`: its `Text` or its `Grammar`
    "format": Fields({"type": KEEP, "definition": KEEP, "syntax": KEEP}),
}

# `tools`, `Tool`: a tool the client runs. Besides `Function` and `Custom`: a `Namespace` of them
# (`name`, `description`, `tools`), and the tools OpenAI defines and the client runs, `Computer`
# (its `type`), `ComputerUsePreview` (`display_width`, `display_height`, `environment`),
# `LocalShell` and `ApplyPatch` (their `type`) and `Shell` (`environment`).
_TOOL: Final = Fields(
    {
        **_CLIENT_TOOL,
        "tools": Items(Fields(_CLIENT_TOOL)),  # `Namespace`
        "display_width": KEEP,
        "display_height": KEEP,
        "environment": _ENVIRONMENT,
    }
)

RESPONSES: Final = Fields(
    {
        "model": KEEP,  # `model`; the filter writes the pool's name over it
        "input": Items(_ITEM),  # `input`: a string, or an array of input items
        "instructions": KEEP,  # `instructions`
        "tools": Items(_TOOL),  # `tools`
        # `tool_choice`: `ToolChoiceOptions` ("none", "auto" or "required"), or `ToolChoiceAllowed`
        # (`mode`, `tools`), `ToolChoiceFunction` or `ToolChoiceCustom` (`name`), or `ToolChoiceTypes`
        # (`type`). The reference types the allowed tools as maps and shows tool definitions: they
        # are kept as `tools` entries are.
        "tool_choice": Fields({"type": KEEP, "name": KEEP, "mode": KEEP, "tools": Items(_TOOL)}),
        "parallel_tool_calls": KEEP,  # `parallel_tool_calls`
        "max_output_tokens": Limit.LARGEST_OUTPUT,  # `max_output_tokens`: the output limit
        # `reasoning`, `Reasoning`. `effort` is the format's reasoning-effort field, kept whatever it
        # holds; `generate_summary` is deprecated, replaced by `summary`.
        "reasoning": Fields({"effort": KEEP, "summary": KEEP, "generate_summary": KEEP, "context": KEEP}),
        # `text`, `ResponseTextConfig`: `format` is `ResponseFormatText`, `ResponseFormatJSONObject` or
        # `ResponseFormatTextJSONSchemaConfig`, whose `schema` is a JSON Schema
        "text": Fields(
            {
                "format": Fields({"type": KEEP, "name": KEEP, "description": KEEP, "schema": KEEP, "strict": KEEP}),
                "verbosity": KEEP,
            }
        ),
        "include": Items(KEEP),  # `include`, `ResponseIncludable`: names of what the answer carries
        "truncation": KEEP,  # `truncation`
        "stream": KEEP,  # `stream`
        "stream_options": Fields({"include_obfuscation": KEEP}),  # `stream_options`
        # The sampling parameters, each under its own name in the reference.
        "temperature": KEEP,
        "top_p": KEEP,
        "top_logprobs": KEEP,
    }
)
