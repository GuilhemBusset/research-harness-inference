"""The body allow-list for Anthropic Messages (system design, sections 6.1 and 6.2, step 4).

`MESSAGES` is what the gateway keeps of the body of `POST /v1/messages`, and `COUNT_TOKENS` what
it keeps of the body of `POST /v1/messages/count_tokens`. Each is given to
`rhi_gateway.body.BodyFilter`, which drops every field the list does not name, clamps the numbers
the list marks and refuses a media reference that is not inline data. Claude Code, in the
terminal, headless and through the Agent SDK, and the Anthropic SDK send this format.

**Where the fields come from.** Anthropic's published documentation, read on 2026-11-15. Every
entry below names the parameter or the object that holds it there:

- *Create a Message*, "Body parameters" (https://platform.claude.com/docs/en/api/messages/create):
  every entry that names no other page.
- *Count tokens in a Message*, "Body parameters"
  (https://platform.claude.com/docs/en/api/messages/count_tokens): which of them counting takes.
- *Create a Message* of the beta reference
  (https://platform.claude.com/docs/en/api/beta/messages/create), for the entries marked "beta":
  the blocks of a system message that add and remove a tool. The reference above has the system
  message (`SystemMessageParam`) and not yet these blocks.
- The release notes (https://platform.claude.com/docs/en/release-notes/overview), for the three
  sampling fields. The reference no longer lists them: the entry of August 20, 2026 says the SDK's
  version 1.0 removed "the `temperature`, `top_p`, and `top_k` parameters on Messages methods", and
  the entries of May 28 and October 7, 2026 say Anthropic's newer models refuse a value that is not
  the default. They are still fields of the format, clients written before that send them, and
  they are how a request sets an open model's sampling.

**What is listed** is the request a client can make of an engine: the conversation, the tools the
client defines and runs itself, and how to generate. That is the reference's body parameters, its
three message objects, these content blocks with every field the reference gives them (text,
image, document, search result, thinking, redacted thinking, tool use, tool result, and the tool
reference inside a tool result), and its `Tool` object.

**What is not listed**, and is therefore dropped like a field nobody has heard of:

- what speaks to Anthropic's own service, which this endpoint does not have: `container`,
  `diagnostics`, `inference_geo`, `service_tier` and `speed`; the tools Anthropic defines or runs
  (every kind of `tools` entry but `Tool`), and the blocks that carry their calls and results
  (`server_tool_use`, the `..._tool_result` blocks of web search, web fetch and code execution,
  `container_upload`, `browser_state`), and what only a citation of a web search result has
  (`CitationWebSearchResultLocationParam`: `encrypted_index` and `url`). Such a block keeps the fields it shares with the blocks
  above and loses the others. One whose `content` is an object is refused, by the shared filter's
  rule for an object that stands where an array is described; the engine refuses every one of
  them by its type `[E28]`;
- the rest of the beta reference: `context_management`, which Claude Code sends with every
  request and the engine does not read, `mcp_servers`, `fallbacks`, `compaction`, `thread`, a
  system message's `clear_at` and `output_config`, the references to an MCP server's tools;
- every field that is the engine's own and not the format's.

**What the request profile rewrites is kept as it is sent** (sections 6.1 and 6.2, step 4): the
reasoning effort (`output_config.effort`; the vLLM project's replay of Claude Code's traffic
records `{"effort":"high"}` there `[E28]`), a system message wherever it sits in `messages`, and a
document block. This list refuses none of them, whatever the pool's model does with them.

**Three entries are not like their neighbours:**

- `thinking` is kept whole, with any type and any field: the engine reads it from vLLM 0.33 on
  (section 6.1). Its `budget_tokens` is not clamped with the output limit.
- `citations` has two shapes in the reference, an array of locations on a text block and an
  object with a switch on a document or a search result. Both are described (`Shapes`), so a
  field the reference does not give a location or the switch is dropped like any other.
- `source` is described as the object of an image or a document. On a search result it is a
  string, which the filter keeps as it is.

Nothing here reads configuration, the environment or the network.
"""

from __future__ import annotations

from typing import Final

from rhi_gateway.body import KEEP, Fields, Items, Limit, Shapes, Spec

# `CacheControlEphemeral`: a cache breakpoint, on the request, a block or a tool.
_CACHE_CONTROL: Final = Fields({"type": KEEP, "ttl": KEEP})

# What an image or a document is read from. `url` and `file_id` are named so that the shared
# filter meets them: it refuses a URL that is not a `data:` URL, and any stored file.
_SOURCE: Final[dict[str, Spec]] = {
    "type": KEEP,  # every source
    "media_type": KEEP,  # `Base64ImageSource`, `Base64PDFSource`, `PlainTextSource`
    "data": KEEP,  # the same three
    "url": KEEP,  # `URLImageSource`, `URLPDFSource`
    "file_id": KEEP,  # `FileImageSource`, `FileDocumentSource`
}

# `citations`. On a document or a search result: `CitationsConfigParam`, an object. On a text
# block: an array of `TextCitationParam`, the places an earlier answer cited.
_CITATIONS: Final = Shapes(
    fields=Fields({"enabled": KEEP}),
    items=Items(
        Fields(
            {
                "type": KEEP,  # every location
                "cited_text": KEEP,  # every location
                # `CitationCharLocationParam`, `CitationPageLocationParam`, `CitationContentBlockLocationParam`
                "document_index": KEEP,
                "document_title": KEEP,
                "start_char_index": KEEP,  # `CitationCharLocationParam`
                "end_char_index": KEEP,
                "start_page_number": KEEP,  # `CitationPageLocationParam`
                "end_page_number": KEEP,
                # `CitationContentBlockLocationParam`, `CitationSearchResultLocationParam`
                "start_block_index": KEEP,
                "end_block_index": KEEP,
                # `CitationSearchResultLocationParam`
                "search_result_index": KEEP,
                "source": KEEP,
                "title": KEEP,
            }
        )
    ),
)

# `TextBlockParam`: in a message, in `system`, in a tool result, in a search result.
_TEXT: Final[dict[str, Spec]] = {
    "type": KEEP,  # every block
    "text": KEEP,
    "cache_control": _CACHE_CONTROL,  # every block but the two thinking blocks
    "citations": _CITATIONS,  # and `DocumentBlockParam`, `SearchResultBlockParam`
}

# The blocks of a document given as content (`ContentBlockSource`, `content`): text and image.
_IN_A_DOCUMENT: Final[dict[str, Spec]] = {
    **_TEXT,
    # `ImageBlockParam`
    "source": Fields(_SOURCE),
    "transformations": Fields({"oversized_image": KEEP}),  # `ImageTransformationsParam`
}

# The blocks of a tool result (`ToolResultBlockParam`, `content`): the two above, a document, a
# search result and a tool reference.
_IN_A_TOOL_RESULT: Final[dict[str, Spec]] = {
    **_IN_A_DOCUMENT,
    # `DocumentBlockParam`
    "source": Fields({**_SOURCE, "content": Items(Fields(_IN_A_DOCUMENT))}),  # and `ContentBlockSource`
    "context": KEEP,
    "title": KEEP,  # and `SearchResultBlockParam`
    # `SearchResultBlockParam`, whose `source` is a string
    "content": Items(Fields(_TEXT)),
    # `ToolReferenceBlockParam`
    "tool_name": KEEP,
}

# `tools`, `Tool`: a tool the client defines and runs.
_TOOL: Final = Fields(
    {
        "type": KEEP,  # "custom", or absent
        "name": KEEP,
        "description": KEEP,
        "input_schema": KEEP,  # a JSON Schema: data to the engine
        "cache_control": _CACHE_CONTROL,
        "strict": KEEP,
        "defer_loading": KEEP,
        "allowed_callers": KEEP,
        "eager_input_streaming": KEEP,
        "input_examples": KEEP,
    }
)

# The blocks of a message (`messages`, `ContentBlockParam`).
_IN_A_MESSAGE: Final[dict[str, Spec]] = {
    **_IN_A_TOOL_RESULT,
    # `ThinkingBlockParam`: the model's earlier reasoning, which Claude Code sends back
    "thinking": KEEP,
    "signature": KEEP,
    # `RedactedThinkingBlockParam`
    "data": KEEP,
    # `ToolUseBlockParam`
    "id": KEEP,
    "name": KEEP,
    "input": KEEP,  # the call's arguments: data to the engine
    "caller": Fields({"type": KEEP, "tool_id": KEEP}),  # `DirectCaller`, `ServerToolCaller`
    "toolset_name": KEEP,  # and `ToolResultBlockParam`
    # `ToolResultBlockParam`
    "tool_use_id": KEEP,
    "content": Items(Fields(_IN_A_TOOL_RESULT)),
    "is_error": KEEP,
    # Beta: `BetaRequestToolAdditionBlock` and `BetaRequestToolRemovalBlock`, in a system message.
    # `tool` is a `BetaToolChangeToolReference` (`type`, `name`) or, in an addition, a
    # `BetaToolChangeToolDefinitionParam` (`type`, `definition`), whose `definition` is "the same
    # object a `tools` entry holds". vLLM 0.33 takes both (section 4.2) `[E28]`.
    "tool": Fields({"type": KEEP, "name": KEEP, "definition": _TOOL}),
}

MESSAGES: Final = Fields(
    {
        "model": KEEP,  # `model`; the filter writes the pool's name over it
        "max_tokens": Limit.LARGEST_OUTPUT,  # `max_tokens`; Claude Code's main loop sends 32000 `[E15]`
        # `messages`: `UserMessageParam`, `AssistantMessageParam` and `SystemMessageParam`. A role is
        # kept whatever it is, so a system message stays at its place in the array.
        "messages": Items(Fields({"role": KEEP, "content": Items(Fields(_IN_A_MESSAGE))})),
        "system": Items(Fields(_TEXT)),  # `system`: a string, or an array of `TextBlockParam`
        "tools": Items(_TOOL),  # `tools`
        # `tool_choice`: `ToolChoiceAuto`, `ToolChoiceAny`, `ToolChoiceTool`, `ToolChoiceNone`
        "tool_choice": Fields({"type": KEEP, "name": KEEP, "disable_parallel_tool_use": KEEP}),
        "thinking": KEEP,  # `thinking`, `ThinkingConfigParam`
        # `output_config`, `OutputConfig`: `effort` is the format's reasoning-effort field, and
        # `format` a `JSONOutputFormat`, whose `schema` is a JSON Schema.
        "output_config": Fields({"effort": KEEP, "format": Fields({"type": KEEP, "schema": KEEP})}),
        "cache_control": _CACHE_CONTROL,  # `cache_control`
        "metadata": Fields({"user_id": KEEP}),  # `metadata`, `Metadata`
        "stop_sequences": Items(KEEP),  # `stop_sequences`
        "stream": KEEP,  # `stream`: true, false or absent, as it is sent
        # The release notes (above): the sampling fields.
        "temperature": KEEP,
        "top_k": KEEP,
        "top_p": KEEP,
    }
)

# What only generation uses: the fields above that are no parameter of *Count tokens in a Message*.
GENERATION_ONLY: Final = frozenset(
    {"max_tokens", "metadata", "stop_sequences", "stream", "temperature", "top_k", "top_p"}
)

# The list for `POST /v1/messages/count_tokens`: the same entries, so the same fields at every depth.
COUNT_TOKENS: Final = Fields({name: spec for name, spec in MESSAGES.fields.items() if name not in GENERATION_ONLY})
