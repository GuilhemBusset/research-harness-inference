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
client defines and runs itself, and how to generate. That is these input items with every field
the reference gives them: the three message objects (`EasyInputMessage`, `Message`,
`ResponseOutputMessage`) with their content parts, `Reasoning`, `FunctionCall`,
`FunctionCallOutput`, `CustomToolCall` and `CustomToolCallOutput`; the two tools a client runs
(`Function`, `Custom`); the choice of a tool; and the parameters of sampling, length, reasoning,
output format and streaming. Codex sends its conversation, its turns of reasoning and its tool
calls as these items, its shell and its other tools as `Function`, and its patch tool as `Custom`.

**What is not listed**, and is therefore dropped like a field nobody has heard of:

- what speaks to OpenAI's own service, which this endpoint does not have: its stored responses and
  conversations (`store`, `previous_response_id`, `conversation`, `background`, `metadata`, and
  the `ItemReference` item, an ID of something OpenAI stored), its stored prompts (`prompt`), its
  moderation (`moderation`), its processing tiers and modes (`service_tier`, `reasoning.mode`),
  its abuse detection and programs (`safety_identifier`, `user`, `access_programs`), its context
  management and image settings (`context_management`, `image`), its prompt cache
  (`prompt_cache_key`, `prompt_cache_options`, `prompt_cache_retention`, and a content part's
  `prompt_cache_breakpoint`; here the cache is the engine's, under the salt the gateway writes);
- the tools OpenAI defines or runs (every kind of `tools` entry but `Function` and `Custom`, and
  `max_tool_calls`, which counts their calls), and what serves only them on a client's tool: its
  programmatic callers and its tool search (`allowed_callers`, `defer_loading`, `output_schema`).
  The items that carry their calls and results (web search, file search, code interpreter,
  computer, shell, MCP, tool search and the others) keep the fields they share with the items
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
and not sent. The reference allows such parts in a message's `content` and in the `output` of a
function call or of a custom tool call; one description of a part serves all three. A citation of
an earlier answer (`annotations`) is described with its fields too, so one that names a web
address or a stored file is refused by the same rule; the engine writes no citation.

Nothing here reads configuration, the environment or the network.
"""

from __future__ import annotations

from typing import Final

from rhi_gateway.body import KEEP, Fields, Items, Limit, Spec

# `Direct` or `Program`: who called a tool, on `FunctionCall`, `FunctionCallOutput` and `CustomToolCall`.
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
        # `FunctionCall`, `CustomToolCall`, and their outputs
        "call_id": KEEP,
        "name": KEEP,  # `FunctionCall`, `CustomToolCall`, `FunctionCallOutput`
        "namespace": KEEP,
        "caller": _CALLER,
        "arguments": KEEP,  # `FunctionCall`: a string of JSON
        "input": KEEP,  # `CustomToolCall`: the text the model wrote for the tool
        # `FunctionCallOutput`, `CustomToolCallOutput`: a string or an array of parts
        "output": Items(Fields(_PART)),
    }
)

# `tools`, `Tool`: a tool the client defines and runs.
_TOOL: Final = Fields(
    {
        "type": KEEP,  # "function" or "custom"
        "name": KEEP,  # `Function`, `Custom`
        "description": KEEP,
        "parameters": KEEP,  # `Function`: a JSON Schema, data to the engine
        "strict": KEEP,  # `Function`
        # `Custom`, `CustomToolInputFormat`: its `Text` or its `Grammar`
        "format": Fields({"type": KEEP, "definition": KEEP, "syntax": KEEP}),
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
