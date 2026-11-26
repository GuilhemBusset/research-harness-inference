"""The contract of the launch fingerprint (`rhi_common.launch_settings`): which arguments of an
engine count, that nothing of an argument is hashed but a value on its option's list, and that
arguments the contract does not know give no fingerprint.

The arguments are those of a vllm pool's release, written here as `rhi.pools.vllm_command` builds
them; `tests/unit/pools/test_vllm_command.py` holds the two modules together. The bucket's name
is made of the placeholder account, and the alias names nobody's model.
"""

from __future__ import annotations

import json

import pytest

from rhi_common.launch_settings import UNAVAILABLE, fingerprint, normalize

BUCKET = "rhi-models-123456789012-eu-west-3"
MODEL = f"s3://{BUCKET}/coursework/{'c' * 40}/"
# A word that could be a deployment's own name for a model: short, lower-case, on no list.
ALIAS = "ourdraft"
BUCKETS = json.dumps({"request_latency": [1, 2.5, 5]}, separators=(",", ":"))
SPECULATION = json.dumps({"method": "mtp", "num_speculative_tokens": 3}, separators=(",", ":"))
# The arguments of a release with every serving setting on.
ARGUMENTS = [
    MODEL,
    *("--load-format", "runai_streamer"),
    *("--port", "8000"),
    *("--allowed-media-domains", "media.invalid"),
    "--served-model-name=coursework",
    *("--max-model-len", "262144"),
    *("--max-num-seqs", "8"),
    "--kv-cache-metrics",
    *("--kv-cache-dtype", "fp8"),
    "--no-enable-prefix-caching",
    *("--max-num-batched-tokens", "8192"),
    *("--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder"),
    *("--reasoning-parser", "qwen3"),
    *("--custom-histogram-buckets", BUCKETS),
    *("--speculative-config", SPECULATION),
    "--trust-remote-code",
]
SETTINGS = {
    "load-format": "runai_streamer",
    "max-model-len": 262144,
    "max-num-seqs": 8,
    "kv-cache-dtype": "fp8",
    "enable-prefix-caching": False,
    "max-num-batched-tokens": 8192,
    "enable-auto-tool-choice": True,
    "tool-call-parser": "qwen3_coder",
    "reasoning-parser": "qwen3",
    "speculative-config": {"method": "mtp", "num_speculative_tokens": 3},
    "trust-remote-code": True,
}


def changed(option: str, *value: str) -> list[str]:
    """`ARGUMENTS` with `value` in place of the value of `option`, or with `option` and `value`
    added when they have no such option."""
    if option not in ARGUMENTS:
        return [*ARGUMENTS, option, *value]
    at = ARGUMENTS.index(option)
    return [*ARGUMENTS[: at + 1], *value, *ARGUMENTS[at + 1 + len(value) :]]


def without(*arguments: str) -> list[str]:
    return [argument for argument in ARGUMENTS if argument not in arguments]


def test_the_settings_are_the_listed_ones_and_nothing_of_the_others() -> None:
    assert normalize(ARGUMENTS) == SETTINGS
    found = fingerprint(ARGUMENTS)
    assert len(found) == 64
    assert set(found) <= set("0123456789abcdef")
    # A release with no flag, the mock pool's, is known too: it has no setting.
    assert normalize([]) == {}
    assert fingerprint([]) not in (UNAVAILABLE, found)


def test_how_an_argument_is_written_does_not_change_the_fingerprint() -> None:
    joined = [*without("--max-num-batched-tokens", "8192", "--kv-cache-dtype", "fp8")]
    joined += ["--kv-cache-dtype=fp8", "--max-num-batched-tokens=08192"]
    assert joined != ARGUMENTS
    assert fingerprint(joined) == fingerprint(ARGUMENTS) != UNAVAILABLE


@pytest.mark.parametrize(
    "other",
    [
        changed("--max-num-batched-tokens", "4096"),
        without("--max-num-batched-tokens", "8192"),
        changed("--max-model-len", "65536"),
        changed("--max-num-seqs", "16"),
        changed("--kv-cache-dtype", "auto"),
        without("--no-enable-prefix-caching"),
        [*without("--no-enable-prefix-caching"), "--enable-prefix-caching"],
        changed("--load-format", "auto"),
        changed("--tool-call-parser", "hermes"),
        changed("--reasoning-parser", "deepseek_r1"),
        changed("--speculative-config", SPECULATION.replace("3", "5")),
        without("--speculative-config", SPECULATION),
        without("--trust-remote-code"),
        changed("--tensor-parallel-size", "2"),
        changed("--quantization", "fp8"),
    ],
)
def test_a_setting_that_differs_gives_another_fingerprint(other: list[str]) -> None:
    assert UNAVAILABLE not in (fingerprint(other), fingerprint(ARGUMENTS))
    assert fingerprint(other) != fingerprint(ARGUMENTS)


@pytest.mark.parametrize(
    "other",
    [
        # Another deployment's bucket, a pool on download, and another pool's name.
        [MODEL.replace("123456789012", "111122223333"), *ARGUMENTS[1:]],
        [f"--rhi-fetch-model={MODEL},/var/lib/rhi-model", "/var/lib/rhi-model", *ARGUMENTS[1:]],
        [*without("--served-model-name=coursework"), "--served-model-name=-another"],
        # What the engine reports, and where it listens.
        without("--kv-cache-metrics"),
        without("--custom-histogram-buckets", BUCKETS),
        changed("--port", "8001"),
    ],
)
def test_what_is_left_out_changes_nothing(other: list[str]) -> None:
    assert other != ARGUMENTS
    assert fingerprint(other) == fingerprint(ARGUMENTS) != UNAVAILABLE


@pytest.mark.parametrize(
    "unknown",
    [
        # A value that is on no list, in each kind of option: none is hashed.
        changed("--tool-call-parser", ALIAS),
        changed("--reasoning-parser", ALIAS),
        changed("--kv-cache-dtype", ALIAS),
        changed("--load-format", ALIAS),
        changed("--quantization", ALIAS),
        changed("--max-model-len", "256k"),
        changed("--max-num-batched-tokens", MODEL),
        changed("--max-num-seqs", "1" * 10),
        # The speculative configuration: a field or a method that is not the list's, a number that is none.
        changed("--speculative-config", json.dumps({"method": "mtp", "num_speculative_tokens": 3, "model": ALIAS})),
        changed("--speculative-config", json.dumps({"method": ALIAS, "num_speculative_tokens": 3})),
        changed("--speculative-config", json.dumps({"method": "mtp", "num_speculative_tokens": True})),
        changed("--speculative-config", json.dumps({"method": "mtp", "num_speculative_tokens": "3"})),
        changed("--speculative-config", "mtp"),
        # Arguments the contract does not read as a release's.
        [*ARGUMENTS, "--chat-template", "/srv/template.jinja"],
        [*ARGUMENTS, "-tp", "2"],
        [*ARGUMENTS, "--max-num-seqs", "8"],
        [*ARGUMENTS, "--enable-prefix-caching"],
        [*ARGUMENTS, MODEL],
        [*ARGUMENTS, "--trust-remote-code=true"],
        [*ARGUMENTS, "--tensor-parallel-size"],
        [*without("8192"), "--quantization", "fp8"],
        [*ARGUMENTS, ""],
    ],
)
def test_arguments_the_contract_does_not_know_give_no_fingerprint(unknown: list[str]) -> None:
    assert normalize(unknown) is None
    assert fingerprint(unknown) == UNAVAILABLE
