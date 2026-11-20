"""The arguments a vllm pool's engine is started with (system design, sections 4.1, 4.3 and 4.4).

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
- `--load-format runai_streamer`: `--load-format` in `EngineArgs.add_cli_args`, from
  `LoadConfig.load_format` in `vllm/config/load.py`, which lists `runai_streamer`;
- `--port 8000`: `FrontendArgs.port`;
- `--allowed-media-domains media.invalid`: `--allowed-media-domains` in `EngineArgs.add_cli_args`,
  from `ModelConfig.allowed_media_domains` in `vllm/config/model.py`, a list of domains.

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

**The model.** Weights load straight from S3 into GPU memory with the Run:ai streamer (section
4.4). The model is the pool's prefix in the model bucket at the pool's revision,
`s3://<model bucket>/<pool>/<revision>/`: the layout of `rhi.pools.model_store`, which the copy job
writes. vLLM reads a model whose name starts with `s3://` from object storage
(`SUPPORTED_SCHEMES` in `vllm/transformers_utils/runai_utils.py`): it fetches the small files
beside the weights to a local directory (`ModelConfig.maybe_pull_model_tokenizer_for_runai`) and
the streamer loads the weights. The bucket's name is the caller's, from
`rhi.pools.model_store.bucket_name`: it holds the account and the region, which only a deployment
knows.

**Media only from inline data.** vLLM has no option that turns fetching a media URL off. With an
allow-list, `MediaConnector` in `vllm/multimodal/media/connector.py` refuses an `http` or `https`
URL whose host is not on the list (`_assert_url_in_allowed_media_domains`), and reads a `data:`
URL before it looks at any host (`load_from_url`). The list names one domain, under `.invalid`,
which RFC 6761 reserves and which never resolves: every URL is refused, and inline data is read
as before. A `file:` URL is refused because no local media path is allowed.

**Logging.** Prompts and responses are not stored (section 6.6), and the engine's log goes to the
pool's log group, so neither request logging option is ever passed.

This module imports nothing of the CDK: the unit tests call it as it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rhi_common.pool_config import Pool

# The port every engine listens on (section 4.1; `rhi.network.security.ENGINE_PORT`).
PORT = 8000
# vLLM's name for loading weights with the Run:ai streamer.
LOAD_FORMAT = "runai_streamer"
# The one domain media may be fetched from: reserved by RFC 6761, it never resolves.
MEDIA_DOMAIN = "media.invalid"


class VllmCommandError(ValueError):
    """A pool cannot be given vLLM's arguments. The message names the pool, never a value."""


def model_location(pool: Pool, bucket: str) -> str:
    """Where the pool's weights are: its prefix in the model bucket named `bucket`, at its revision."""
    if pool.engine != "vllm" or pool.revision is None:
        raise VllmCommandError(f"pool {pool.name!r}: only a vllm pool, which pins a revision, has vLLM's arguments")
    return f"s3://{bucket}/{pool.name}/{pool.revision}/"


def arguments(pool: Pool, *, bucket: str) -> list[str]:
    """What follows `vllm serve` for a vllm pool, in order: the pool's engine flags.

    `bucket` is the model bucket's name, which may hold deployment-time values.
    """
    built = [
        model_location(pool, bucket),
        *("--load-format", LOAD_FORMAT),
        *("--port", str(PORT)),
        *("--allowed-media-domains", MEDIA_DOMAIN),
    ]
    if pool.trust_remote_code:
        # A reviewed exception, which the pool records with its reason (section 4.4).
        built.append("--trust-remote-code")
    return built
