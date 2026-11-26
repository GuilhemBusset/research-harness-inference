# Evaluation scorecard

Summary digest: `sha256:77a1809d8b1c97de4bab1327ee96b9df52d45af6860644bb8ce6b560a42fdbcf`

Run `sha256:af56506c217fe7e4224846c66c7ea9e6a68c98c529a13a5df2c0506e49d9d298`, workload `sha256:7f62a46abda53cb7df50d9088621ae380cdbd208616fe2ccf5c18dd0fd69b2ba`, treatment `sha256:ee1b33aaf2e3f77f617a0ad0b079867172d3e63f5aadc98190352b2560f55170`.
Suite course-v1, profile qualify, class size 2.

## Recommendation

| Shape | Recommendation | Host count | Run | Tested hosts | Capacity | Absolute quality (class load) | Relative quality (class load) |
|---|---|---|---|---|---|---|---|
| hands_on | unknown | – | run | 2 | met | met | unknown (no_reference) |
| several_agents | not_met | – | run | 2 | missed | met | unknown (no_reference) |

Class size 2. Common configuration: none. Any other class size or host count: unknown (not measured, not projected).

## Configured and observed conditions

| Setting | Requested | Observed | Status |
|---|---|---|---|
| host_count | 2 | 2 | match |
| instance_type | g7e.2xlarge | g7e.2xlarge | match |
| gpu_type | RTX PRO 6000 | RTX PRO 6000 | match |
| gpus_per_host | 1 | 1 | match |
| model_revision | bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb | bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb | match |
| tokenizer_revision | cccccccccccccccccccccccccccccccccccccccc | cccccccccccccccccccccccccccccccccccccccc | match |
| quantization | bits_4 | bits_4 | match |
| engine_image_digest | sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee | sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee | match |
| vllm_version | 0.33.0 | 0.33.0 | match |
| context_limit_tokens | 131072 | – | not_observed |
| kv_dtype | bits_8 | bits_8 | match |
| prefix_caching | yes | yes | match |
| tensor_parallelism | 1 | 1 | match |
| step_budget_tokens | 8192 | – | not_observed |
| speculative_decoding | off | off | match |
| renderer_digest | sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff | – | not_observed |
| gateway_profile_digest | sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | match |

## Capacity at the class size

Host cost is an estimate at 3.36 USD a host-hour (example rate).

**hands_on** at 2 agent slots: pass; limiting row: ttft_p95 (headroom 0.22, repetition 0).

| Rep | Verdict | Reasons | p95 TTFT ms (later turns/min) | p50 / p10 tok/s (streams/min) | Failed / attempts | Failed share (Wilson 95%) | Preemptions | Engine tok/s | Cost / M output USD (estimate) |
|---|---|---|---|---|---|---|---|---|---|
| 0 | pass | – | 3900 (102/100) | 20 / 20 (104/100) | 0 / 104 (observed floor) | 0 (0–0.03562) | 0 | 8 | 233.3 |
| 1 | pass | – | 3900 (102/100) | 20 / 20 (104/100) | 0 / 104 (observed floor) | 0 (0–0.03562) | 0 | 8 | 233.3 |
| 2 | pass | – | 3900 (102/100) | 20 / 20 (104/100) | 0 / 104 (observed floor) | 0 (0–0.03562) | 0 | 8 | 233.3 |

Across repetitions: p95 TTFT 3900 to 3900 ms, p10 speed 20 to 20 tok/s, failed share 0 to 0.

**several_agents** at 4 agent slots: known_miss; limiting row: speed_p10 (headroom -0.5, repetition 1, missed).

| Rep | Verdict | Reasons | p95 TTFT ms (later turns/min) | p50 / p10 tok/s (streams/min) | Failed / attempts | Failed share (Wilson 95%) | Preemptions | Engine tok/s | Cost / M output USD (estimate) |
|---|---|---|---|---|---|---|---|---|---|
| 0 | pass | – | 1500 (104/100) | 20 / 20 (108/100) | 0 / 108 (observed floor) | 0 (0–0.03435) | 0 | 8 | 233.3 |
| 1 | measured_miss | – | 1500 (104/100) | 20 / 5 (108/100) | 0 / 108 (observed floor) | 0 (0–0.03435) | 0 | 8 | 233.3 |
| 2 | pass | – | 1500 (104/100) | 20 / 20 (108/100) | 0 / 108 (observed floor) | 0 (0–0.03435) | 0 | 8 | 233.3 |

Across repetitions: p95 TTFT 1500 to 1500 ms, p10 speed 5 to 20 tok/s, failed share 0 to 0.

## Live requests by harness and load

| Load | Harness | Requests | Failed | p95 TTFT ms (later turns) | p50 / p10 tok/s (streams) |
|---|---|---|---|---|---|
| class_load | claude_code | 90 | 0 | 800 (36) | 20 / 20 (90) |
| class_load | codex | 36 | 0 | 800 (18) | 20 / 20 (36) |
| class_load | opencode | 18 | 0 | 800 (9) | 20 / 20 (18) |
| class_load | pi | 18 | 0 | 800 (9) | 20 / 20 (18) |
| class_load | sdk_tool_loop | 18 | 0 | 800 (9) | 20 / 20 (18) |
| low_load | claude_code | 90 | 0 | 800 (36) | 20 / 20 (90) |
| low_load | codex | 36 | 0 | 800 (18) | 20 / 20 (36) |
| low_load | opencode | 18 | 0 | 800 (9) | 20 / 20 (18) |
| low_load | pi | 18 | 0 | 800 (9) | 20 / 20 (18) |
| low_load | sdk_tool_loop | 18 | 0 | 800 (9) | 20 / 20 (18) |

## Quality by family, harness and load

| Load | Family | Harness | Successes / attempts (planned) | Instances 3/3 | Floor |
|---|---|---|---|---|---|
| low_load | analysis | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| low_load | debugging | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| low_load | debugging | codex | 6 / 9 (9) | 0 / 3 | fail (at 0.8) |
| low_load | optimization | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| low_load | optimization | codex | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| low_load | optimization | opencode | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| low_load | optimization | pi | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| low_load | optimization | sdk_tool_loop | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| low_load | research | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |

Weighted success at low_load: hands_on 0.9167, several_agents 1; combined 0.9583.

| Load | Family | Harness | Successes / attempts (planned) | Instances 3/3 | Floor |
|---|---|---|---|---|---|
| class_load | analysis | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| class_load | debugging | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| class_load | debugging | codex | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| class_load | optimization | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| class_load | optimization | codex | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| class_load | optimization | opencode | 8 / 9 (9) | 2 / 3 | pass (at 0.8) |
| class_load | optimization | pi | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| class_load | optimization | sdk_tool_loop | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |
| class_load | research | claude_code | 9 / 9 (9) | 3 / 3 | pass (at 0.8) |

Weighted success at class_load: hands_on 0.9889, several_agents 1; combined 0.9944.

## Replay and live tasks by shape (p50 / p90)

| Shape | Measure | Replay | Live tasks |
|---|---|---|---|
| hands_on | input_tokens | 45500 / 66000 (n=312) | 7000 / 8000 (n=252) |
| hands_on | output_tokens | 21 / 21 (n=312) | 61 / 61 (n=252) |
| hands_on | cached_share | 0.4302 / 0.7692 (n=312) | 0.25 / 0.5 (n=252) |
| hands_on | tool_delay_ms | 6000 / 8000 (n=306) | 4190 / 4190 (n=126) |
| hands_on | turns_per_task | 52 / 52 (n=6) | 4 / 4 (n=126) |
| hands_on | fan_out | 1 / 1 (n=6) | 1 / 1 (n=126) |
| hands_on | route messages | 1 | 0.2857 |
| hands_on | route responses | 0 | 0.2857 |
| hands_on | route chat_completions | 0 | 0.4286 |
| several_agents | input_tokens | 33000 / 44000 (n=324) | 6000 / 8000 (n=108) |
| several_agents | output_tokens | 21 / 21 (n=324) | 61 / 61 (n=108) |
| several_agents | cached_share | 0.5882 / 0.8696 (n=324) | 0 / 0.5 (n=108) |
| several_agents | tool_delay_ms | 6000 / 8000 (n=312) | 4190 / 4190 (n=36) |
| several_agents | turns_per_task | 27 / 27 (n=12) | 6 / 6 (n=36) |
| several_agents | fan_out | 2 / 2 (n=6) | 2 / 2 (n=36) |
| several_agents | route messages | 1 | 1 |
| several_agents | route responses | 0 | 0 |
| several_agents | route chat_completions | 0 | 0 |

## Task time and completions across tokenizers

| Run | Tokenizer (requested) | Tokenizer (observed) | Load | Successful task ms | Completions / hour | Successes / attempts | Emitted tokens / turn |
|---|---|---|---|---|---|---|---|
| run | cccccccccccccccccccccccccccccccccccccccc | cccccccccccccccccccccccccccccccccccccccc | low_load | 63462 | 52.55 | 78 / 81 | 61 |
| run | cccccccccccccccccccccccccccccccccccccccc | cccccccccccccccccccccccccccccccccccccccc | class_load | 63250 | 53.9 | 80 / 81 | 61 |
