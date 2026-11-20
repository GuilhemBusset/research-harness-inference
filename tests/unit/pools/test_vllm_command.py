"""The arguments a vllm pool's engine is started with (`rhi.pools.vllm_command`; system design,
sections 4.1 and 4.4): what the engine contract asks of every vllm pool, and the one exception a
pool may record.

The names are vLLM's at version 0.33.0, written here as the contract's table gives them, not read
from the module. The revision is made when the test runs and names no real commit.
"""

from __future__ import annotations

import hashlib
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


@pytest.mark.parametrize("changes", [{}, EXCEPTION], ids=["as-every-pool", "with-the-exception"])
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
    assert other[1:] == arguments[1:]
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
