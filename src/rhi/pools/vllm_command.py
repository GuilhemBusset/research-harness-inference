"""The arguments a vllm pool's engine is started with (system design, sections 4.1 to 4.4, 4.7 and 7.2).

A pool's release carries its engine flags, a list of arguments (`rhi.pools.release`), and the
engine's container receives them after `vllm serve`, which the image's entry point runs
(`services/engine/vllm/entrypoint.sh`). `arguments` builds that list from the pool's
configuration when the app synthesizes. What vLLM reads from its environment is the same for
every vllm pool and is set in the image; the secret reaches vLLM in `VLLM_API_KEY`, from the entry
point, and is in no argument and in no release.

**The names are vLLM's at version 0.33.0**, each read in vLLM's source at the tag `v0.33.0`
(github.com/vllm-project/vllm). The server's parser is `make_arg_parser`, in
`vllm/entrypoints/common/cli_args.py`: it adds the positional `model_tag`, then one option for
every attribute of `FrontendArgs` and of its base `BaseFrontendArgs` in the same file, named after
the attribute with hyphens for underscores (`BaseFrontendArgs.add_cli_args`), then the options of
`AsyncEngineArgs.add_cli_args` and `EngineArgs.add_cli_args`, in `vllm/engine/arg_utils.py`, which
name each option in full and take its type and default from an attribute of a configuration class.

**What every vllm pool is given**, and where the tag defines it:

- the model, first and by position: `model_tag` in `make_arg_parser`, which `ServeSubcommand.cmd`
  in `vllm/entrypoints/cli/serve.py` makes the model;
- `--load-format runai_streamer`, for a pool on the streamer: `--load-format` in
  `EngineArgs.add_cli_args`, from `LoadConfig.load_format` in `vllm/config/load.py`, which lists
  `runai_streamer`. Left out, as for a pool on download, it is `auto`, vLLM's default loader;
- `--port 8000`: `FrontendArgs.port`;
- `--allowed-media-domains media.invalid`: `--allowed-media-domains` in `EngineArgs.add_cli_args`,
  from `ModelConfig.allowed_media_domains` in `vllm/config/model.py`, a list of domains;
- `--served-model-name=<the pool's name>`: `--served-model-name` in `EngineArgs.add_cli_args`,
  from `ModelConfig.served_model_name`, a list of names, so an option of one value or more. Left
  out, the served name would be the model's location;
- `--max-model-len <the pool's context length>`: `--max-model-len` in `EngineArgs.add_cli_args`,
  from `ModelConfig.max_model_len`, the context of a prompt and its output, in tokens;
- `--max-num-seqs <the pool's target concurrency>`: `--max-num-seqs` in
  `EngineArgs.add_cli_args`, from `SchedulerConfig.max_num_seqs` in `vllm/config/scheduler.py`,
  the most requests running at once;
- `--kv-cache-metrics`: `--kv-cache-metrics` in `EngineArgs.add_cli_args`, from
  `ObservabilityConfig.kv_cache_metrics` in `vllm/config/observability.py`, a boolean that is
  false when left out.

**What a pool is given only when its serving settings ask** (`rhi_common.pool_config`). A setting
the pool leaves out, or gives the value vLLM has by itself, adds no argument:

- `--kv-cache-dtype fp8`, for a cache of 8 bits: `--kv-cache-dtype` in `EngineArgs.add_cli_args`,
  from `CacheConfig.cache_dtype` in `vllm/config/cache.py`. Left out, it is `auto`, the model's
  own data type: the 16 bits.
- `--no-enable-prefix-caching`, for a pool that turns prefix caching off: `--enable-prefix-caching`
  in `EngineArgs.add_cli_args`, from `CacheConfig.enable_prefix_caching`, a boolean, which
  `get_kwargs` gives argparse's `BooleanOptionalAction`, and with it the form that starts with
  `--no-`. Left out, vLLM turns prefix caching on for a model that supports it
  (`EngineArgs._set_default_chunked_prefill_and_prefix_caching_args`).
- `--max-num-batched-tokens <the step budget>`: `--max-num-batched-tokens` in
  `EngineArgs.add_cli_args`, from `SchedulerConfig.max_num_batched_tokens`, the most tokens of one
  engine step. Left out, vLLM chooses it
  (`EngineArgs._set_default_max_num_seqs_and_batched_tokens_args`).
- `--enable-auto-tool-choice` and `--tool-call-parser <the parser>`:
  `BaseFrontendArgs.enable_auto_tool_choice` and `BaseFrontendArgs.tool_call_parser`. vLLM refuses
  the first without the second, so they come together. Left out, no tool call is parsed.
- `--reasoning-parser <the parser>`: `--reasoning-parser` in `EngineArgs.add_cli_args`, from
  `StructuredOutputsConfig.reasoning_parser` in `vllm/config/structured_outputs.py`. Left out,
  reasoning is not separated from the answer.
- `--custom-histogram-buckets <a JSON object>`: `--custom-histogram-buckets` in
  `EngineArgs.add_cli_args`, from `ObservabilityConfig.custom_histogram_buckets`, which maps a
  bucket family to its boundaries: each a positive number, in increasing order. A family that is
  not in the object keeps vLLM's boundaries (`vllm/v1/metrics/buckets.py`). The tag knows eight
  families; a pool's configuration holds the three that are times (section 7.2).
- `--speculative-config {"method":"mtp","num_speculative_tokens":<the pool's number>}`, for a pool
  that turns the model's draft head on: `--speculative-config` in `EngineArgs.add_cli_args`, a
  JSON object that becomes a `SpeculativeConfig` of `vllm/config/speculative.py`. The object is
  the one of vLLM's recipe for the coursework model (`models/Qwen/Qwen3.8-27B.yaml` in
  github.com/vllm-project/recipes, the mode `mtp` of `spec_decoding`), which says to give the
  number of tokens: vLLM cannot read it from this model's configuration. Left out, there is no
  speculative decoding (section 4.7).

**What a pool is given only when it records its exception** (`trust_remote_code`, with its
reason, in `rhi_common.pool_config`):

- `--trust-remote-code`: `--trust-remote-code` in `EngineArgs.add_cli_args`, from
  `ModelConfig.trust_remote_code`. Left out, vLLM runs no code of the model's repository.

**What no pool is ever given**, each with what vLLM does when it is left out:

- `--profiler-config`, whole or by its parts as `--profiler-config.<part>`: `--profiler-config`
  in `EngineArgs.add_cli_args`, from `VllmConfig.profiler_config` in `vllm/config/vllm.py`, a
  `ProfilerConfig` of `vllm/config/profiler.py`. Left out, there is no profiler, and `attach_router`
  in `vllm/entrypoints/serve/profile/api_router.py` adds no profiling route.
- `--trust-request-chat-template`: `BaseFrontendArgs.trust_request_chat_template`. Left out, a
  request's own chat template is refused.
- `--allowed-local-media-path`: `--allowed-local-media-path` in `EngineArgs.add_cli_args`, from
  `ModelConfig.allowed_local_media_path`. Left out, no request reads a file of the instance.
- `--enable-log-requests`: `--enable-log-requests` in `AsyncEngineArgs.add_cli_args`. Left out, no
  request is logged.
- `--enable-log-outputs`: `BaseFrontendArgs.enable_log_outputs`. Left out, no response is logged.
- `--api-key`: `FrontendArgs.api_key`. Left out, vLLM takes its key from `VLLM_API_KEY`
  (`vllm/envs.py`, read in `vllm/entrypoints/serve/middleware/register.py`).
- `--reasoning-effort-map`: `BaseFrontendArgs.reasoning_effort_map`, which rewrites a request's
  reasoning effort. Left out, vLLM passes the effort as the request gives it. The gateway's
  request profile maps the effort (section 6.2), where a unit test proves it without a GPU, and
  the engine must not map it a second time.

**The model.** By default, weights load straight from S3 into GPU memory with the Run:ai streamer
(section 4.4). The model is the pool's prefix in the model bucket at the pool's revision,
`s3://<model bucket>/<pool>/<revision>/`: the layout of `rhi.pools.model_store`, which the copy job
writes. vLLM reads a model whose name starts with `s3://` from object storage
(`SUPPORTED_SCHEMES` in `vllm/transformers_utils/runai_utils.py`): it fetches the small files
beside the weights to a local directory (`ModelConfig.maybe_pull_model_tokenizer_for_runai`) and
the streamer loads the weights. The bucket's name is the caller's, from
`rhi.pools.model_store.bucket_name`: it holds the account and the region, which only a deployment
knows.

**The fallback loader.** A pool whose `loader` is `download` (`rhi_common.pool_config`) is not
streamed: its model is copied out of S3 to a local folder before vLLM starts, and vLLM loads it
from there with its default loader (section 4.4; evidence E11). vLLM copies nothing out of S3 by
itself, so the image carries a program of this project for it, `services/engine/vllm/fetch_model.py`,
and the pool's arguments begin with one argument of this project, before vLLM's own:
`--rhi-fetch-model=<the model's location>,<the local folder>`. The image's entry point runs the
program with it and takes it off before it starts vLLM with the rest (`entrypoint.sh`). Then comes
the folder, by position, as vLLM's model, and the arguments every pool has, without
`--load-format`. The folder, `MODEL_FOLDER`, is in the container's own file system, under none of
the folders the engine's unit mounts into it (`services/engine/units/rhi-engine.service`): Docker
keeps that file system on the instance's EBS volume. The container is removed when it stops, so
the copy is made at every start. A pool on `stream`, the default, has no such argument, and its
arguments are those above.

**Media only from inline data.** vLLM has no option that turns fetching a media URL off. With an
allow-list, `MediaConnector` in `vllm/multimodal/media/connector.py` refuses an `http` or `https`
URL whose host is not on the list (`_assert_url_in_allowed_media_domains`), and reads a `data:`
URL before it looks at any host (`load_from_url`). The list names one domain, under `.invalid`,
which RFC 6761 reserves and which never resolves: every URL is refused, and inline data is read
as before. A `file:` URL is refused because no local media path is allowed.

**Logging.** Prompts and responses are not stored (section 6.6), and the engine's log goes to the
pool's log group, so neither request logging option is ever passed.

**The served name.** Students choose a pool by its name and the gateway forwards that name
(section 4.2), so the engine serves the model under it. A pool's name holds only lower-case
letters, digits and hyphens (`rhi_common.pool_config`): never a slash, which Claude Code refuses
in a model's name (evidence E28). A name may start with a hyphen, and argparse reads an argument
that starts with one as an option, not as a value: the name is written in the option's own
argument, after `=`, where every name a pool may have is a value. `FlexibleArgumentParser`
(`vllm/utils/argparse_utils.py`) passes an argument of that form on to argparse as it is.

**The cache metrics.** With `--kv-cache-metrics` the engine samples how long a block of its cache
sat idle before it was evicted, `vllm:kv_block_idle_before_evict_seconds` (section 7.2).

This module imports nothing of the CDK: the unit tests call it as it is.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rhi_common.pool_config import BUCKET_FAMILIES

if TYPE_CHECKING:
    from rhi_common.pool_config import Pool

# The port every engine listens on (section 4.1; `rhi.network.security.ENGINE_PORT`).
PORT = 8000
# vLLM's name for loading weights with the Run:ai streamer.
LOAD_FORMAT = "runai_streamer"
# The argument of this project that asks the image's entry point for a copy of the model, which it
# takes off before vLLM starts (`services/engine/vllm/entrypoint.sh` and `fetch_model.py`).
FETCH_OPTION = "--rhi-fetch-model"
# Where a pool on download has its model copied to, in the container's own file system.
MODEL_FOLDER = "/var/lib/rhi-model"
# The one domain media may be fetched from: reserved by RFC 6761, it never resolves.
MEDIA_DOMAIN = "media.invalid"
# vLLM's name for a cache of 8 bits. A cache of 16 bits is the model's own data type: no argument.
KV_CACHE_8_BITS = "fp8"
# vLLM's name for speculative decoding with the draft head the model's checkpoint holds.
DRAFT_HEAD_METHOD = "mtp"


class VllmCommandError(ValueError):
    """A pool cannot be given vLLM's arguments. The message names the pool, never a value."""


def model_location(pool: Pool, bucket: str) -> str:
    """Where the pool's weights are: its prefix in the model bucket named `bucket`, at its revision."""
    if pool.engine != "vllm" or pool.revision is None:
        raise VllmCommandError(f"pool {pool.name!r}: only a vllm pool, which pins a revision, has vLLM's arguments")
    return f"s3://{bucket}/{pool.name}/{pool.revision}/"


def fetch_argument(location: str, folder: str) -> str:
    """The argument that has the image's entry point copy every file under `location` to `folder`."""
    if "," in location or "," in folder:
        raise VllmCommandError("a model's location and its local folder hold no comma")
    return f"{FETCH_OPTION}={location},{folder}"


def _json(value: object) -> str:
    """A JSON object as one argument, with no space, as vLLM's recipes write one."""
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def arguments(pool: Pool, *, bucket: str) -> list[str]:
    """What follows `vllm serve` for a vllm pool, in order: the pool's engine flags.

    `bucket` is the model bucket's name, which may hold deployment-time values.
    """
    location = model_location(pool, bucket)
    if pool.loader == "download":
        # The copy first, which the entry point takes off, then the local folder as vLLM's model,
        # with vLLM's default loader.
        model = [fetch_argument(location, MODEL_FOLDER), MODEL_FOLDER]
    else:
        model = [location, "--load-format", LOAD_FORMAT]
    built = [
        *model,
        *("--port", str(PORT)),
        *("--allowed-media-domains", MEDIA_DOMAIN),
        # In one argument: a pool's name may start with a hyphen, which alone would read as an option.
        f"--served-model-name={pool.name}",
        *("--max-model-len", str(pool.context_length)),
        *("--max-num-seqs", str(pool.target_concurrency)),
        "--kv-cache-metrics",
    ]
    # The serving settings: one that is as vLLM runs by itself adds nothing.
    if pool.kv_cache_bits == 8:
        built += ["--kv-cache-dtype", KV_CACHE_8_BITS]
    if not pool.prefix_caching:
        built.append("--no-enable-prefix-caching")
    if pool.step_budget_tokens is not None:
        built += ["--max-num-batched-tokens", str(pool.step_budget_tokens)]
    if pool.tool_call_parser is not None:
        built += ["--enable-auto-tool-choice", "--tool-call-parser", pool.tool_call_parser]
    if pool.reasoning_parser is not None:
        built += ["--reasoning-parser", pool.reasoning_parser]
    if pool.histogram_buckets is not None:
        boundaries = {family: getattr(pool.histogram_buckets, family) for family in BUCKET_FAMILIES}
        replaced = {family: list(given) for family, given in boundaries.items() if given is not None}
        if replaced:
            built += ["--custom-histogram-buckets", _json(replaced)]
    if pool.draft_head_tokens is not None:
        # Off until spike S5 shows it helps and S3 that it changes no tool call (section 4.7).
        speculation = {"method": DRAFT_HEAD_METHOD, "num_speculative_tokens": pool.draft_head_tokens}
        built += ["--speculative-config", _json(speculation)]
    if pool.trust_remote_code:
        # A reviewed exception, which the pool records with its reason (section 4.4).
        built.append("--trust-remote-code")
    return built
