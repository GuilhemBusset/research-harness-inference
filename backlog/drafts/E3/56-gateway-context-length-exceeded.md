+++
id = "E3-56"
title = "Gateway: an engine's refusal of a prompt longer than the pool's context is a 400 context_length_exceeded that Claude Code compacts on, not a 502 engine_error it retries"
labels = ["size:M", "kind:app"]
epic = 5
+++

## Context

In spike S3's first run on the coursework pool, the Claude Code session of the compaction step
grew past the engine's 262,144-token context. vLLM refused the next turn, the gateway answered it
502 `engine_error`, and Claude Code took the 502 for a transient failure: it sent the same request
11 times (its default of 10 retries, with exponential backoff from one second to about 38), all
answered 502 `engine_error`, and exited 1 without compacting.

What vLLM 0.33.0 answered is a 400. Its log reads "Error in create_messages" at
`api_router.py:76`, the exception handler of `create_messages` in
`vllm/entrypoints/anthropic/api_router.py`, which renders the exception with
`create_error_response` (`vllm/entrypoints/serve/exception_handling/error_response.py`): a
`VLLMValidationError` is status 400, and `translate_error_type` names 400 `invalid_request_error`.
The two bodies vLLM 0.33.0 sends for this refusal, which the tests use as they are:

- on `/v1/messages`: `{"type": "error", "error": {"type": "invalid_request_error", "message": "This model's maximum context length is 262144 tokens. However, your prompt contains at least 262145 input tokens, which exceeds this limit even without any output tokens. Please reduce the length of the input prompt. (parameter=input_tokens, value=262145)"}}`;
- on the OpenAI routes, the same message in `{"error": {"message": ..., "type": "BadRequestError", "param": "input_tokens", "code": 400}}`.

On the Messages route vLLM drops the `param`, so the message's words are the only mark of this
refusal: an engine answer of status 400 whose error message contains "maximum context length" is
it. Every other answer that is not a success stays `engine_error`.

The gateway turns every engine answer that is not a success into 502 `engine_error`
(`services/gateway/rhi_gateway/proxy.py`, `Proxy.forward`, the branch on `response.is_success`).
Anthropic's API answers a prompt longer than the model's context with a 400
`invalid_request_error` whose message begins "prompt is too long"
(platform.claude.com/docs/en/build-with-claude/context-windows), and Claude Code's error reference
(code.claude.com/docs/en/errors, "Prompt is too long") says it recognises that condition by its
words, as it does Bedrock's own wording, and compacts the session and sends the turn again. A 502
it retries unchanged. OpenAI's code for the same condition is `context_length_exceeded`, which
spike S3 already counts as a refusal at the window (`LENGTH_CAUSES` in `spikes/s3_harness_task.py`).

So the gateway gets a tenth cause, `context_length_exceeded`, status 400 and no `Retry-After`,
rendered like every other cause by `rhi_gateway.errors` (its name as the error's `type`, and its
`code` on the OpenAI formats). Its sentence holds no number, as every cause's sentence, and begins
with Anthropic's words, so that Claude Code reads the condition in it: `prompt is too long: the
conversation is longer than this model's context.` None of the engine's text reaches the client
(section 5.3): the gateway reads the engine's error to choose the cause and sends only its own
constant.

The window `rhi login` writes (issue E3-57) keeps a Claude Code session below the context in the
normal case; this issue is what lets a session recover when one turn still grows past it.

Suggested branch: `fix/gateway-context-length-exceeded`

## Deliverables

- [ ] `services/common/rhi_common/causes.py` defines `CONTEXT_LENGTH_EXCEEDED`, named `context_length_exceeded`, status 400, no `Retry-After`, with the sentence `prompt is too long: the conversation is longer than this model's context.`, and its docstring counts ten causes.
- [ ] `services/gateway/rhi_gateway/proxy.py` answers an engine's answer of status 400 whose error message contains "maximum context length" with the cause `context_length_exceeded`, on each of the four forwarded routes, reading that answer within the request's time limit, and answers every other engine answer that is not a success with `engine_error` as before.
- [ ] The docstring of `services/gateway/rhi_gateway/proxy.py` says which engine refusal becomes `context_length_exceeded` and why, beside the engine's other failures.
- [ ] `tests/unit/gateway/test_context_length.py` drives the gateway with an upstream that answers each of the two vLLM 0.33.0 bodies of the Context, on each format's route, and asserts the client's status 400, the body `rhi_gateway.errors` renders for `context_length_exceeded`, that no byte of the engine's message reaches the client, and that the request is settled once.
- [ ] `tests/unit/gateway/test_context_length.py` asserts that an engine 400 whose message does not contain "maximum context length", and an engine 500 whose message does, are each answered 502 `engine_error`.
- [ ] `tests/unit/gateway/test_errors.py` holds the tenth cause in its table of the design's statuses.
- [ ] `docs/design/system-design.md` gives `context_length_exceeded` its row in the table of section 6.2, step 8, and in section 4.2 the sentence "When a prompt is too long, vLLM's error is not one Claude Code recognises, so the session cannot compact itself and is lost" says instead that the gateway answers vLLM's refusal with that cause, on which Claude Code compacts.
- [ ] `docs/student-guide.md` gives `context_length_exceeded` its row in the table of causes, saying that Claude Code compacts by itself and that another harness's session must be compacted or started again.
- [ ] `spikes/s3_harness_task.py` says in its docstring and above `UNDETERMINED_CAUSES` that the gateway answers `context_length_exceeded` for an engine's refusal of a prompt too long, and `engine_error` for an engine it lost or that failed otherwise.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_context_length.py tests/unit/gateway/test_errors.py tests/unit/gateway/test_request_ends.py tests/unit/test_docs.py tests/unit/spikes/test_s3.py` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] On the coursework pool, the gateway's record of a Messages request whose prompt is longer than the pool's context names the cause `context_length_exceeded` with status 400 (needs gpu:capacity).
- [ ] Claude Code 2.1.323, with no auto-compact window set, run against the coursework pool past the pool's context, compacts on the gateway's `context_length_exceeded` answer (its event `compact_boundary`) instead of retrying the turn (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.2, 5.3 and 6.2, step 8
- `services/gateway/rhi_gateway/proxy.py`
- `services/gateway/rhi_gateway/errors.py`
- `services/common/rhi_common/causes.py`
- `tests/unit/gateway/test_request_ends.py`, `test_engine_error`
