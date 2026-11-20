"""The arguments a vllm pool's engine is started with (`rhi.pools.vllm_command`; system design,
sections 4.1, 4.2 and 4.4): what the engine contract asks of every vllm pool, the one exception a
pool may record, and the serving settings of a pool's release.

The names are vLLM's at version 0.33.0, written here as the contract's table gives them, not read
from the module. The revision is made when the test runs and names no real commit.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from rhi.pools import vllm_command
from rhi_common.pool_config import parse_pools

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rhi_common.pool_config import Pool

REVISION = hashlib.sha1(b"a pinned commit", usedforsecurity=False).hexdigest()
BUCKET = "rhi-models-123456789012-eu-west-3"

VLLM = {
    "engine": "vllm",
    "model": "example/model",
    "revision": REVISION,
    "instance_types": ["g7e.2xlarge"],
    "prices": {"g7e.2xlarge": 3.36},
    "max_instances": 2,
    "warm_instances": 2,
    "wake": "on-demand",
    "target_concurrency": 8,
    "gpu_hours_per_day": 16,
    "context_length": 262144,
    "largest_output": 32000,
}
MOCK = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.02},
    "max_instances": 1,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 8,
    "context_length": 8192,
    "largest_output": 1024,
}
EXCEPTION: dict[str, object] = {"trust_remote_code": True, "trust_remote_code_reason": "the model's repository holds its layers"}

# What the contract's table marks absent among the arguments of every vllm pool.
ABSENT = (
    "--profiler-config",
    "--trust-request-chat-template",
    "--allowed-local-media-path",
    "--enable-log-requests",
    "--enable-log-outputs",
    "--api-key",
)
TRUST = "--trust-remote-code"

# The boundaries a pool gives the three bucket families, in seconds (section 7.2).
BUCKETS = {
    "request_latency": [1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600, 1800],
    "time_to_first_token": [0.25, 0.5, 1, 1.5, 2, 3, 5, 7.5, 10, 20, 40, 80],
    "inter_token_latency": [0.01, 0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05, 0.06, 0.075, 0.1, 0.25, 1],
}
# The serving settings of the coursework pool's release (section 4.2). `VLLM` holds its context
# length and its target concurrency.
SECTION_4_2: dict[str, object] = {
    "kv_cache_bits": 8,
    "prefix_caching": True,
    "step_budget_tokens": 8192,
    "tool_call_parser": "qwen3_coder",
    "reasoning_parser": "qwen3",
    "histogram_buckets": BUCKETS,
}
DRAFT_HEAD: dict[str, object] = {"draft_head_tokens": 3}
# What a pool's serving settings may add. Left at their defaults, they add none of these.
BY_SETTING = (
    "--kv-cache-dtype",
    "--enable-prefix-caching",
    "--no-enable-prefix-caching",
    "--max-num-batched-tokens",
    "--enable-auto-tool-choice",
    "--tool-call-parser",
    "--reasoning-parser",
    "--custom-histogram-buckets",
    "--speculative-config",
)


def pool(changes: Mapping[str, object] | None = None, *, name: str = "coursework") -> Pool:
    (parsed,) = parse_pools({name: {**VLLM, **(changes or {})}})
    return parsed


def options(arguments: list[str]) -> list[str]:
    """The name of each option among the arguments: what precedes `=`, or `.` for a part of an option."""
    return [argument.split("=", 1)[0].split(".", 1)[0] for argument in arguments if argument.startswith("-")]


def values(arguments: list[str], option: str) -> list[str]:
    """What follows an option, up to the next one."""
    (at,) = [index for index, argument in enumerate(arguments) if argument == option]
    following = arguments[at + 1 :]
    return following[: next((i for i, argument in enumerate(following) if argument.startswith("-")), len(following))]


@pytest.mark.parametrize(
    "changes",
    [{}, EXCEPTION, {**SECTION_4_2, **DRAFT_HEAD}],
    ids=["as-every-pool", "with-the-exception", "with-every-serving-setting"],
)
def test_every_vllm_pool_has_the_contracts_settings(changes: dict[str, object]) -> None:
    arguments = vllm_command.arguments(pool(changes), bucket=BUCKET)
    assert all(isinstance(argument, str) and argument for argument in arguments)

    # Media only from inline data: the allow-list names one domain, which never resolves.
    assert values(arguments, "--allowed-media-domains") == ["media.invalid"]
    # No profiling route, no chat template from a request, no local file, no request or response
    # in the log, and no secret on the command line.
    assert not set(options(arguments)) & set(ABSENT)
    assert "--api-key" not in options(arguments)
    # No option is given twice, where the last one would win.
    assert len(options(arguments)) == len(set(options(arguments)))


def test_the_model_is_read_from_the_pools_prefix_at_its_revision_by_the_streamer() -> None:
    arguments = vllm_command.arguments(pool(), bucket=BUCKET)
    # First and by position, as `vllm serve` reads its model.
    assert arguments[0] == f"s3://{BUCKET}/coursework/{REVISION}/"
    assert vllm_command.model_location(pool(), BUCKET) == arguments[0]
    assert values(arguments, "--load-format") == ["runai_streamer"]
    assert values(arguments, "--port") == ["8000"]

    other_revision = hashlib.sha1(b"another commit", usedforsecurity=False).hexdigest()
    other = vllm_command.arguments(pool({"revision": other_revision}, name="other-model"), bucket="another-bucket")
    assert other[0] == f"s3://another-bucket/other-model/{other_revision}/"
    # The served name is the pool's; nothing else follows the pool, its revision or the bucket.
    assert [a for a in other[1:] if a != "other-model"] == [a for a in arguments[1:] if a != "coursework"]
    # The model repository's name is Hugging Face's, which the engine never calls.
    assert not any("example/model" in argument for argument in arguments)


def test_model_code_is_trusted_only_for_a_pool_that_records_its_exception() -> None:
    plain = vllm_command.arguments(pool(), bucket=BUCKET)
    assert TRUST not in options(plain)
    assert TRUST not in options(vllm_command.arguments(pool({"trust_remote_code": False}), bucket=BUCKET))

    trusted = vllm_command.arguments(pool(EXCEPTION), bucket=BUCKET)
    assert trusted.count(TRUST) == 1
    # The exception adds that option and changes nothing else; its reason stays with the pool.
    assert [argument for argument in trusted if argument != TRUST] == plain


def test_a_pool_that_is_not_served_by_vllm_has_no_arguments() -> None:
    (mock,) = parse_pools({"mock": MOCK})
    with pytest.raises(vllm_command.VllmCommandError) as refusal:
        vllm_command.arguments(mock, bucket=BUCKET)
    assert repr("mock") in str(refusal.value)


def test_the_serving_settings_of_the_coursework_pool_reach_the_command() -> None:
    arguments = vllm_command.arguments(pool(SECTION_4_2), bucket=BUCKET)
    assert values(arguments, "--max-model-len") == ["262144"]
    assert values(arguments, "--kv-cache-dtype") == ["fp8"]
    assert values(arguments, "--max-num-seqs") == ["8"]
    assert values(arguments, "--max-num-batched-tokens") == ["8192"]
    # vLLM parses tool calls only with automatic tool choice on, and refuses it without a parser.
    assert values(arguments, "--enable-auto-tool-choice") == []
    assert values(arguments, "--tool-call-parser") == ["qwen3_coder"]
    assert values(arguments, "--reasoning-parser") == ["qwen3"]
    # The engine's cache metrics, a switch.
    assert values(arguments, "--kv-cache-metrics") == []

    # Prefix caching is on in vLLM unless the command turns it off, which only a pool that asks gets.
    assert not {"--enable-prefix-caching", "--no-enable-prefix-caching"} & set(options(arguments))
    off = vllm_command.arguments(pool({**SECTION_4_2, "prefix_caching": False}), bucket=BUCKET)
    assert off.count("--no-enable-prefix-caching") == 1
    assert "--enable-prefix-caching" not in off

    # One JSON object, with each family's boundaries as the pool gave them.
    (written,) = values(arguments, "--custom-histogram-buckets")
    boundaries = json.loads(written)
    assert boundaries == BUCKETS
    # At 20 to 45 tokens a second a token takes 22 to 50 ms, which vLLM's own boundaries split in two.
    assert len([boundary for boundary in boundaries["inter_token_latency"] if 0.022 < boundary < 0.05]) > 2


def test_the_engine_serves_the_model_under_the_pools_name() -> None:
    for name in ("coursework", "upgrade"):
        arguments = vllm_command.arguments(pool(SECTION_4_2, name=name), bucket=BUCKET)
        # The name students choose the pool by, which the gateway forwards: never the model's location.
        assert values(arguments, "--served-model-name") == [name]


def test_a_serving_setting_left_at_its_default_adds_no_flag() -> None:
    plain = vllm_command.arguments(pool(), bucket=BUCKET)
    assert not set(options(plain)) & set(BY_SETTING)
    # Every pool has a context length and a target concurrency, and the cache metrics.
    assert values(plain, "--max-model-len") == ["262144"]
    assert values(plain, "--max-num-seqs") == ["8"]
    assert "--kv-cache-metrics" in plain
    # The defaults, written out, are the defaults.
    written_out = {"kv_cache_bits": 16, "prefix_caching": True, "histogram_buckets": {}}
    assert vllm_command.arguments(pool(written_out), bucket=BUCKET) == plain

    # A family the pool leaves out keeps vLLM's boundaries: it is not in the object.
    one = vllm_command.arguments(pool({"histogram_buckets": {"inter_token_latency": [0.02, 0.03]}}), bucket=BUCKET)
    assert json.loads(values(one, "--custom-histogram-buckets")[0]) == {"inter_token_latency": [0.02, 0.03]}


def test_the_draft_head_is_off_by_default() -> None:
    for changes in ({}, SECTION_4_2):
        off = options(vllm_command.arguments(pool(changes), bucket=BUCKET))
        # Neither the option of vLLM's recipe nor one of vLLM's shorter ways to ask for a draft.
        assert not [option for option in off if option == "-sc" or option.startswith("--spec")]

    for tokens in (3, 5):
        on = vllm_command.arguments(pool({**SECTION_4_2, "draft_head_tokens": tokens}), bucket=BUCKET)
        # As vLLM's recipe for the model writes it, with the pool's number of tokens.
        (written,) = values(on, "--speculative-config")
        assert json.loads(written) == {"method": "mtp", "num_speculative_tokens": tokens}
        assert [option for option in options(on) if option == "-sc" or option.startswith("--spec")] == [
            "--speculative-config"
        ]


@pytest.mark.parametrize(
    "changes",
    [{}, SECTION_4_2, {**SECTION_4_2, **DRAFT_HEAD}, {"prefix_caching": False}, EXCEPTION],
    ids=["plain", "section-4-2", "draft-head-on", "prefix-caching-off", "with-the-exception"],
)
def test_no_effort_mapping_in_the_engine(changes: dict[str, object]) -> None:
    # The gateway's request profile maps reasoning effort (section 6.2): the engine never does.
    arguments = vllm_command.arguments(pool(changes), bucket=BUCKET)
    assert not [argument for argument in arguments if "reasoning-effort" in argument.replace("_", "-")]
