+++
id = "E3-63"
title = "The manifest of the faster sibling's 4-bit build, so that spike S5's lever faster-sibling passes CopyWeights"
labels = ["size:S", "kind:app"]
epic = 5
+++

## Context

Spike S5's last lever, `faster-sibling` (issue #204; `docs/measurements/s5.md`, the table of
levers; `spikes/s5_capacity.py`), runs the pool on the faster sibling of section 4.7,
Qwen3.6-35B-A3B. The build is NVIDIA's 4-bit build, `nvidia/Qwen3.6-35B-A3B-NVFP4`, at the commit
`1355db6a052410cfd62085d94b58866fd0f2c3c5`: the same publisher and the same NVFP4 quantization as
the coursework model's 4-bit build, whose manifest is `models/manifests/nvidia--qwen3.8-27b-nvfp4.json`
(issue #161).

The copy job (`scripts/pipeline/copy_weights.py`, the pipeline's action CopyWeights) copies a
pool's weights only from a committed manifest of the pool's model at the pool's revision, and a
pool whose model has none fails the job with nothing copied. No manifest exists for the sibling,
so an execution whose configuration puts a pool on it fails at CopyWeights. Running the lever is
then the owner's change of the pool's model and revision in the configuration (section 4.3).

The manifest is written by the program of `scripts/pipeline/weights.py`, which reads Hugging Face
anonymously and names the file after the repository:

    uv run --locked python scripts/pipeline/weights.py nvidia/Qwen3.6-35B-A3B-NVFP4 1355db6a052410cfd62085d94b58866fd0f2c3c5

Run on 2026-11-27 into a scratch folder, it wrote `nvidia--qwen3.6-35b-a3b-nvfp4.json` with 14
files and 23,457,714,000 bytes, and reported three files not copied: `.gitattributes`,
`.quant_summary.txt` and `README.md`. The three weight shards hold 23.4 GB. Unlike the 27B build,
this repository has no `merges.txt` (its tokenizer is `tokenizer.json`) and no
`processor_config.json`: the allow-list copies what is there, and the program is not changed.

What the repository says at that commit, read on 2026-11-27:

- `config.json`: architecture `Qwen3_5MoeForConditionalGeneration`, 262,144 positions, one
  multi-token prediction layer, and no `auto_map`. No file of the repository ends in `.py`. The
  publisher's own repository, `Qwen/Qwen3.6-35B-A3B`, has the same architecture, no `auto_map` and
  no `.py` file either.
- vLLM `v0.33.0` registers `Qwen3_5MoeForConditionalGeneration` and its prediction layer,
  `Qwen3_5MoeMTP`, in `vllm/model_executor/models/registry.py`. So the model needs no code from
  its repository, and a pool serving it keeps `trust_remote_code` false. Section 4.2's table says
  the opposite ("Needs model code"); this issue corrects the design.
- `chat_template.jinja`, identical to the `chat_template` of `tokenizer_config.json`, reads no
  reasoning effort: the word "effort" is not in it. Its one switch is `enable_thinking`, and it
  opens a thinking block on every turn unless that is false. The effort the request profile maps
  (Claude Code's `high` as `xhigh`) reaches a template that does not read it, which renders
  without an error and without effect. The coursework model's template, by contrast, raises on
  any effort but `xhigh`, `medium` and `low` (E28).

The design records each fact it relies on in `docs/design/evidence.md`; E28 already holds the row
on the coursework model's template, and the sibling's facts belong beside it.

## Deliverables

- [ ] `models/manifests/nvidia--qwen3.6-35b-a3b-nvfp4.json` is the manifest of nvidia/Qwen3.6-35B-A3B-NVFP4 at revision 1355db6a052410cfd62085d94b58866fd0f2c3c5, as the program named in Context writes it, with every file of that revision the allow-list admits and no other.
- [ ] `docs/design/system-design.md` says, in section 4.2's row for Qwen3.6-35B-A3B and in section 4.4, that this model needs no code from its repository because vLLM builds its architecture in, and, in section 4.7's lever for the faster sibling, that the lever serves NVIDIA's 4-bit build and that this build's chat template has no reasoning-effort switch, so it ignores the mapped effort without an error.
- [ ] `docs/design/evidence.md` gains a row under E28, cited by those passages, that records at that revision the licence, the size of the weights, the architecture vLLM v0.33.0 registers, the absence of model code and the template's one thinking switch, each with its source.

## Acceptance criteria

- [ ] `tests/unit/test_weights.py::test_committed_manifests_are_valid` passes with the sibling's manifest committed.
- [ ] `grep -q '"revision": "1355db6a052410cfd62085d94b58866fd0f2c3c5"' models/manifests/nvidia--qwen3.6-35b-a3b-nvfp4.json` exits 0.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] A pipeline execution with the coursework pool's model set to nvidia/Qwen3.6-35B-A3B-NVFP4 at revision 1355db6a052410cfd62085d94b58866fd0f2c3c5 passes CopyWeights and leaves exactly the manifest's 14 files under the pool's prefix at that revision (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.2, 4.4 and 4.7
- `docs/design/evidence.md`, E28
- `docs/measurements/s5.md`, the levers
- `scripts/pipeline/weights.py`, the manifest's format and its program
- `scripts/pipeline/copy_weights.py`, the job that reads the manifests
- `models/manifests/nvidia--qwen3.8-27b-nvfp4.json`, the coursework model's 4-bit build
