# Evidence for the system design

Every `[E n]` in [`system-design.md`](system-design.md) points here. Sources were fetched between 2026-11-11 and 2026-11-13; a row gives its own date where the date matters.

How far each claim was verified:

- **L**: verified against the primary source (repository code, vendor documentation, an official API), by the command or the page given.
- **P**: reported from the primary source during research; the report was not checked against the source again.
- **T**: third-party source only (price trackers, blogs, search summaries of a primary page).
- **M**: from memory or arithmetic; treat as an estimate.

Prices are US East list prices unless stated. Where a decision rests on a claim marked T or M, section 12 of the design names the spike that tests it; the list of known gaps at the end of this file says where no spike exists.

## E1. DeepInfer

| Claim | Level | Source |
| --- | --- | --- |
| Repository created 2026-10-16, Apache-2.0, about 25.9k stars, no releases, no packages | L | `gh api repos/deepseek-ai/DeepInfer`; https://github.com/deepseek-ai/DeepInfer |
| README: "offline inference"; supports DeepSeek-V4.1-Flash; NVIDIA SM100 GPUs and Huawei Ascend 950; Linux, GCC 13, Python 3.12, CUDA 13; built with `./build.sh`; benchmarks on 4 GPUs (prefill) and 64 GPUs (decode) | L | README |
| README lists as not included: a KV cache system, prefill/decode disaggregation, load balancing across replicas | L | README |
| `LLM.generate()` is synchronous, returns after the whole batch; no async, streaming, server or HTTP dependency anywhere in the tree | P | `deepinfer/entrypoints/llm.py`, `git ls-files` of a shallow clone |
| The loader asserts the V4.1-Flash architecture (`vl_aligner` must be present); no model registry | P | `deepinfer/model_config_loader.py`, `deepinfer/hugging_face/names.py` |
| Telemetry is a text logger of per-step statistics; no Prometheus endpoint | P | `deepinfer/engine/stats_logger.py` |
| A port to the previous GPU generation (SM90) is an open pull request touching 56 files, validated only on H20 GPUs | P | https://github.com/deepseek-ai/DeepInfer/pull/92 |
| An open, unmerged pull request from a community author adds "a minimal OpenAI-compatible chat server over the offline engine": `/v1/chat/completions` with streaming, tools and usage, and a metrics route (opened 2026-10-21, updated 2026-11-03, 7 files, about 1,200 lines) | L | `gh api repos/deepseek-ai/DeepInfer/pulls/147` |
| Further open pull requests add a prefill/decode split across hosts and router-backed serving | L | `gh api repos/deepseek-ai/DeepInfer/pulls` (#197, #226) |
| A port to the newer workstation GPU generation (SM120, the G7e GPU) is an open pull request | P | https://github.com/deepseek-ai/DeepInfer/pull/138 |
| `deepseek-ai/deepseek-recipe` (MIT, pushed 2026-10-19) holds encoding libraries and Python and Rust servers for the V4-family prompt format; its README covers Messages, Chat Completions and Responses | L for the repository and its layout; P for the README's content | `gh api repos/deepseek-ai/deepseek-recipe`; its README |
| One unofficial fork (3 stars, days old) also claims an OpenAI-compatible server | P | `gh api repos/deepseek-ai/DeepInfer/forks?sort=stargazers` |
| The exact compiler targets live in vendored kernel submodules that were not fetched | — | Gap: the SM100 requirement rests on the README and PR #92, not on build flags |

## E2. DeepSeek open weights

| Claim | Level | Source |
| --- | --- | --- |
| DeepSeek-V4.1-Flash-1017: 763B parameters, 510 GB of weights, MIT, image-text-to-text, 1M-token context | L | `https://huggingface.co/api/models/deepseek-ai/DeepSeek-V4.1-Flash-1017?blobs=true`; model card |
| DeepSeek-V4-Flash-0731: 304B parameters, 167 GB, MIT | L | same API |
| DeepSeek-V4-Pro-0813: 1.65T parameters, 893 GB, MIT | L | same API |
| DeepSeek-R1-0528-Qwen3-8B: 8.2B parameters, 16.4 GB in BF16, MIT | L | same API |
| DeepSeek-R1-Distill-Qwen-32B: 32.8B parameters, 65.5 GB in BF16, MIT | L | same API |
| No DeepSeek open-weight release under about 300B parameters since the R1 distills (February and May 2025) | P | `https://huggingface.co/api/models?author=deepseek-ai&sort=lastModified` |
| vLLM recipe for DeepSeek-V4.1-Flash-1017: vLLM 0.31.0 or later; minimum 614 GB of GPU memory; "fits one GB200 NVL4 tray (768 GB) at TP4, or one 8-GPU H200 node (1128 GB)"; tool-call and reasoning parsers `deepseek_v41`; engine-ready timeout set to 3600 to 7200 s, "expect a long first load"; one platform reports a 75 to 90 minute first boot on a new image and about 7 minutes after; verification was run on an earlier checkpoint; only Chat Completions is mentioned; the command uses a Rust front end | L | https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4.1-Flash-1017 |
| vLLM recipe for DeepSeek-V4-Flash: default hardware 8 × H200; "serving without speculative decoding was verified on 8× RTX PRO 6000" (the G7e GPU); 8 × H100 listed but unverified, with a report of a startup failure on one version; the launch command includes `--trust-remote-code`; parsers `deepseek_v4` | L | https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4-Flash |
| Whether these models serve the Messages and Responses routes, and how long they take to load from S3 on AWS instance types | — | Gap: spike S6 |

## E3. vLLM serving surface

| Claim | Level | Source |
| --- | --- | --- |
| Routes: `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`, `/v1/models`, `/health`, `/metrics`, `/load` | L | https://docs.vllm.ai/en/stable/serving/online_serving/ |
| Latest release: v0.33.0, on PyPI since 2026-11-02; release candidates of v0.34.0 are tagged. Before it: v0.29.0 on 2026-09-09, v0.30.0 on 2026-09-22, v0.31.0 on 2026-10-05, v0.32.0 on 2026-10-18, so a minor release about every two weeks. | L | `https://pypi.org/pypi/vllm/json`; `gh api repos/vllm-project/vllm/tags` |
| Metric names: `vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:time_to_first_token_seconds`, `vllm:inter_token_latency_seconds`, `vllm:e2e_request_latency_seconds`, `vllm:request_queue_time_seconds`, `vllm:prompt_tokens`, `vllm:generation_tokens`, `vllm:kv_cache_usage_perc`, `vllm:prefix_cache_hits` | L | https://docs.vllm.ai/en/stable/usage/metrics.html |
| At v0.33.0 the engine also defines the names section 7.2 uses: `vllm:prompt_tokens_by_source`, `vllm:prompt_tokens_cached`, `vllm:request_time_per_output_token_seconds`, `vllm:request_generation_tokens`, `vllm:request_decode_time_seconds`, `vllm:request_prefill_time_seconds`, `vllm:request_prefill_kv_computed_tokens`, `vllm:num_preemptions`, `vllm:cache_config_info` | L | `vllm/v1/metrics/loggers.py` at tag `v0.33.0` |
| What they mean: the input counter includes cached tokens and `prompt_tokens_by_source` separates `local_compute` from cache hits; time to first token runs from arrival and includes queueing; time per output token is one value per request, decode time over tokens minus one; counters are exposed with a `_total` suffix; the only labels are the model's name and an engine index | P | `vllm/v1/metrics/stats.py` and `loggers.py` at `v0.33.0` |
| `--custom-histogram-buckets` replaces the boundaries of a bucket family (`request_latency`, `time_to_first_token`, `inter_token_latency`). The default inter-token boundaries start at 0.01, 0.025, 0.05 and 0.075 s | L | `docs/usage/metrics.md` and `vllm/v1/metrics/buckets.py` at `v0.33.0` |
| vLLM ships three Grafana dashboards under `examples/observability/`; none has the cached share, preemptions, speed per stream, cost, or a view per instance | P | The repository at `v0.33.0` |
| Deprecation policy: a metric is hidden one minor version after deprecation and removed the next | L | same page |

## E4. vLLM tool calling

| Claim | Level | Source |
| --- | --- | --- |
| `--enable-auto-tool-choice` plus `--tool-call-parser`; parsers include `hermes`, `qwen3_xml`, `deepseek_v3`, `deepseek_v31`; `required` and named tool choice supported | L | https://docs.vllm.ai/en/stable/features/tool_calling.html |
| The stable tool-calling page documents no parser for plain Qwen3 or for DeepSeek V4; the recipes (E2) use `deepseek_v4` and `deepseek_v41`, so that page lags | L | same page; the recipes. The right parser for the small model is part of spike S3 |
| `qwen3_xml` is documented for Qwen3-Coder models | L | same page |

## E5. GPU instances

| Claim | Level | Source |
| --- | --- | --- |
| Only the P6 family has compute capability 10 (Blackwell data-center). G7 and G7e are capability 12; P5 is 9.0; G6e and G6 are 8.9 | T | https://instances.vantage.sh/aws/ec2/p6-b200.48xlarge , https://instances.vantage.sh/aws/ec2/g7e.2xlarge |
| P6-B200 exists on demand only as `p6-b200.48xlarge` (8 GPUs, 192 vCPUs) | P | https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html , https://aws.amazon.com/ec2/instance-types/p6/ |
| On-demand prices: `g6.xlarge` $0.805, `g5.xlarge` $1.006, `g6e.xlarge` $1.861, `g7e.2xlarge` $3.363, `p5.48xlarge` $55.04, `p6-b200.48xlarge` $113.93 | P | Price trackers, then matched to the cent against the AWS pricing feed published 2026-11-10 |
| `g7e.48xlarge` $33.14, `p5en.48xlarge` $63.30 | P | Same feed |
| `g7e.24xlarge` $16.57, `g7e.12xlarge` $8.29, `g6e.12xlarge` $10.49, `g6e.48xlarge` $30.13, `p5.4xlarge` $6.88. Per GB of GPU memory the multi-GPU G6e sizes cost more than G7e, and two `g7e.2xlarge` cost less than one `g7e.12xlarge` | T | https://instances.vantage.sh, fetched 2026-11-13; not matched against the AWS feed |
| `g7e.2xlarge`: 8 vCPUs on 4 cores, 64 GiB, one RTX PRO 6000 with 96 GiB. `g7e.24xlarge`: 96 vCPUs, 1,024 GiB, four of them. `g6e.2xlarge`: one L40S with 44 GiB | L | https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html |
| `g6e.2xlarge` about $2.24: 8 vCPUs, 64 GiB, one L40S | T | A price tracker; the page does not label the region |
| `g6e.xlarge` has 4 vCPUs on 2 physical cores; vLLM documents a minimum of "2 + N physical cores" | P | https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html ; https://docs.vllm.ai/en/stable/configuration/optimization.html |
| `g6e.xlarge`: one L40S, 48 GB. `g7e.2xlarge`: one RTX PRO 6000, 96 GB. `g7e.48xlarge`: eight of them, 768 GB. `g5.xlarge`, `g6.xlarge`: 24 GB | P | AWS instance pages and the G7e launch post |
| Ireland is not listed for G7e, P5 or P6; Stockholm carries G6e, G7, G7e, P5 and P6-B200 | T | Price trackers; confirm with `describe-instance-type-offerings` |

## E6. Quotas

| Claim | Level | Source |
| --- | --- | --- |
| Default On-Demand and Spot vCPU quotas for the G and P families are 0 in a new account; they are adjustable | P | https://docs.aws.amazon.com/ec2/latest/instancetypes/ec2-instance-quotas.html |
| How long an increase takes is not documented | — | Gap. The owner's own account quotas could not be read: the local AWS session had expired |

## E7. Capacity Blocks

| Claim | Level | Source |
| --- | --- | --- |
| Supported for P-family types including `p6-b200.48xlarge`, not for G-family; paid up front; no cancellation | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html |
| `p6-b200.48xlarge`: $113.666 per instance-hour | P | https://aws.amazon.com/ec2/capacityblocks/pricing |
| Minimum duration of one day | T | Search summaries; the AWS pages fetched state no minimum |

## E8. SageMaker scale to zero

| Claim | Level | Source |
| --- | --- | --- |
| Scale-out from zero needs a step-scaling policy on a `NoCapacityInvocationFailures` alarm; requests fail while capacity is provisioned | P | https://docs.aws.amazon.com/sagemaker/latest/dg/endpoint-auto-scaling-zero-instances.html |
| Streaming responses are capped at 8 minutes | P | https://docs.aws.amazon.com/sagemaker/latest/dg/hosting-faqs.html |
| End-to-end start measured at 258 s for an 8B model with container caching | P | https://aws.amazon.com/blogs/machine-learning/amazon-sagemaker-inference-2026-year-to-date-launches-in-review/ |
| Price premium over EC2 of 15 to 40% | T | Third-party price pages |

## E9. ECS and EKS

| Claim | Level | Source |
| --- | --- | --- |
| ECS Managed Instances supports GPU families including G6e, G7e, P5 and P6-B200; AWS manages the image and driver | P | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/managed-instances-instance-types.html , `.../managed-instances-gpu.html` |
| AWS's own scale-to-zero sample on ECS Managed Instances: about 13 minutes from request to first output with a 14 GB image | P | https://aws.amazon.com/blogs/containers/run-gpu-batch-inference-on-amazon-ecs-managed-instances-with-scale-to-zero/ |
| ECS capacity-provider managed scaling scales out to two instances from zero, and needs 15 minutes of datapoints to scale in | P | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/managed-scaling-behavior.html |
| With ECS, instances cannot be returned to the warm pool on scale-in | P | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using-warm-pool.html |
| ECS Managed Instances: "Custom AMIs are not supported" | P | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ManagedInstances.html |
| EKS control plane costs $0.10 per hour; the KEDA HTTP add-on holds requests and is still beta | T | https://github.com/kedacore/http-add-on and pricing summaries |

## E10. Warm pools

| Claim | Level | Source |
| --- | --- | --- |
| Warm pool states are stopped, running and hibernated; stopped instances cost only their volumes; instances can return to the pool on scale-in | P | https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-warm-pools.html |
| No Spot in a mixed-instances group with a warm pool | P | same page |
| GPU families are not in the hibernation-supported list | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/hibernating-prerequisites.html |
| A launch-template change leaves existing instances untouched, in service or in the warm pool; "an instance refresh first replaces `InService` instances. Then it replaces instances in the warm pool" | P | Warm pools page above |
| "By default, cloud-init runs user data only on the first boot" | P | https://docs.aws.amazon.com/autoscaling/ec2/userguide/warm-pool-instance-lifecycle.html |
| The pool's size is its prepared capacity minus the group's desired capacity; a pool minimum keeps that many regardless, and only equal values give an absolute size | P | https://docs.aws.amazon.com/autoscaling/ec2/APIReference/API_PutWarmPool.html |
| A manual increase of desired capacity draws from the warm pool; `SetDesiredCapacity` is absolute and ignores cooldowns by default | P | Warm pools page; https://docs.aws.amazon.com/autoscaling/ec2/APIReference/API_SetDesiredCapacity.html |
| A warm instance that cannot start, including for lack of capacity, is terminated as a failed launch, and a depleted pool means a cold launch | P | Warm pools page |
| Lifecycle hooks wait one hour by default; the same wait state serves the launch hook on entry to the pool and the termination hook on return | P | Lifecycle page above |
| Mixed instance types are allowed with a warm pool, On-Demand only and without weights | P | https://docs.aws.amazon.com/autoscaling/ec2/userguide/create-warm-pool.html |
| AWS's base GPU image with the open-source NVIDIA driver (Ubuntu 22.04), release 20261110, lists G7e among its supported instances, with G6e, P5, P5e, P5en and P6. Its family page carries a known issue for multi-node work on `g7e.8xlarge`, which this design does not do, and gives the public SSM parameter for the latest image ID | L | https://docs.aws.amazon.com/dlami/latest/devguide/aws-deep-learning-ami-gpubaseoss-ul2204-2026-11-10.html ; https://docs.aws.amazon.com/dlami/latest/devguide/aws-deep-learning-x86-base-gpu-ami-ubuntu-22-04.html |
| The "single CUDA" variant of the base image is a different image: its release notes list G6e, G7, P5 and P6 and not G7e. It is not the one this design uses | P | https://docs.aws.amazon.com/dlami/latest/devguide/aws-deep-learning-ami-gpubasesinglecuda-ul2204-2026-10-09.html |
| The instance store of a GPU instance is wiped when it stops | P | Same image page and instance documentation; where Docker keeps its data on that image was not confirmed: spike S1 |
| No published measurement of a stopped GPU instance returning to service | — | Gap. The design stops no instance: E32 |

## E11. Cold start and model loading

| Claim | Level | Source |
| --- | --- | --- |
| Stock cold start of about 600 s: image pull 420 s, weights 120 s; after a slimmer image, parallel pull and streamed weights: pull 55 s, weights 4.9 s | P | https://builder.aws.com/content/3Hp00o5AsSPZwexVw1nqc7tfwHT/how-we-cut-inference-cold-starts-from-minutes-to-seconds |
| Run:ai streamer from S3 on `p5.48xlarge`: 64 GiB in 12 to 29 s; 200 GB in about 25 s tuned; defaults 80 to 460 s; local NVMe only about 5 s faster than tuned S3 | P | https://aws.amazon.com/blogs/containers/fast-model-loading-for-ai-inference-on-amazon-eks/ |
| vLLM supports `--load-format runai_streamer` with `s3://` paths | P | https://docs.vllm.ai/en/latest/models/extensions/runai_model_streamer.html |
| One report of a model loading slower with the streamer than without | T | Issue on a fork; the reason the loader is a per-pool setting |
| GPU utilization is a poor scaling signal under batching | T | AWS and vLLM guidance; mechanism from memory |
| FSx for Lustre minimum of roughly $170 a month; EFS about $0.30 per GB-month | T | Pricing summaries |

## E12. Client timeouts and retries

| Claim | Level | Source |
| --- | --- | --- |
| Claude Code: 10 retries by default; `API_TIMEOUT_MS` 600000 for response headers; byte and event watchdogs of 300 s through a custom base URL; a pings-only stream is tolerated for about 12.5 minutes; spend-limit 429s are not retried | P | https://code.claude.com/docs/en/env-vars , `.../errors.md` , `.../network-config.md` |
| Codex CLI: `request_max_retries` 4, `stream_max_retries` 5, `stream_idle_timeout_ms` 300000 | P | https://learn.chatgpt.com/docs/config-file/config-reference |
| Codex CLI's idle timer waits for the next parsed SSE event, and its parser discards comments, so comment keep-alives do not reset it | P | Codex CLI's source |
| OpenAI Python SDK: 2 retries, 10-minute timeout | P | https://raw.githubusercontent.com/openai/openai-python/main/README.md |
| Anthropic SDKs: 2 retries, 10-minute timeout | T | Search summary |
| How each client behaves through a full hold | — | Gap. The design holds no request (section 5.3) |

## E13. Application Load Balancer

| Claim | Level | Source |
| --- | --- | --- |
| Idle timeout 1 to 4000 s, default 60 s; bytes in either direction reset it | P | https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-load-balancer-attributes.html |
| 503 when a target group has no registered or healthy targets | P | https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-troubleshooting.html |
| Deregistration delay 0 to 3600 s | P | Target group attributes documentation |
| On scale-in a terminating instance "is first deregistered from the load balancer" and drains before the termination hook | P | https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks-overview.html |
| A target with no connections is deregistered at once but "is displayed as `draining` until the deregistration delay timeout expires"; whether Auto Scaling waits for that, and what happens on the return-to-pool path, is not documented | P | https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html ; gap: spike S7 |
| ECS waits for a task's target to finish deregistering (300 s by default) before it sends SIGTERM; a Fargate task then has at most 120 s | P | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/load-balancer-connection-draining.html |
| A target group health check can run every 5 s, and one passing check makes a new target eligible | P | https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html |
| With sticky sessions, "the selected routing algorithm is used for the initial target selection. Future requests from the same client will be forwarded to the same target, bypassing the selected routing algorithm". Stickiness is not available with the weighted random algorithm; least outstanding requests is not excluded | L | https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html |
| Duration-based stickiness uses a cookie named `AWSALB` that the load balancer generates and encrypts; the duration is 1 second to 7 days and the expiry is reset after every request. "If the cookie is present but can't be decoded, or if it refers to a target that was deregistered or is unhealthy, the load balancer selects a new target and updates the cookie". A request for a target that is draining "is routed to a healthy target" | L | Same page |
| The same page lists "at least one healthy instance in each Availability Zone" as a requirement for sticky sessions. Whether stickiness holds with one or two targets across three zones is not stated | L | Same page; gap: spike S8 |
| The gateway, as the load balancer's client, replaying that cookie per student | M | Design; follows from the documented behaviour, not tried: spike S8 |

## E14. Other edges

| Claim | Level | Source |
| --- | --- | --- |
| API Gateway REST response streaming: up to 15 minutes, 5-minute idle timeout on regional endpoints; usage plans count requests, not tokens | P | https://docs.aws.amazon.com/apigateway/latest/developerguide/response-transfer-mode.html |
| CloudFront origin response timeout: sources give 30 s default, or 120 s, with a higher value by quota request | P/T | https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesOrigin.html ; conflicting third-party values |
| Lambda response streaming keeps billing after a client disconnects; function URLs do not stream inside a VPC | P | https://docs.aws.amazon.com/lambda/latest/dg/configuration-response-streaming.html |

## E15. What the harnesses speak

| Claim | Level | Source |
| --- | --- | --- |
| Claude Code needs Anthropic Messages; `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`, `apiKeyHelper` | P | https://code.claude.com/docs/en/llm-gateway-protocol.md , `.../llm-gateway-connect.md` |
| Anthropic does not support routing Claude Code to non-Claude models through any gateway | P | https://code.claude.com/docs/en/llm-gateway.md |
| Claude Code sends `max_tokens` 32000 on main turns; open defects in vLLM's Messages route for Claude Code traffic, for every model family (E28) | L | https://github.com/vllm-project/vllm/issues/58647 |
| Behind a custom base URL, Claude Code assumes a 200,000-token window for a model it does not recognise; one environment variable overrides it and another lowers the point at which a session compacts | T | Search summaries of Claude Code's model configuration page; the page was not fetched. Spike S3 checks that a full session compacts |
| Codex CLI accepts only `wire_api = "responses"`; `"chat"` is a configuration error | P | https://github.com/openai/codex/discussions/7782 and `codex-rs/model-provider-info/src/lib.rs` |
| DeepSeek Harness's native adapter speaks Anthropic Messages, sends the key in `x-api-key`, and rejects redirects | P | https://github.com/deepseek-ai/deepseek-harness (`packages/llm/llm-deepseek/README.md`) |
| DeepSeek's hosted API maps `claude-*` model names to its own models | P | https://api-docs.deepseek.com/guides/anthropic_api |

## E16. LiteLLM and other gateways

| Claim | Level | Source |
| --- | --- | --- |
| Compromised releases on 2026-03-24 (v1.82.7, v1.82.8) and 2026-10-21 (v1.107.0 on PyPI and Docker Hub) | P | https://docs.litellm.ai/blog/security-incident-oct-21-2026 ; https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-docker/ |
| 18 or 19 security advisories published in 2026 (two counts on the same day), five critical, including authentication bypasses | P | `gh api repos/BerriAI/litellm/security-advisories` |
| Budgets are in dollars; SSO is free for up to 5 users; Postgres required, Redis beyond one instance; 1 vCPU and 4 GB per worker recommended | P | https://docs.litellm.ai/docs/proxy/users , `.../enterprise` , `.../proxy/prod` |
| Open defects translating Messages to Chat Completions for reasoning models on vLLM | T | Issue titles via search |
| Kong's token rate limiting is Enterprise-only; Envoy AI Gateway needs Kubernetes; TensorZero is archived | P/T | Vendor documentation and `gh api` |

## E17. Identity

| Claim | Level | Source |
| --- | --- | --- |
| GitHub's device flow: codes last 15 minutes; the caller passes the scopes it wants; no client secret is needed; 50 code confirmations an hour per app | P | https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps |
| A GitHub App's user token carries only the permissions both the user and the app have, takes no scopes, and expires after 8 hours by default | P | https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app |
| A token with no scope can read public profile information, including the numeric user ID | P | https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps |
| Cognito user pools have no GitHub provider; GitHub OAuth is not OpenID Connect | P | https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_CreateIdentityProvider.html |
| Whether an app with no permissions that nobody has installed completes the device flow | — | Gap: spike S9 |

## E18. WAF

| Claim | Level | Source |
| --- | --- | --- |
| The managed cross-site-scripting body rule blocks real Claude Code prompts | P | https://code.claude.com/docs/en/llm-gateway-connect.md (troubleshooting) |
| $5 per web ACL, $1 per rule, $0.60 per million requests | P | https://aws.amazon.com/waf/pricing/ |

## E19. Observability

| Claim | Level | Source |
| --- | --- | --- |
| CloudWatch ingests OTLP metrics and answers PromQL (GA 2026-06-16), $0.50 per GB; a query spans at most 7 days | P | https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-PromQL.html ; https://aws.amazon.com/cloudwatch/pricing/ |
| Whether PromQL alarms can drive EC2 Auto Scaling is contradicted between an AWS documentation table and a recent blog | P/T | https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-actions.html |
| The AWS managed collector scrapes EC2 only from static address lists | P | https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/managed-prometheus-collectors-vpc-setup.html |
| Managed Prometheus: $0.90 per 10M samples ingested, $0.03 per GB-month, $0.10 per billion samples queried | P | https://aws.amazon.com/prometheus/pricing/ |
| Amazon Managed Grafana: $9 per editor, $5 per viewer; signs in through IAM Identity Center (needs AWS Organizations) or SAML; CloudFormation covers the workspace only; service-account tokens last at most 30 days | P | https://aws.amazon.com/grafana/pricing/ ; https://docs.aws.amazon.com/grafana/latest/userguide/authentication-in-AMG-SSO.html ; API reference |
| DCGM exporter 4.9.0 (2026-11-06) renamed its default metrics and changed units: utilization as a ratio (`DCGM_FI_DEV_GPU_UTIL_RATIO`), memory in bytes, energy in joules (`DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL`), power as `DCGM_FI_DEV_BOARD_POWER_WATTS`. A 4.8 image still emits the old names | P | https://github.com/NVIDIA/dcgm-exporter/releases/tag/4.9.0 and `etc/default-counters.csv` at tags 4.9.0 and 4.8.4 |
| Grafana Foundation SDK for Python: typed, version 0.0.22, public preview | P | https://pypi.org/pypi/grafana-foundation-sdk/json |
| SGLang metric names differ from vLLM's | P | https://docs.sglang.io/references/production_metrics.html |
| With no GPU instance there are no engine metrics, so the demand signal must come from the gateway | M | Follows from the architecture |

## E20. Delivery toolchain

| Claim | Level | Source |
| --- | --- | --- |
| Versions: aws-cdk-lib 2.276.0, CDK CLI 2.1148.1, uv 0.13.7, ty 0.0.101 (beta), ruff 0.17.4, cdk-nag 3.0.2 | P | PyPI and GitHub releases |
| CDK supports Node 22 and 24; Node 26 is not in the support table | P | https://docs.aws.amazon.com/cdk/v2/guide/node-versions.html |
| `cdk synth --lookups=false` fails if a context lookup is needed | P | https://docs.aws.amazon.com/cdk/v2/guide/ref-cli-cmd.html |
| cdk-nag 3 is a policy-validation plugin (`Validations.of(app).add_plugins(...)`); `NagSuppressions` is removed | P | cdk-nag README; https://docs.aws.amazon.com/cdk/v2/guide/policy-validation-synthesis.html |
| A connection created by CloudFormation starts pending and is authorized once in the console | P | https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-codestarconnections-connection.html |
| CodePipeline V2 execution modes; queued mode gives each commit its own execution | P | https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.pipelines/README.html |
| No native reporting of pipeline results to GitHub commit statuses was found | P | Absence of evidence in documentation and announcements |
| `cdk bootstrap` gives CloudFormation's execution role `AdministratorAccess` unless told otherwise; AWS recommends a permissions boundary | P | https://github.com/aws/aws-cdk/blob/main/packages/aws-cdk-lib/pipelines/README.md |
| `ConfirmPermissionsBroadening` checks a stage about to be deployed; the pipeline's own stack is not a stage and updates earlier | P | same README |
| A pipeline's self-update can cancel executions still queued | P | same README |
| A CloudFormation parameter that reads a public SSM path takes the latest value on every stack update | P | https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/parameters-section-structure.html |
| A CodeBuild webhook on pull requests of a public repository can run untrusted code with the project's role; January 2026 incident and AWS bulletin | P | https://aws.amazon.com/security/security-bulletins/2026-002-AWS ; https://www.wiz.io/blog/wiz-research-codebreach-vulnerability-aws-codebuild |
| Machine images can be resolved from public SSM parameters at deploy time | M | Standard CDK (`MachineImage.from_ssm_parameter`) |
| ty against current aws-cdk-lib | — | Gap: an earlier false-positive issue is closed, not re-tested: spike S11 |

## E21. Account identifiers and scanning

| Claim | Level | Source |
| --- | --- | --- |
| AWS: account IDs "are not considered secret, sensitive, or confidential information" | P | https://docs.aws.amazon.com/accounts/latest/reference/manage-acct-identifiers.html |
| GitHub secret scanning has no pattern for a bare account ID | P | https://docs.github.com/en/code-security/secret-scanning/introduction/supported-secret-scanning-patterns |
| gitleaks 8.30.1, trufflehog 3.99.8 | P | GitHub releases |

## E22. Bedrock

| Claim | Level | Source |
| --- | --- | --- |
| Custom Model Import does not list DeepSeek's native architectures (the R1 distills are Qwen and Llama architectures, which it does list) and is not available through CloudFormation | P | https://docs.aws.amazon.com/bedrock/latest/userguide/model-customization-import-model.html |
| Managed DeepSeek-V3.2: $0.62 and $1.85 per million input and output tokens | P | The AWS price list |
| Bedrock's OpenAI-compatible endpoint offers Chat Completions and Responses, not Messages | P | https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html |
| Input tokens for each hour an agent runs: 7 to 15 million | M | Arithmetic from the request rate of E31 and an assumed 50,000-token transcript |
| Whether Bedrock bills repeated input at a cached rate for this model | — | Not checked |

## E23. vLLM as a shared, exposed service

| Claim | Level | Source |
| --- | --- | --- |
| `--api-key` protects "only ... endpoints under the `/v1`, `/v2`, `/inference`, and `/cohere` path prefixes"; `/invocations`, `/pause`, `/tokenize`, `/metrics` and others answer without it; the guide recommends a reverse proxy that allow-lists routes | P | https://github.com/vllm-project/vllm/blob/main/docs/usage/security.md |
| "vLLM does not provide isolation between tenants that share the same server process"; it recommends per-tenant cache identifiers and request validation at a proxy | P | same guide |
| 98 security advisories published in 2026, 39 since 1 October; many are one request field that stalls or crashes the engine, some while `/health` keeps answering | P | `gh api repos/vllm-project/vllm/security-advisories` |
| Without an allow-list of media domains the engine can be made to fetch internal addresses | P | same guide |
| Usage statistics are sent to a vLLM server unless `VLLM_NO_USAGE_STATS` is set | P | `vllm/envs.py` |
| The AWS-managed Session Manager policy grants `ssm:GetParameter` on every parameter | P | https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonSSMManagedInstanceCore.html |

## E24. Grafana sign-in

| Claim | Level | Source |
| --- | --- | --- |
| Grafana's GitHub sign-in can restrict by organization, team or e-mail domain; there is no user allow-list. Sign-up is on by default and a user without a mapped role becomes a viewer unless strict role mapping is set | P | https://github.com/grafana/grafana/blob/main/docs/sources/setup-grafana/configure-access/configure-authentication/github/index.md |
| "A user with the `Viewer` role can issue any possible query to a data source"; per-data-source permissions are an Enterprise and Cloud feature | P | Grafana configuration documentation |
| The default admin password is `admin` unless initial admin creation is disabled | P | same |
| An ALB can authenticate users against a Cognito user pool before forwarding, and passes the identity in a header; Grafana can trust a proxy header for sign-in | M | Both long-standing features; the combination with the pinned settings is spike S10 |

## E25. The course

| Claim | Level | Source |
| --- | --- | --- |
| Five biweekly sessions; the first is four hours, the others about two | P | The cursus repository's `README.md` and `docs/cursus-narrative.md` |
| Claude Code is the course's anchor harness, with Codex shown beside it | P | `docs/cursus-narrative.md` |
| The students are doctoral researchers in optimisation and mathematics; the work is optimisation modelling in Python, running solvers, and writing it up. The second session runs two skill packs inside the harness: a multi-agent paper-writing pack, with parallel plotting and literature agents and a drafting agent that reads the figures, and a bundle of science skills that wrap literature and data services. The third session runs the same harness headless and through its SDK | L | The cursus repository's `README.md` and `docs/cursus-narrative.md`, read on 2026-11-12 |
| Class size and homework hours | — | Not documented anywhere: decisions for the owner |

## E26. Edge details

| Claim | Level | Source |
| --- | --- | --- |
| The default ALB security policy through CloudFormation and the CDK is `ELBSecurityPolicy-2016-08`, which allows TLS 1.1 and lacks TLS 1.3 | P | https://docs.aws.amazon.com/elasticloadbalancing/latest/application/describe-ssl-policies.html |
| AWS recommends `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` | P | Same page |
| WAF log redaction does not apply to request sampling; sampling must be turned off or given data protection | P | https://docs.aws.amazon.com/waf/latest/developerguide/logging-management-configure.html |
| "Any person or integration with write permissions to a repository can set the state of any status check", unless the ruleset names the expected source | P | https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets |
| Making a private repository public also makes its Actions history and logs public | P | https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility |

## E27. Retention

| Claim | Level | Source |
| --- | --- | --- |
| CloudWatch metrics cannot be deleted and expire after 15 months | P | https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch_concepts.html |
| Managed Prometheus retention is a workspace setting | P | https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-workspace-configuration.html |
| AWS Budgets works from billing data "updated at least once per day" | P | https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html |

## E28. Open-weight models and the harnesses

| Claim | Level | Source |
| --- | --- | --- |
| Qwen3.8-27B: created 2026-08-05, Apache-2.0, image and text in, 27.8B parameters; 64 layers of which every fourth keeps full attention (16), 4 key-value heads of dimension 256; 262,144 positions. The 8-bit repository, `Qwen/Qwen3.8-27B-FP8`, is 30.9 GB, Apache-2.0. It is the smallest model of the 3.8 series | L | `https://huggingface.co/api/models/Qwen/Qwen3.8-27B`, `.../Qwen3.8-27B-FP8?blobs=true`, `config.json`, and the publisher's model list |
| Its chat template accepts reasoning effort `xhigh`, `medium` and `low` and raises an error on any other value | L | `https://huggingface.co/Qwen/Qwen3.8-27B/raw/main/chat_template.jinja`, lines 47 to 49 |
| vLLM's recipe for it marks one RTX PRO 6000 (the G7e GPU) as verified, on 2026-08-15 with a development build of vLLM 0.26. "Verified" there means the model was "booted, served and answered" a prompt. The builds it lists for that GPU are the 16-bit weights, with at most 8 requests running, and NVIDIA's 4-bit build, with an 8-bit cache, the 262,144 context, at most 8 requests and a step budget of 8,192 tokens. The publisher's 8-bit build lists H100 and the RTX 5090 among its hardware, and not the RTX PRO 6000. Parsers `qwen3_coder` and `qwen3`; no model code needed | L | https://github.com/vllm-project/recipes , `models/Qwen/Qwen3.8-27B.yaml` |
| The same recipe's cache sizes at start: on one RTX 5090, 152,917 tokens with an 8-bit cache and 76,458 with a 16-bit one; on two RTX 5090, 377,456 tokens with the 8-bit weights, and 445,875 and 920,517 with two 4-bit builds. The 4-bit build is 21.9 GB | P | Same file. The two-GPU table does not say which cache each row used |
| The vLLM project replayed captured Claude Code traffic through `/v1/messages` and each family's chat template. It found, for every family, at least one defect. For Qwen3.8: "every default Claude Code request returns 400", because Claude Code sends effort `high`. For all families whose template wants system text first: inline system messages are moved to the top, so "prefix cache misses on the whole conversation, every turn". DeepSeek-V4 "is fine"; V4.1 raises on effort `medium`; gpt-oss drops images and flattens tool schemas; Devstral raises whenever the history holds a thinking block; Gemma 4 loses tool results | L | https://github.com/vllm-project/vllm/issues/58647 (open) |
| Status of the fixes on 2026-11-13: the pull request that keeps inline system messages in place (#53501) is open, and its issue (#63331) is open; `--reasoning-effort-map` (#61094) was merged on 2026-10-26 and is in v0.33.0; keep-alive pings while a Messages stream waits for its first token (#61356) are an open pull request; the `thinking` field is no longer dropped from v0.33.0 | L, except the last (P) | `gh api repos/vllm-project/vllm/issues/<n>`; `gh api .../compare/<merge commit>...v0.33.0` |
| The same replay, item 5: "`document` (PDFs from the Read tool), `search_result`, `tool_addition` / `tool_removal` and server-tool blocks all fail. Once one is in the history, every later request in the session fails." A fix for the tool addition and removal blocks (#57693) was merged on 2026-10-02 and is in v0.33.0; none for document blocks was found | L | Same issue; `gh api repos/vllm-project/vllm/pulls/57693` |
| Claude Code's built-in web search is run by its vendor's service, not by the harness, so it would not work behind this endpoint | M | From memory, not checked. Spike S3 |
| Claude Code's request shape in that replay: about 25 tools with nested schemas, interleaved thinking with parallel tool calls, system messages mid-conversation that change each turn, `max_tokens` 32000 on the main loop. With a 128k context a session "can't recover" once it overflows | L for the request shape; P for the overflow comment | Same issue, body and comments |
| vLLM's own integration guides use gpt-oss-120b for Claude Code and a Qwen3.6 model, with the same two parsers, for Codex; a served model name must not contain a slash | P | `docs/serving/integrations/claude_code.md` and `codex.md` at `v0.32.0` |
| The other candidates of section 4.2: sizes, licences, contexts and whether they read images | P | Hugging Face API and `config.json` of each repository, read on 2026-11-13 |
| Which GPU each candidate's recipe was run on, its parsers, and whether it needs model code | P | https://github.com/vllm-project/recipes |
| Candidates considered and not shown in section 4.2. Nemotron 3.5 Super VL (120B, 12B active, OpenMDW 1.1): its 4-bit build is 75 GB and ran on the G7e GPU, leaving about 10 GiB for transcripts; independent index 14. Devstral Small 2 (24B, Apache-2.0): no recipe; 160 KiB a token of transcript; its tokenizer raises an error when the history holds a thinking block. Gemma 4 31B (Apache-2.0): 160 KiB a token; drops tool results that follow an image in a tool result, and renders some tool schemas with loss. GLM-5.3-Flash (321B, 18B active, MIT): recipe run on 8 × H100; cannot turn reasoning off. DeepSeek-V4-Flash (304B, 13B active, MIT, no images): recipe run on 8 of the G7e GPU; the one family the replay above found working unchanged. DeepSeek-R1-0528-Qwen3-8B (8.2B, MIT): 131,072 context, no images | P | Hugging Face API, the recipes, and issue 58647 |
| Cost of a token of transcript, at 16 bits: Qwen3.8-27B 64 KiB; Qwen3.6-35B-A3B 20 KiB; Devstral Small 2 and Gemma 4 31B 160 KiB. From two tensors per caching layer, times key-value heads, times head dimension, times two bytes | M | Arithmetic from each `config.json`; the 2 × RTX 5090 run above bounds Qwen3.8-27B at 64 to 80 KiB |
| Independent results: an index of 20 for Qwen3.8-27B, 14 for Nemotron 3.5 Super VL, 33 for DeepSeek-V4.1-Flash; on SWE-rebench, 31.2% for Qwen3.6-27B and 24.7% for Qwen3.6-35B-A3B. Qwen3.8 is on no independent agent benchmark yet | P | https://artificialanalysis.ai/models/open-source ; https://swe-rebench.com |
| Publisher results: Qwen3.8-27B 61.7 on SWE-bench Pro, run in Claude Code, and 73.0 on Terminal-Bench 2.1; Qwen3.8-Flash-Next 62.5 on SWE-bench Pro and 58.7 on DeepSWE against the 27B's 42.2 | P for what the cards say; the numbers are the publisher's | The two model cards |
| Qwen3.8-Flash-Next's licence: "If the licensee or any of its affiliates conducts a Model as a Service or AI Work Assistant business, the licensee shall obtain a separate license from Qwen before Using the Software or its derivative works for any commercial purpose." It defines Model as a Service as "giving a third party access to language model inference or fine-tuning (e.g., via API or a hosted endpoint)", and exempts "internal Use of the Software, provided that such Use does not make the Software, its outputs, or its underlying model capabilities available to any third party" | L | `https://huggingface.co/Qwen/Qwen3.8-Flash-Next/raw/main/LICENSE`. How it applies to a university course is a legal question this file does not answer |
| A next Qwen generation is announced; no weights, licence or date are published | T | Third-party articles; the publisher's model list shows none |
| Whether the 8-bit build loads on the G7e GPU, whether an image inside a tool result survives, whether Codex's tool namespaces work with this model, and whether the effort mapping clears every refusal | — | Gap: spikes S3 and S5 |

## E29. Performance measurements, and the arithmetic of section 4.7

| Claim | Level | Source |
| --- | --- | --- |
| RTX PRO 6000 Blackwell Server Edition: 96 GB, memory bandwidth 1,597 GB/s. AWS: G7e has "1.85 times the GPU memory bandwidth" of G6e and "up to 2.3 times inference performance" | P | NVIDIA's product page; the AWS launch post for G7e |
| No published vLLM measurement on a G7e instance, of Qwen3.8-27B on the RTX PRO 6000, or of Qwen3.8-Flash-Next on four of them | — | Not found. Every coursework and upgrade number in section 4.7 is an estimate |
| Qwen3.8-27B at 4 bits on one L40S, vLLM 0.31.0, short prompts: 43.7 tokens/s for one stream; 554 tokens/s in total at 32 streams; with the model's draft head and 3 draft tokens, 65.0 tokens/s for one stream and 215 against 143 in total at 4 streams | T | https://computingforgeeks.com/ollama-vs-vllm-vs-llama-cpp/ |
| A real Claude Code session of 52,000 to 65,000 tokens replayed on Qwen3.8-27B (4-bit, vLLM 0.32.0, a small development machine): as sent, about 19,000 tokens hit the cache each turn and the first token took 30 to 43 s; with each inline system message rewritten as user content in place, 51,200 to 60,800 tokens hit and the first token took 1.9 to 5.9 s | L | https://github.com/vllm-project/vllm/issues/63331 . One reporter's measurement; it implies a prefill near 1,100 tokens/s on that machine |
| An open defect: the draft head of Qwen3.8-27B diverges from plain decoding with 5 or more draft tokens | L for the issue | https://github.com/vllm-project/vllm/issues/63593 |
| A 30B model with 3B active parameters, 8-bit, on one RTX PRO 6000: 142 tokens/s for one stream at a 1,000-token context, 106 at 32,000, 73 at 96,000; peak prefill 36,943 tokens/s; first token after 1.7, 5.8 and 12.2 s for cold prompts of 32,000, 64,000 and 96,000 tokens | T | https://www.millstoneai.com/inference-benchmark/qwen3-coder-30b-a3b-instruct-fp8-1x-rtx-pro-6000-blackwell |
| gpt-oss-120b (5B active) on one RTX PRO 6000, short context: 181 tokens/s per stream alone, 80 at 8 streams, 44 at 32. A 122B model with 10B active on two of them: 91 tokens/s for one stream | T | https://whitepapers.openzeka.com/llm-inference-benchmarks/gpt-oss-120b/rtx-pro-6000/mxfp4-vllm-tp1/ ; https://eordano.github.io/rtx-6000-research/ |
| DeepSeek-V4.1-Flash on 8 × H200, vLLM 0.33.0, 4-bit weights, draft head on, replaying recorded Claude Code sessions: per stream 375.9 tokens/s alone, 295.9 at 8, 215.1 at 16, 142.9 at 32, 60.4 at 64; 1,239 output tokens/s for the machine at 32 and 2,227 at 64; median time to first token 0.24 to 0.36 s up to 64 | T | InferenceX raw results (SemiAnalysis), read through its API. The pages carried a notice that the copy may be out of date |
| The same run on the L40S at 4 streams: 143 tokens/s in total, so 35.8 a stream against 43.7 alone, with prompts too short for the cached transcript to matter. With 32 prompts of 474 tokens, the first token came after 3.0 s | T | Same source as the L40S row above; the figure per stream is the total divided by four |
| vLLM on the step budget: "Smaller values (e.g., 2048) achieve better ITL because there are fewer prefills slowing down decodes"; larger values favour the time to first token | P | `docs/configuration/optimization.md` at `v0.33.0` |
| Section 4.7, one stream alone: 30.9 GB of weights read for each token against 1,597 GB/s is a bound of 52 tokens/s. The estimates take 77 to 87% of a bound; the L40S run above is at 80% or more of its own | M | Arithmetic. The efficiency range is the main assumption of the section |
| Section 4.7, 8 streams, decode alone: each step reads the weights once and every stream's cached transcript, 8 × 50,000 to 100,000 tokens × 32 KiB, so 44 to 57 GB a step: 28 to 36 steps a second at the bound, 20 to 30 at the assumed efficiency | M | Arithmetic |
| Section 4.7, 8 agent streams: the row above less the GPU time spent computing the streams' new input (8 streams, 1,000 to 5,000 new tokens a turn, a turn every 28 to 43 s, against a prefill of 4,000 to 6,000 tokens/s: 3 to 36%, about a sixth at mid values), less the cost per extra stream seen on the L40S (10 to 15% at 8 streams): 15 to 22 | M | Arithmetic |
| Section 4.7, the 4-bit build: 21.9 GB read for each token, a bound of 73; by the same steps, 18 to 28 tokens/s at 8 agent streams | M | Arithmetic |
| Section 4.7, prefill: the 30B model above reaches 36,943 tokens/s with 3.3B active parameters, which is about 240 TFLOPS of useful work; over the 27.8B parameters of the dense model, about 4,400 tokens/s. The L40S run above implies 5,000 tokens/s or more on short prompts on a GPU about half as fast | M | Arithmetic |
| Section 4.7, cache capacity: 90% of 95.6 GiB, less 28.8 GiB of weights and 3 to 6 GiB of working memory, leaves 51 to 54 GiB; at 32 to 48 KiB a token with an 8-bit cache that is 1.1 to 1.8 million tokens. The model's shape gives 32 KiB; the single-GPU run in E28 puts the cost under 48 and shows the 8-bit cache holding twice the 16-bit one | M | Arithmetic |
| Section 4.7, students per instance: a turn of 500 tokens takes 23 to 33 s at 15 to 22 tokens/s; with 5 to 10 s of tools between turns, a running agent holds a stream 70 to 87% of the time, so 8 streams carry about 10 running agents. Hands-on, between half and all of the students have one running: 10 to 20. With one or two streams a student almost all the time: 4 to 8 | M | Arithmetic on assumed gaps and shares; nothing published. The weakest assumption of the coursework table |
| Section 4.7, the upgrade pool: speeds scaled from the two measurements of models with 5B and 10B active parameters above, less an allowance for four GPUs joined over PCIe | M | Judgment; the weakest numbers of the section |
| When the cache is full, the engine preempts a running request and recomputes it later, and new requests wait. A finished request's blocks stay cached until the space is needed, oldest first. A cache salt is mixed into the hash of the first block, so only requests with the same salt share anything | P | `docs/configuration/optimization.md` and `docs/design/prefix_caching.md` at `v0.33.0` |

## E30. Ray Serve

| Claim | Level | Source |
| --- | --- | --- |
| Latest release: Ray 2.60.0, published 2026-11-04 | L | `gh api repos/ray-project/ray/releases/latest` |
| The stable front door of Ray Serve's LLM module registers `/v1/models`, `/v1/completions`, `/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/transcriptions` and `/v1/score`. The file names neither `/v1/messages` nor `/v1/responses` | L | `python/ray/llm/_internal/serve/core/ingress/ingress.py` at tag `ray-2.60.0` |
| The pull request adding the Responses route (#65773) and the one adding the Messages route (#65486) are closed and were not merged. A pull request documenting Messages "via direct streaming" (#66230) is open | L | `gh api repos/ray-project/ray/pulls/65773`, `.../65486`, `.../66230` |
| Both routes are reachable only in an experimental "direct streaming" mode, where a replica serves vLLM's own application for one model | P | Ray Serve LLM documentation and source |
| Ray merged "Support Anthropic `/v1/messages` in `KVAwareRouter`" (#66827) on 2026-10-09; it is not in the 2.60.0 release | L | `gh api repos/ray-project/ray/pulls/66827`; `gh api .../compare/<merge commit>...ray-2.60.0` |
| Ray 2.60.0 requires `vllm[audio]==0.29.0`. vLLM 0.29.0 is from 2026-09-09; 0.33.0, four minor releases later, had been out for two days when Ray 2.60.0 shipped | L | `python/setup.py` at tag `ray-2.60.0`; E3 |
| An earlier Ray image could not serve a new model because of its pinned vLLM | L for the issue and its title | https://github.com/ray-project/ray/issues/66486 |
| Scale to zero exists (`min_replicas = 0`); the proxy holds a request with no keep-alive while a replica starts | P | Ray Serve autoscaling documentation |
| On plain EC2, Ray's cluster launcher creates and stops instances itself; it reuses stopped nodes it has launched before (`cache_stopped_nodes`), and has no provider for Auto Scaling groups or warm pools. The head node must stay up; fault tolerance for the cluster's control store is documented for Kubernetes only | P | Ray cluster launcher and fault-tolerance documentation |
| Ray's documentation says a cluster lets any client that reaches it run arbitrary code, and expects a trusted network. Token authentication is one shared secret and is not yet the default | P | Ray security documentation, read through a summarising fetch; the wording is not a verbatim quote |
| A remote-code-execution flaw in Ray (CVE-2025-62593) was added to CISA's list of exploited vulnerabilities on 2026-08-17 | L | CISA's Known Exploited Vulnerabilities catalogue, version 2026.11.13 |
| Ray's prefix-aware router does not take vLLM's `cache_salt` into account | P | Router source |
| What the proxy costs in latency: one user's measurement of 17 to 33 ms at the median for time to first token at concurrency 1, and 114 to 758 ms at concurrency 64 | T | A user's benchmark |
| What a client sees during a cold start under direct streaming, and how fast Ray restarts a stopped node | — | Not found |

## E31. What agent traffic looks like, and how to replay it

| Claim | Level | Source |
| --- | --- | --- |
| In replays of recorded Claude Code sessions: input per request has a median of 74,000 to 130,000 tokens and a mean of 106,000 to 184,000; output has a median of 325 to 513 and a mean of 955 to 1,431; 91 to 98% of input tokens hit the cache; a session sends a request every 12 to 25 s | T | InferenceX raw results for two models on 8 × H200; the request interval is derived from the request rate and the concurrency |
| Other reports of the cached share: 92% in one operator's production agent traffic, 91% in a public trace set | P | https://llm-d.ai/blog/from-agent-traces-to-replay |
| A public, licensed set of agent sessions exists for replay: 52 developers, 8,058 sessions, 665,453 model calls, Claude Code and Codex | P | NVIDIA's AIPerf documentation for the TraceLab traces |
| Tools that replay multi-turn agent sessions with growing transcripts against an OpenAI-compatible endpoint include AIPerf, GuideLLM and Inference-Perf; `vllm bench serve` sends independent requests and reports goodput against given limits | P for AIPerf and `vllm bench serve`; T for the others | Tool documentation; the llm-d post above |
| Definitions used in section 4.7: time to first token from submission to first token; time per output token as decode time over output tokens minus one; system throughput as output tokens over elapsed time | P | https://docs.nvidia.com/nim/benchmarking/llm/latest/metrics.html ; vLLM's source (E3) |
| Students' sessions will be lighter than those traces | M | Assumption; spike S5 and the first session's dashboard will show |

## E32. GPU capacity for the coursework pool

Observed in the owner's account between 2026-11-23 and 2026-11-25, in the region the endpoint runs in, which this file does not name. The AWS pages were read on 2026-11-24 and 2026-11-25.

| Claim | Level | Source |
| --- | --- | --- |
| Over about 23 hours, the coursework pool's group (one type, `g7e.2xlarge`, On-Demand, three zones) made 121 launch attempts: 9 succeeded, 77 found insufficient capacity in the two zones that offer the type, and 35 were placed in the third zone, which does not offer it | L | The group's scaling activities, read with describe calls on 2026-11-24 |
| The pool needed an instance 9 times. It got one at once 3 times, and waited 21, 25, 29, 29, 29 and 37 minutes the 6 other times; the waits began at every time of day | L | Same |
| The pool's stopped warm-pool instance was started 5 times, once for each wake from zero. All 5 starts failed for insufficient capacity; each time the group terminated the instance and launched a new one, which arrived 25 to 37 minutes later | L | Same |
| The scaler applied each of its decisions within seconds: 5 wakes, and 5 returns to zero after the idle time. The pool's maximum was 1, so growth beyond one instance was never exercised | L | Same, and the scaler's log |
| Three of the pipeline's verifications failed only because the instance arrived 1 to 7 minutes after the check stopped waiting | L | The pipeline's executions of the same day |
| With the group spanning only the zones that offer its type, and with the next sizes of the same GPU and a single-GPU type of another family listed as fallbacks, a gap of about 30 minutes still found no capacity for any listed type in any zone | P | The same activities on 2026-11-25, as the operator reported them; not read again |
| AWS's Spot placement score, from 1 to 10, in that region: 4 for one `g7e.2xlarge`, 1 for three, and 8 for one when the request also accepts the two next sizes. Three other regions that offer the type scored 2, 6 and 8 for one instance | L | `ec2 get-spot-placement-scores`, 2026-11-24 |
| The score is a recommendation for Spot requests, which "does not provide any guarantees in terms of available capacity"; AWS documents no such signal for On-Demand | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-placement-score.html |
| After a launch finds no capacity, Auto Scaling looks for it in the group's other zones, and "continues to automatically make the launch request until capacity becomes available". The time between two requests is not documented | L | https://docs.aws.amazon.com/autoscaling/ec2/userguide/ts-as-capacity.html |
| A group fills its On-Demand capacity with its instance types in the order of its overrides (`prioritized`). That it goes on to the next type when the first has no capacity follows from AWS's advice to list several types; no page states it for this strategy | P | https://docs.aws.amazon.com/autoscaling/ec2/userguide/allocation-strategies.html ; https://docs.aws.amazon.com/autoscaling/ec2/userguide/mixed-instances-groups-set-up-overview.html |
| "Amazon EC2 Auto Scaling can also suspend processes for Auto Scaling groups that repeatedly fail to launch instances", most commonly groups "that have been trying to launch instances for over 24 hours but have not succeeded in launching any instances"; such processes can be resumed | L | https://docs.aws.amazon.com/autoscaling/ec2/userguide/as-suspend-resume-processes.html |
| Scale-in protection "starts when the instance state is `InService`". When every instance of a group is protected and a scale-in occurs, the desired capacity is decremented and no instance is terminated until its protection is lifted | L | https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-instance-protection.html |
| A stopped instance keeps no claim on hardware: at its start it is usually placed on another host, and insufficient capacity is a documented result of starting one. It can start only in the zone of its volume | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/how-ec2-instance-stop-start-works.html ; https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/troubleshooting-launch.html ; the zone follows from https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes.html |
| An On-Demand Capacity Reservation is charged at the On-Demand rate from its creation to its cancellation, by the second, whether or not an instance runs in it. It has no commitment. It is for one zone, one instance type, one platform and one tenancy. Creating one "could fail if ... Amazon EC2 does not have sufficient capacity" | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-reservations-pricing-billing.html ; https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-reservations.html ; https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-reservations-create.html |
| An instance with the default preference runs in a matching open reservation, also when it is started from stopped. A group can be told to use reservations first, or only | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-reservations-modify-instance.html ; https://docs.aws.amazon.com/autoscaling/ec2/userguide/target-capacity-reservations.html |
| A reservation for a future date is requested 5 to 120 days ahead, for at least 32 vCPUs and, for the G family, a commitment of 14 days; AWS may decline it | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-reservations-create.html |
| Capacity Blocks exist for P and Trainium types and for no G type, and a group whose launch template targets one cannot have a warm pool | P | https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html ; https://docs.aws.amazon.com/autoscaling/ec2/userguide/capacity-reservation-create-asg-procedure.html |
| How often, and for how long, the type is without capacity on another day, or in another region | — | Gap: one day in one account. The design does not depend on the figure (section 5.4) |

## Known gaps

Stated once more, because they are where the design could be wrong:

1. How long an instance that is asked for waits for capacity is known from one day in one account (E32). The design does not depend on the figure, only on the instructor booking ahead of it: on a day with a longer gap, a session starts short (section 5.4).
2. How long a launch takes on the coursework pool's instance type, from launch to serving, is an estimate from published stage timings (E11). Every launch records its stages, and S1 reads them.
3. Claude Code on open-weight models is unsupported by its vendor and has known engine-side defects. The two that stop the coursework model are worked around in the gateway, on the strength of one reporter's measurement (S3).
4. What one instance serves is arithmetic, not a measurement: no one has published a benchmark of the coursework model on its GPU, or of the upgrade model on its instance (S5, S6).
5. The upgrade model has an upstream recipe verified on four of its GPU, but nobody has run it on the AWS instance type named here (S6). The coursework model's 8-bit build is verified on its GPU's generation, not on its GPU (S3, S5).
6. The coursework model's quality rests on its publisher's numbers, one independent index and its predecessor's independent result (S3).
7. The fixed monthly costs in section 11.2 are estimates from list prices; the token volume behind the Bedrock comparison is an estimate from other people's sessions (E31). The price of the upgrade pool's instance type comes from a price tracker (E5).
8. The owner's actual GPU quotas were not read, and nobody documents how long an increase takes.
9. Claims without a spike that the design still leans on: the minimum length of a Capacity Block (E7), the SageMaker and ECS behaviours used to reject those options (E8, E9), and the Ray Serve behaviours used to reject it (E30), which were read from documentation and source and not tried.
10. Keeping a student on one instance by replaying the load balancer's cookie follows from documented behaviour and has not been tried; AWS lists a requirement that a small pool does not meet (E13, S8).
11. Claude Code's assumed 200,000-token window, and the setting that lowers its compaction point, come from search summaries, not from its documentation read directly (E15, S3).
12. What the second session's research packs need behind vLLM is known from one upstream issue and from memory: PDFs are refused, tools and skills added mid-session pass through a rewrite nobody has tested, and built-in web search is believed absent (E28, S3).
13. Which build of the coursework model to serve is open: the publisher's 8-bit build has not been run on its GPU, and what 4 bits cost in tool-call accuracy is unmeasured (E28, S3, S5).
14. Students per instance rests on assumed gaps between turns and an assumed share of students with an agent running (E29, S5).
15. What Auto Scaling does with an instance still starting when desired capacity is lowered, and how often it repeats a launch that found no capacity, are not documented (E32, S7).
