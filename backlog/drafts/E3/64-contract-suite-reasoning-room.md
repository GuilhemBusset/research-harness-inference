+++
id = "E3-64"
title = "Contract suite: its requests leave a reasoning model room to answer, and a failed test prints which request and which check failed, so that the coursework pool's vLLM passes test_inference"
labels = ["size:L", "kind:test"]
epic = 5
+++

## Context

The document `rhi-engine-contract` (issue #859), sent by the spike job `engine_contract` (issue
#860), ran the four contract tests on the nodes of both pools on 2026-11-28. On the mock pool all
four passed. On the coursework pool (vLLM v0.33.0, the 4-bit build, the release's flags of
`src/rhi/pools/vllm_command.py`) the output was `test_inference failed`, `test_health passed`,
`test_metrics passed`, `test_bearer_secret passed`, `exit 1`, and nothing else: by design the run
prints no word of pytest's report. The same pool serves real traffic through the gateway (spikes
S3 and S5, and the GPU smoke test).

**What the three passing tests rule out.** They send, with the pool's secret and the model name
`coursework`, a whole Chat Completions request read for its text (`test_health`), a streamed one
read for its usage (`test_metrics`), and one request to each of the four routes, answered 200
(`test_bearer_secret`). So the served name, the secret, the Chat text and usage, and the four
routes hold. What `test_inference` adds is the calling requests, the Responses and Messages
answers read for text, calls and usage, and the token count.

**The most likely cause.** Of all the suite's requests, only the Messages request has an output
budget, `max_tokens` 1024 (`tests/contract/test_engine_contract.py`, `REQUESTS`, line 61), and
no request sets a reasoning effort (lines 58 to 63). The coursework model reasons before it
answers, at the effort the request sets (section 4.2), and its template's default is `xhigh`
(section 6.2; E28), whose instruction asks the model to validate its assumptions and consider
alternatives. In vLLM v0.33.0 the Messages route passes `max_tokens` on unchanged
(`vllm/entrypoints/anthropic/serving.py`, line 643) and sets no effort for a request with neither
`thinking` nor `output_config` (lines 670 and 699). When the budget ends inside the reasoning,
the whole answer is one thinking block, with no text block and `stop_reason` `max_tokens` (lines
131 and 883 to 912), and the stream carries thinking deltas alone (lines 1091 to 1112). The suite
then fails at `assert said.text` (line 242) for the request without a function, or at the call
(line 246) for the calling one. Through the gateway this never shows: Claude Code asks for 32,000
tokens (section 17's largest output, E15), and S5's load reads no text. 1,024 tokens are 18
seconds of reasoning at the 57 tokens a second S5 measured for one stream.

This is consistent with every outcome observed, but the run printed nothing that confirms it, so
it is likely rather than proven. Also read in vLLM v0.33.0 and found to hold: a named tool
choice on the three formats (the grammar starts when the reasoning ends,
`vllm/v1/structured_output/__init__.py`, lines 305 to 358); the Responses stream's function-call
events for a model without Harmony (`vllm/entrypoints/openai/responses/streaming_events.py`,
`emit_simple_tool_call_open` and `emit_simple_tool_call_delta`, lines 1047 to 1136); the Messages
usage, whose `input_tokens` leaves out the cached part and the newly cached part (`serving.py`,
lines 75 to 111), which is the whole prompt here, since this model's cache block, sized to its
linear-attention state, is over a thousand tokens and longer than any prompt of the suite. Not
ruled out: a whole answer that took longer than the client's 120-second read time
(`tests/contract/conftest.py`, `READ_SECONDS`), which would take more than about 6,800 tokens of
reasoning.

**Why the fix is in the suite.** Section 4.1 promises the four routes, with streaming and tool
calling. It promises no text within a given budget, and for a model that reasons first an answer
that is reasoning alone, cut at the client's budget, is a correct answer. The release's flags are
the recipe's (section 4.2) and serve real traffic, so nothing in them changes. And the suite needs
none of the gateway's rewrites: it sends no effort to map and none of the blocks the profile
replaces. What the suite lacks is what every harness sends: an effort, and a budget the reasoning
fits in. So every generation request asks for `low`, the lowest effort the coursework model's
template accepts and the gateway passes unchanged (section 17), and for 2,048 tokens. At 57 tokens
a second, 2,048 tokens take 36 seconds, so a whole answer arrives within the client's read time,
and the run's 17 generation requests within the document's 900 seconds
(`src/rhi/pools/contract_check.py`, `TIMEOUT_SECONDS`). The mock reads neither field.

**Why a reason line.** The run's output is a fixed vocabulary, which keeps the pool's secret and
the engine's answers off it (section 4.1). The one word `failed` left the cause above to be
inferred from source code. A reason in the same kind of vocabulary, made of the suite's own
words, says which request failed which check and how the answer ended, and holds nothing of the
request or the answer. After the outcome line of a test whose call failed, the run prints one more
line, `rhi-contract-reason <test> <request> <check> <finish>`, whose words are these:

| Word | Its values |
| --- | --- |
| `<request>` | `health`; `metrics`; `count`; `<format>-whole`, `<format>-streamed`, `<format>-whole-calling` and `<format>-streamed-calling`, for `<format>` one of `chat`, `responses`, `messages`; `<route>-without-secret`, `<route>-wrong-secret` and `<route>-with-secret`, for `<route>` one of `chat`, `responses`, `messages`, `count`; `none` when the failure names no request |
| `<check>` | `status-<three digits>`, the status a route answered where another was expected; `not-a-stream`; `no-end-event`; `no-usage`; `no-text`; `a-call`, a call in the answer to a request that declared no function; `no-call`, an answer to a calling request without exactly one call of `read_file`; `arguments-not-an-object`; `no-input-tokens`, the counting route's answer; `metric-missing-<quantity>` and `metric-kind-<quantity>`, for `<quantity>` one of the eight values of `rhi_common.engine_metrics.Quantity`; `metrics-elsewhere`; `timeout`; `no-connection`; `unreadable`, an answer not of its format's shape; `unexplained`, any failure the suite does not name |
| `<finish>` | How the answer said it ended, in its format: Chat Completions' `finish_reason` `stop`, `length` or `tool_calls`; Responses' `status` `completed` or `incomplete`; Messages' `stop_reason` `end_turn`, `max_tokens`, `tool_use` or `stop_sequence`. `other` for any other value; `none` when the answer gives none or the failure came before an answer |

The observed failure, if the cause above is right, reads `rhi-contract-reason test_inference
messages-whole no-text max_tokens`.

Suggested branch: `fix/contract-suite-reasoning-room`

## Deliverables

- [ ] `tests/contract/test_engine_contract.py` sends every request of the three generation routes, in every test, with the reasoning effort `low` in its format's own field (`reasoning_effort` for Chat Completions, `reasoning.effort` for Responses, `output_config.effort` for Messages, the fields the gateway's profile entry for reasoning effort rewrites) and an output budget of 2,048 tokens in its format's own field (`max_completion_tokens` for Chat Completions, `max_output_tokens` for Responses, `max_tokens` for Messages).
- [ ] `tests/contract/test_engine_contract.py` reads each answer's finish reason as the Context's table names it, whole and streamed, and every check of the four tests an engine at an address answers fails with an assertion whose message begins with the line `<request> <check> <finish>` in the words of that table, a timeout, a transport error or an answer the suite cannot read included.
- [ ] `tests/contract/test_engine_contract.py` holds a test of the suite that records each request the suite sends to the mock and fails when a generation request lacks the effort `low` or the budget of 2,048 tokens in its format's field.
- [ ] `tests/contract/test_engine_contract.py::test_the_suite_catches_a_broken_engine` asserts the reason of each fault it injects, and adds the fault of the Context: Messages answers that hold reasoning alone and end at `max_tokens`, whole and streamed, which fail `test_inference` with `messages-whole no-text max_tokens`.
- [ ] `services/engine/contract/rhi_contract_outcomes.py` writes, for each of its four tests whose call failed, one more line `<test> reason <request> <check> <finish>` after the outcomes: the first line of the failing exception's message when each of its three words is in the Context's vocabulary, and `none unexplained none` otherwise.
- [ ] `tests/unit/engine/test_contract_outcomes.py` runs the plugin on a failing test whose message is a reason and on one whose message holds a made-up secret and an engine's text, and asserts the reason line of the first, `none unexplained none` for the second, and no other byte written.
- [ ] `services/engine/contract/run.sh` prints, after a test's outcome line, `rhi-contract-reason <test> <request> <check> <finish>` when the plugin wrote a reason for that test whose three words each match the Context's vocabulary, and nothing for any other line, and `tests/unit/engine/test_contract_run.py` asserts that line for a run where a test failed with a reason and its absence for a reason line of words outside the vocabulary.
- [ ] `spikes/engine_contract.py` records in each `instance_check` line a field `reasons`, each of the four tests with its three words from the invocation's `rhi-contract-reason` line or `null`, reading only lines whose test is one of `TESTS` and whose words match the vocabulary, and `tests/unit/spikes/test_engine_contract.py` asserts both the recorded reason and a dropped line.
- [ ] `docs/design/system-design.md` says in section 4.1 that the suite's requests ask for effort `low` and 2,048 tokens, because the coursework model reasons first, at `xhigh` by default, and a smaller budget can end in reasoning alone, and lists the reason line and its words beside the vocabulary the script prints.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/contract tests/unit/engine/test_contract_run.py tests/unit/engine/test_contract_outcomes.py tests/unit/spikes/test_engine_contract.py` passes.
- [ ] `uv run --locked pytest tests/contract/test_engine_contract.py::test_the_suite_catches_a_broken_engine` passes, the Messages answer of reasoning alone failing `test_inference` with the reason `messages-whole no-text max_tokens`.
- [ ] `grep -q "rhi-contract-reason" docs/design/system-design.md` exits 0.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] A build of `rhi-spike-engine_contract` on `main`, in a session the owner booked of the coursework pool, writes an `instance_check` line for each instance with the status `Success`, each of the four tests `passed` and the exit 0: issue #149's last item and issue #859's (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 4.2, 6.2 and 17
- `docs/design/evidence.md`, E15 and E28
- `docs/measurements/s5.md`, the single-stream rows of the 4-bit build
- `tests/contract/test_engine_contract.py`, `REQUESTS`, `ask` and `test_inference`
- `tests/contract/conftest.py`, `READ_SECONDS`
- `services/engine/contract/run.sh`
- `services/engine/contract/rhi_contract_outcomes.py`
- `spikes/engine_contract.py`, `reported`
- `src/rhi/pools/contract_check.py`, `TIMEOUT_SECONDS`
- `services/gateway/rhi_gateway/profile/reasoning_effort.py`
- [vLLM v0.33.0, the Messages route](https://github.com/vllm-project/vllm/blob/v0.33.0/vllm/entrypoints/anthropic/serving.py)
