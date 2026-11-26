# Evaluation scorecard

Summary digest: `sha256:0ca4b9c16389f840e772b7f4826fc062eda9e027d7006bc49664c888631ecbcb`

Run `sha256:af56506c217fe7e4224846c66c7ea9e6a68c98c529a13a5df2c0506e49d9d298`, workload `sha256:7f62a46abda53cb7df50d9088621ae380cdbd208616fe2ccf5c18dd0fd69b2ba`, treatment `sha256:ee1b33aaf2e3f77f617a0ad0b079867172d3e63f5aadc98190352b2560f55170`.
Suite course-v1, profile qualify, class size 2.

## Recommendation

| Shape | Recommendation | Host count | Run | Tested hosts | Capacity | Absolute quality (class load) | Relative quality (class load) | Class load shown | Hosts served |
|---|---|---|---|---|---|---|---|---|---|
| hands_on | unknown | – | run | 2 | met | met | unknown (no_reference) | met | met |
| several_agents | not_met | – | run | 2 | missed | met | unknown (no_reference) | met | met |

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

**hands_on** at 2 agent slots: pass; limiting row: ttft_p95 (headroom 0.22, repetition 0); sent from the class's seats in every repetition: yes.

| Rep | Verdict | Reasons | p95 TTFT ms (later turns/min) | p50 / p10 tok/s (streams/min) | Failed / attempts | Failed share (Wilson 95%) | Preemptions | Engine tok/s (by host) | Cost / M output USD (estimate) |
|---|---|---|---|---|---|---|---|---|---|
| 0 | pass | – | 3900 (164/100) | 20 / 20 (166/100) | 0 / 166 (observed floor) | 0 (0–0.02262) | 0 | 8 (0: 4, 1: 4) | 233.3 |
| 1 | pass | – | 3900 (164/100) | 20 / 20 (166/100) | 0 / 166 (observed floor) | 0 (0–0.02262) | 0 | 8 (0: 4, 1: 4) | 233.3 |
| 2 | pass | – | 3900 (164/100) | 20 / 20 (166/100) | 0 / 166 (observed floor) | 0 (0–0.02262) | 0 | 8 (0: 4, 1: 4) | 233.3 |

Across repetitions: p95 TTFT 3900 to 3900 ms, p10 speed 20 to 20 tok/s, failed share 0 to 0.

**several_agents** at 4 agent slots: known_miss; limiting row: speed_p10 (headroom -0.5, repetition 1, missed); sent from the class's seats in every repetition: yes.

| Rep | Verdict | Reasons | p95 TTFT ms (later turns/min) | p50 / p10 tok/s (streams/min) | Failed / attempts | Failed share (Wilson 95%) | Preemptions | Engine tok/s (by host) | Cost / M output USD (estimate) |
|---|---|---|---|---|---|---|---|---|---|
| 0 | pass | – | 1500 (396/100) | 20 / 20 (400/100) | 0 / 400 (observed floor) | 0 (0–0.009512) | 0 | 8 (0: 4, 1: 4) | 233.3 |
| 1 | measured_miss | – | 1500 (372/100) | 20 / 5 (376/100) | 0 / 376 (observed floor) | 0 (0–0.01011) | 0 | 8 (0: 4, 1: 4) | 233.3 |
| 2 | pass | – | 1500 (396/100) | 20 / 20 (400/100) | 0 / 400 (observed floor) | 0 (0–0.009512) | 0 | 8 (0: 4, 1: 4) | 233.3 |

Across repetitions: p95 TTFT 1500 to 1500 ms, p10 speed 5 to 20 tok/s, failed share 0 to 0.

## Capacity sweep (every measured load)

| Shape | Agent slots | Rep | Class size | Verdict | p95 TTFT ms | p50 / p10 tok/s | Failed share |
|---|---|---|---|---|---|---|---|
| hands_on | 2 | 0 | yes | pass | 3900 (164) | 20 / 20 (166) | 0 |
| hands_on | 2 | 1 | yes | pass | 3900 (164) | 20 / 20 (166) | 0 |
| hands_on | 2 | 2 | yes | pass | 3900 (164) | 20 / 20 (166) | 0 |
| several_agents | 4 | 0 | yes | pass | 1500 (396) | 20 / 20 (400) | 0 |
| several_agents | 4 | 1 | yes | measured_miss | 1500 (372) | 20 / 5 (376) | 0 |
| several_agents | 4 | 2 | yes | pass | 1500 (396) | 20 / 20 (400) | 0 |

## Class load behind the live tasks

| Shape | Cell | Rep | Shown | Gap | Background seats (planned) |
|---|---|---|---|---|---|
| hands_on | 15 | 0 | met | – | 1 (1) |
| hands_on | 16 | 0 | met | – | 1 (1) |
| hands_on | 33 | 1 | met | – | 1 (1) |
| hands_on | 34 | 1 | met | – | 1 (1) |
| hands_on | 52 | 2 | met | – | 1 (1) |
| hands_on | 55 | 2 | met | – | 1 (1) |
| several_agents | 14 | 0 | met | – | 1 (1) |
| several_agents | 17 | 0 | met | – | 1 (1) |
| several_agents | 35 | 1 | met | – | 1 (1) |
| several_agents | 36 | 1 | met | – | 1 (1) |
| several_agents | 53 | 2 | met | – | 1 (1) |
| several_agents | 54 | 2 | met | – | 1 (1) |

## Live requests by shape, harness and load

| Shape | Load | Harness | Requests | Failed | p95 TTFT ms (later turns) | p50 / p10 tok/s (streams) |
|---|---|---|---|---|---|---|
| hands_on | class_load | claude_code | 36 | 0 | 800 (18) | 20 / 20 (36) |
| hands_on | class_load | codex | 36 | 0 | 800 (18) | 20 / 20 (36) |
| hands_on | class_load | opencode | 18 | 0 | 800 (9) | 20 / 20 (18) |
| hands_on | class_load | pi | 18 | 0 | 800 (9) | 20 / 20 (18) |
| hands_on | class_load | sdk_tool_loop | 18 | 0 | 800 (9) | 20 / 20 (18) |
| hands_on | low_load | claude_code | 36 | 0 | 800 (18) | 20 / 20 (36) |
| hands_on | low_load | codex | 36 | 0 | 800 (18) | 20 / 20 (36) |
| hands_on | low_load | opencode | 18 | 0 | 800 (9) | 20 / 20 (18) |
| hands_on | low_load | pi | 18 | 0 | 800 (9) | 20 / 20 (18) |
| hands_on | low_load | sdk_tool_loop | 18 | 0 | 800 (9) | 20 / 20 (18) |
| several_agents | class_load | claude_code | 54 | 0 | 800 (18) | 20 / 20 (54) |
| several_agents | low_load | claude_code | 54 | 0 | 800 (18) | 20 / 20 (54) |

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
| hands_on | input_tokens | 61000 / 94000 (n=498) | 7000 / 8000 (n=252) |
| hands_on | output_tokens | 21 / 21 (n=498) | 61 / 61 (n=252) |
| hands_on | cached_share | 0.3226 / 0.6897 (n=498) | 0.25 / 0.5 (n=252) |
| hands_on | tool_delay_ms | 6000 / 8000 (n=492) | 4190 / 4190 (n=126) |
| hands_on | turns_per_task | 83 / 83 (n=6) | 4 / 4 (n=126) |
| hands_on | fan_out | 1 / 1 (n=6) | 1 / 1 (n=126) |
| hands_on | route messages | 1 | 0.2857 |
| hands_on | route responses | 0 | 0.2857 |
| hands_on | route chat_completions | 0 | 0.4286 |
| several_agents | input_tokens | 68500 / 108000 (n=1176) | 6000 / 8000 (n=108) |
| several_agents | output_tokens | 21 / 21 (n=1176) | 61 / 61 (n=108) |
| several_agents | cached_share | 0.2878 / 0.6667 (n=1176) | 0 / 0.5 (n=108) |
| several_agents | tool_delay_ms | 6000 / 8000 (n=1164) | 4190 / 4190 (n=36) |
| several_agents | turns_per_task | 100 / 100 (n=12) | 6 / 6 (n=36) |
| several_agents | fan_out | 2 / 2 (n=6) | 2 / 2 (n=36) |
| several_agents | route messages | 1 | 1 |
| several_agents | route responses | 0 | 0 |
| several_agents | route chat_completions | 0 | 0 |

## Task time and completions across tokenizers

| Run | Tokenizer (requested) | Tokenizer (observed) | Load | Successful task ms | Completions / hour | Successes / attempts | Emitted tokens / turn |
|---|---|---|---|---|---|---|---|
| run | cccccccccccccccccccccccccccccccccccccccc | cccccccccccccccccccccccccccccccccccccccc | low_load | 63462 | 51.4 | 78 / 81 | 61 |
| run | cccccccccccccccccccccccccccccccccccccccc | cccccccccccccccccccccccccccccccccccccccc | class_load | 63250 | 52.72 | 80 / 81 | 61 |
