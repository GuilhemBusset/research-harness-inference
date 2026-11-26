"""The public fingerprint of the settings an engine was started with (agent session evaluation,
"Reproducing and comparing runs").

Two engines can run the same image and the same model revision with different arguments: a
config-only experiment changes one argument and nothing else. So the lifecycle agent says, beside
the release it serves, a fingerprint of the arguments its engine runs with
(`rhi_lifecycle.serving`). The arguments themselves are never published: the deployment puts
private values in some of them, such as the model bucket, whose name holds the account. This
module is the contract between the two: which arguments count, how each is read, and what is
left out.

**What is fingerprinted** is on a list, option by option, and so is every value an option may
take. Nothing of an argument reaches the fingerprint but through its list:

| Option | Values |
| --- | --- |
| `--max-model-len`, `--max-num-seqs`, `--max-num-batched-tokens`, `--tensor-parallel-size` | a whole number of at most nine digits |
| `--kv-cache-dtype` | `KV_CACHE_DTYPES` |
| `--load-format` | `LOAD_FORMATS` |
| `--quantization` | `QUANTIZATIONS` |
| `--tool-call-parser` | `TOOL_CALL_PARSERS` |
| `--reasoning-parser` | `REASONING_PARSERS` |
| `--speculative-config` | a JSON object of exactly `method`, one of `SPECULATIVE_METHODS`, and `num_speculative_tokens`, a whole number as above |
| `--enable-prefix-caching`, `--enable-auto-tool-choice`, `--trust-remote-code` | none: the option, or the option with `--no-` before its name |

The names are vLLM's own, read in its source at the version the engine image is built from
(`rhi.pools.vllm_command` says where). A list holds names anyone can read there, so a value on it
tells nothing of a deployment. A value that is not on its option's list is not hashed, whatever it
looks like: a short word can be a private alias as well as a public name.

**What is left out**, known and never read: the model, which is the one argument that is no
option and holds the model bucket's name or a local folder; `--rhi-fetch-model`, which holds the
same location; `--served-model-name`, the pool's name; `--port` and `--allowed-media-domains`,
the same for every pool; `--kv-cache-metrics`, `--enable-prompt-tokens-details` and
`--custom-histogram-buckets`, which change what the engine reports and not what it serves. The
image and the model revision are not arguments: `rhi_lifecycle_release_info` carries them.

**How an argument is written does not count.** `--option value` and `--option=value` are one
setting, the order of the options is not kept, and a number is read as a number. What vLLM does
by itself when an option is left out is not filled in: an option that is left out and the same
option with vLLM's default are two fingerprints, because only vLLM knows its defaults.

**Anything else is `UNAVAILABLE`.** An option that is on neither list, an option given twice, a
second argument that is no option, a value that is not on its option's list: the arguments are
not ones this contract knows, and no fingerprint is made of a part of them. `UNAVAILABLE` is also
what the agent says when its host gives no arguments at all, as under a unit older than this
contract. It is a gap in the evidence and no failure: nothing waits for a fingerprint.

**The fingerprint** is the SHA-256, in hexadecimal, of the settings and of `CONTRACT`, written
as JSON with sorted keys. `CONTRACT` changes whenever the lists or the reading change, so that
two fingerprints that are equal were made the same way.

`rhi.pools.vllm_command` builds the arguments of a release, and a test holds the two together:
every argument it can build is one this contract knows.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# The version of this contract, which is part of what is hashed.
CONTRACT = 2

# What stands for a fingerprint where there is none to give. No fingerprint is this word.
UNAVAILABLE = "unavailable"

# vLLM's names at v0.33.0, each list as its source gives it.
# Of `CacheDType` in `vllm/config/cache.py`: the model's own data type, which is vLLM's default,
# and the cache of 8 bits, the one other a pool's configuration can ask for.
KV_CACHE_DTYPES = frozenset({"auto", "fp8"})
# Of `LoadConfig.load_format` in `vllm/config/load.py`: vLLM's default loader and the streamer.
LOAD_FORMATS = frozenset({"auto", "runai_streamer"})
# Of `SpeculativeMethod` in `vllm/config/speculative.py`: the draft head of the model's
# checkpoint, the one method a pool's configuration can turn on.
SPECULATIVE_METHODS = frozenset({"mtp"})
# `QuantizationMethods` in `vllm/model_executor/layers/quantization/__init__.py`.
QUANTIZATIONS = frozenset({
    "awq", "auto_awq", "fp8", "fbgemm_fp8", "fp_quant", "modelopt", "modelopt_fp4", "modelopt_mxfp8",
    "modelopt_mixed", "auto_gptq", "gptq", "gptq_marlin", "awq_marlin", "humming", "compressed-tensors",
    "experts_int8", "quark", "moe_wna16", "torchao", "inc", "mxfp4", "gpt_oss_mxfp4", "deepseek_v4_fp8",
    "online", "fp8_per_tensor", "fp8_per_block", "fp8_per_channel", "int8_per_channel_weight_only",
    "nvfp4_per_token", "mxfp8",
})  # fmt: skip
# The names `vllm/tool_parsers/__init__.py` registers.
TOOL_CALL_PARSERS = frozenset({
    "dots", "deepseek_v3", "deepseek_v31", "deepseek_v32", "deepseek_v4", "deepseek_v41", "cohere_command3",
    "cohere_command4", "ernie45", "glm45", "glm47", "ling3", "granite-20b-fc", "granite", "granite4", "hermes",
    "hf", "beam", "poolside_v1", "hunyuan_a13b", "hy_v3", "hy_v4", "internlm", "jamba", "lfm2", "kimi_k2",
    "kimi_k3", "k2_horizon", "llama3_json", "llama4_json", "llama4_pythonic", "longcat", "mimo", "minimax_m2",
    "minimax_m3", "minicpm5", "mistral", "olmo3", "plamo3", "muse", "muse_glimmer", "musespark1x", "openai",
    "phi4_mini_json", "pythonic", "qwen3_coder", "qwen3_json", "qwen3_xml", "seed_oss", "step3", "step3p5",
    "step5", "inkling", "xlam", "gigachat3", "functiongemma", "gemma4", "apertus",
})  # fmt: skip
# The names `vllm/reasoning/__init__.py` registers.
REASONING_PARSERS = frozenset({
    "deepseek_r1", "deepseek_v3", "deepseek_v4", "deepseek_v41", "poolside_v1", "cohere_command3",
    "cohere_command4", "ernie45", "gemma4", "glm45", "glm47", "ling3", "openai_gptoss", "granite",
    "granite_thinking_parser", "hf", "beam", "holo2", "hunyuan_a13b", "hy_v3", "hy_v4", "kimi_k2", "kimi_k3",
    "k2_horizon", "mimo", "minicpmv", "minimax_m2", "minimax_m2_append_think", "minimax_m3", "mistral",
    "nemotron_v3", "olmo3", "plamo3", "muse", "muse_glimmer", "musespark1x", "qwen3", "seed_oss", "step3",
    "step3p5", "step5", "inkling",
})  # fmt: skip

_NUMBER = re.compile(r"[0-9]{1,9}", re.ASCII)


class _Unknown(Exception):
    """The arguments are not ones the contract knows."""


def _number(text: str) -> int:
    if _NUMBER.fullmatch(text) is None:
        raise _Unknown
    return int(text)


def _one_of(names: frozenset[str]) -> Callable[[str], str]:
    def read(text: str) -> str:
        if text not in names:
            raise _Unknown
        return text

    return read


def _speculation(text: str) -> dict[str, object]:
    try:
        given = json.loads(text)
    except ValueError:
        raise _Unknown from None
    if not isinstance(given, dict) or set(given) != {"method", "num_speculative_tokens"}:
        raise _Unknown
    method, tokens = given["method"], given["num_speculative_tokens"]
    # A whole number as JSON writes one, and no boolean, which Python counts among its integers.
    if not isinstance(method, str) or method not in SPECULATIVE_METHODS or type(tokens) is not int:
        raise _Unknown
    return {"method": method, "num_speculative_tokens": _number(str(tokens))}


# The options that are fingerprinted with a value, each with the reading of its value.
_VALUED: dict[str, Callable[[str], object]] = {
    "--max-model-len": _number,
    "--max-num-seqs": _number,
    "--max-num-batched-tokens": _number,
    "--tensor-parallel-size": _number,
    "--kv-cache-dtype": _one_of(KV_CACHE_DTYPES),
    "--load-format": _one_of(LOAD_FORMATS),
    "--quantization": _one_of(QUANTIZATIONS),
    "--tool-call-parser": _one_of(TOOL_CALL_PARSERS),
    "--reasoning-parser": _one_of(REASONING_PARSERS),
    "--speculative-config": _speculation,
}
# Those that are fingerprinted as on or off: the option, or `--no-` and its name.
_SWITCHES = frozenset({"--enable-prefix-caching", "--enable-auto-tool-choice", "--trust-remote-code"})
# What is left out: the options whose value is never read, and the one that has none.
_LEFT_OUT_VALUED = frozenset({
    "--rhi-fetch-model", "--served-model-name", "--port", "--allowed-media-domains", "--custom-histogram-buckets",
})  # fmt: skip
_LEFT_OUT_SWITCHES = frozenset({"--kv-cache-metrics", "--enable-prompt-tokens-details"})


def _switch(option: str) -> tuple[str, bool]:
    """The switch `option` turns, and which way: `--no-<name>` turns `--<name>` off."""
    if option.startswith("--no-"):
        return "--" + option.removeprefix("--no-"), False
    return option, True


def normalize(arguments: Sequence[str]) -> dict[str, object] | None:
    """The settings of `arguments` that the contract fingerprints, each under its option's name
    without the hyphens before it, or None when the arguments are not ones the contract knows.
    `arguments` are an engine's flags, in order, as a release carries them."""
    settings: dict[str, object] = {}
    seen: set[str] = set()
    model = False
    rest = iter(arguments)
    try:
        for argument in rest:
            if not argument.startswith("-"):
                # The model, and nothing of it is read. No release has a second.
                if model:
                    raise _Unknown
                model = True
                continue
            option, equals, value = argument.partition("=")
            name, on = _switch(option)
            if name in _SWITCHES or name in _LEFT_OUT_SWITCHES:
                if equals:
                    raise _Unknown
                found: object = on
            elif option in _VALUED or option in _LEFT_OUT_VALUED:
                name = option
                if not equals:
                    # The value is the next argument, which is no option.
                    value = next(rest, "-")
                    if value.startswith("-"):
                        raise _Unknown
                found = _VALUED[option](value) if option in _VALUED else None
            else:
                raise _Unknown
            if name in seen:
                raise _Unknown
            seen.add(name)
            if name in _SWITCHES or name in _VALUED:
                settings[name.removeprefix("--")] = found
    except _Unknown:
        return None
    return settings


def fingerprint(arguments: Sequence[str]) -> str:
    """The fingerprint of the settings an engine started with `arguments` runs with: 64
    hexadecimal digits, or `UNAVAILABLE` when the arguments are not ones the contract knows."""
    settings = normalize(arguments)
    if settings is None:
        return UNAVAILABLE
    document = json.dumps({"contract": CONTRACT, "settings": settings}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()
