"""The body allow-list for OpenAI Chat Completions (system design, sections 6.1 and 6.2, steps 4 and 6).

`CHAT_COMPLETIONS` is what the gateway keeps of the body of `POST /v1/chat/completions`.
`ChatBodyFilter` gives it to `rhi_gateway.body.BodyFilter`, which drops every field the list does
not name, clamps the numbers the list marks and refuses a media reference that is not inline data;
then it asks the engine for the usage of a stream (below). Programs written with the OpenAI SDK,
aider and opencode send this format.

**Where the fields come from.** OpenAI's API reference, *Create chat completion*, "Body
Parameters", read on 2026-11-15
(https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Every entry below names the parameter, or the object of that page that holds the field.

**What is listed** is the request a client can make of an engine: the conversation, the tools the
client defines and runs itself, and how to generate. That is the reference's six message objects
with their content parts and tool calls, its two tool objects, the choice of a tool, the two
deprecated parameters that tools replaced, and the parameters of sampling, length, reasoning,
output format and streaming.

**What is not listed**, and is therefore dropped like a field nobody has heard of:

- what speaks to OpenAI's own service, which this endpoint does not have: its stored completions
  (`store`, `metadata`), its moderation models (`moderation`), its processing tiers
  (`service_tier`), its web search (`web_search_options`), its abuse detection
  (`safety_identifier`, `user`), its prompt cache (`prompt_cache_key`, `prompt_cache_options`,
  `prompt_cache_retention`, and a content part's `prompt_cache_breakpoint`; here the cache is the
  engine's, under the salt the gateway writes), and its spoken answers (`audio` and `modalities`,
  and an assistant message's `audio`, the ID of an answer OpenAI stored);
- every field that is the engine's own and not the format's, such as `chat_template_kwargs`,
  `top_k`, or a message's `reasoning`.

**What the request profile rewrites is kept as it is sent** (sections 6.1 and 6.2, step 4):
`reasoning_effort` with any value, and a `system` or a `developer` message wherever it sits in
`messages`. One description serves every role, so no message is moved, merged or refused for its
role or its place.

**Media.** An image part is described down to `image_url.url` and a file part down to
`file.file_id`, so the shared filter meets both: an image given as a `data:` URL is kept whole,
any other URL is refused, and so is a file that is stored and not sent (`file_data`). Audio input
is base64 in the format and has no reference.

**Usage on a stream** (section 6.2, step 6). This format reports a stream's usage only in a final
chunk the request must ask for, with `stream_options.include_usage`. The gateway settles each
request to that usage, so `ChatBodyFilter` writes the option as true into every request whose
`stream` is true, whatever the client sent there, and keeps the client's other stream option. It
does so before the pool's request profile runs. What it returns says whether the client's own
body asked: only then does the client get the chunk (`rhi_gateway.usage.chat`). A request that is
not a stream is forwarded as the list leaves it, and is recorded as not having asked.

A `stream` that is neither a boolean nor null is refused. The engine reads some numbers and
strings as true, and a stream the gateway did not take for one would carry no usage.

Nothing here reads configuration, the environment or the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from rhi_common.causes import Cause
from rhi_common.pool_config import Pool
from rhi_gateway.body import (
    KEEP,
    MAX_BODY_BYTES,
    MODEL_FIELD,
    BodyFilter,
    BodySource,
    Fields,
    Forwarded,
    Items,
    Limit,
    Rewrite,
    Spec,
    read_body,
)
from rhi_gateway.errors import GatewayError

STREAM_FIELD = "stream"
STREAM_OPTIONS_FIELD = "stream_options"
INCLUDE_USAGE_FIELD = "include_usage"

# `ChatCompletionContentPartText`: a part of every message that takes parts, and of `prediction`.
_TEXT_PART: Final[dict[str, Spec]] = {"type": KEEP, "text": KEEP}

# The parts of a message's `content`. The reference gives a user message all of them (text,
# image, audio and file), an assistant message text and refusal, and the others text.
_PART: Final[dict[str, Spec]] = {
    **_TEXT_PART,
    # `ChatCompletionContentPartImage`. The shared filter refuses a `url` that is not a `data:` URL.
    "image_url": Fields({"url": KEEP, "detail": KEEP}),
    "input_audio": Fields({"data": KEEP, "format": KEEP}),  # `ChatCompletionContentPartInputAudio`
    # `FileContentPart`. The shared filter refuses a `file_id` that is not null.
    "file": Fields({"file_data": KEEP, "file_id": KEEP, "filename": KEEP}),
    "refusal": KEEP,  # `ChatCompletionContentPartRefusal`
}

# `messages`, `ChatCompletionMessageParam`: the developer, system, user, assistant, tool and
# function message objects. A role is kept whatever it is, and a message is kept where it is.
_MESSAGE: Final = Fields(
    {
        "role": KEEP,  # every message
        "content": Items(Fields(_PART)),  # every message: a string, an array of parts, or null
        "name": KEEP,  # every message but the tool message
        # `ChatCompletionAssistantMessageParam`
        "refusal": KEEP,
        "tool_calls": Items(
            Fields(
                {
                    "id": KEEP,  # both kinds of call
                    "type": KEEP,
                    # `ChatCompletionMessageFunctionToolCall`; `arguments` is a string of JSON
                    "function": Fields({"name": KEEP, "arguments": KEEP}),
                    "custom": Fields({"name": KEEP, "input": KEEP}),  # `ChatCompletionMessageCustomToolCall`
                }
            )
        ),
        "function_call": Fields({"name": KEEP, "arguments": KEEP}),  # deprecated, replaced by `tool_calls`
        "tool_call_id": KEEP,  # `ChatCompletionToolMessageParam`
    }
)

# `tools`, `ChatCompletionTool`: a tool the client defines and runs.
_TOOL: Final = Fields(
    {
        "type": KEEP,  # "function" or "custom"
        # `ChatCompletionFunctionTool`, `FunctionDefinition`; `parameters` is a JSON Schema: data to the engine
        "function": Fields({"name": KEEP, "description": KEEP, "parameters": KEEP, "strict": KEEP}),
        # `ChatCompletionCustomTool`; `format` is its `Text` or its `Grammar`
        "custom": Fields(
            {
                "name": KEEP,
                "description": KEEP,
                "format": Fields({"type": KEEP, "grammar": Fields({"definition": KEEP, "syntax": KEEP})}),
            }
        ),
    }
)

CHAT_COMPLETIONS: Final = Fields(
    {
        "model": KEEP,  # `model`; the filter writes the pool's name over it
        "messages": Items(_MESSAGE),  # `messages`
        "tools": Items(_TOOL),  # `tools`
        # `tool_choice`, `ChatCompletionToolChoiceOption`: "none", "auto" or "required", or
        # `ChatCompletionNamedToolChoice` (`function`), `ChatCompletionNamedToolChoiceCustom`
        # (`custom`) or `ChatCompletionAllowedToolChoice` (`allowed_tools`). The reference types
        # the allowed tools as maps and shows tool definitions: they are kept as `tools` entries are.
        "tool_choice": Fields(
            {
                "type": KEEP,
                "function": Fields({"name": KEEP}),
                "custom": Fields({"name": KEEP}),
                "allowed_tools": Fields({"mode": KEEP, "tools": Items(_TOOL)}),
            }
        ),
        "parallel_tool_calls": KEEP,  # `parallel_tool_calls`
        # `function_call`, `ChatCompletionFunctionCallOption`, and `functions`: deprecated, replaced
        # by `tool_choice` and `tools`
        "function_call": Fields({"name": KEEP}),
        "functions": Items(Fields({"name": KEEP, "description": KEEP, "parameters": KEEP})),
        # `max_completion_tokens`, and `max_tokens`, which it replaced: both are the output limit
        "max_completion_tokens": Limit.LARGEST_OUTPUT,
        "max_tokens": Limit.LARGEST_OUTPUT,
        "n": KEEP,  # `n`
        "stop": Items(KEEP),  # `stop`: a string, or an array of strings
        # `reasoning_effort`, `ReasoningEffort`: the format's reasoning-effort field, kept whatever it holds
        "reasoning_effort": KEEP,
        "verbosity": KEEP,  # `verbosity`
        # `response_format`: `ResponseFormatText`, `ResponseFormatJSONObject` or
        # `ResponseFormatJSONSchema`, whose `schema` is a JSON Schema
        "response_format": Fields(
            {
                "type": KEEP,
                "json_schema": Fields({"name": KEEP, "description": KEEP, "schema": KEEP, "strict": KEEP}),
            }
        ),
        # `prediction`, `ChatCompletionPredictionContent`: the text the answer is expected to repeat
        "prediction": Fields({"type": KEEP, "content": Items(Fields(_TEXT_PART))}),
        "stream": KEEP,  # `stream`: true, false, null or absent, as it is sent
        # `stream_options`, `ChatCompletionStreamOptions`; `ChatBodyFilter` writes `include_usage`
        "stream_options": Fields({INCLUDE_USAGE_FIELD: KEEP, "include_obfuscation": KEEP}),
        # The sampling parameters, each under its own name in the reference.
        "temperature": KEEP,
        "top_p": KEEP,
        "frequency_penalty": KEEP,
        "presence_penalty": KEEP,
        "logit_bias": KEEP,  # a map from token IDs to a bias: data to the engine
        "seed": KEEP,
        "logprobs": KEEP,
        "top_logprobs": KEEP,
    }
)


@dataclass(frozen=True)
class ChatForwarded(Forwarded):
    """What the gateway sends for a Chat Completions request, and what it must remember of it."""

    client_asked_for_usage: bool
    """Whether the client's own body asked for a stream's usage chunk. Only then does it get the chunk."""


def _ask_for_usage(body: dict[str, Any]) -> bool:
    """Write the usage option into a stream's body, and say whether the client had asked for it."""
    stream = body.get(STREAM_FIELD)
    if stream is not None and not isinstance(stream, bool):
        raise GatewayError(Cause.REQUEST_REFUSED, "the stream flag is not a boolean")
    if not stream:
        return False
    sent = body.get(STREAM_OPTIONS_FIELD)
    options = dict(sent) if isinstance(sent, dict) else {}
    asked = options.get(INCLUDE_USAGE_FIELD) is True
    options[INCLUDE_USAGE_FIELD] = True
    body[STREAM_OPTIONS_FIELD] = options
    return asked


@dataclass(frozen=True)
class ChatBodyFilter:
    """The filter of Chat Completions: the shared filter with this format's list, then the usage option.

    The fields are those of `rhi_gateway.body.BodyFilter`, and mean the same.
    """

    pools: tuple[Pool, ...]
    default_pool: Pool | None = None
    profiles: Mapping[str, Rewrite] = field(default_factory=dict)
    maximum: int = MAX_BODY_BYTES

    async def read(self, source: BodySource, *, student: str, request_id: str) -> ChatForwarded:
        """Read a request's body, never past the maximum, and give what to forward for it."""
        return self.apply(await read_body(source, self.maximum), student=student, request_id=request_id)

    def apply(self, raw: bytes, *, student: str, request_id: str) -> ChatForwarded:
        """What to forward for a body, and whether the client asked for a stream's usage."""
        asked: list[bool] = []

        # Given to the shared filter as every pool's profile: it runs where a profile does, on the
        # filtered body, whose `model` is by then the pool's name.
        def rewrite(body: dict[str, Any]) -> dict[str, Any]:
            asked.append(_ask_for_usage(body))
            profile = self.profiles.get(body[MODEL_FIELD])
            return body if profile is None else profile(body)

        names = {pool.name for pool in (*self.pools, self.default_pool) if pool is not None}
        shared = BodyFilter(
            CHAT_COMPLETIONS, self.pools, self.default_pool, dict.fromkeys(names, rewrite), self.maximum
        )
        forwarded = shared.apply(raw, student=student, request_id=request_id)
        return ChatForwarded(forwarded.pool, forwarded.body, client_asked_for_usage=asked[0])
