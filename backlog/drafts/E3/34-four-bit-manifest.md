+++
id = "E3-34"
title = "The manifest of the coursework model's 4-bit build, for spikes S3 and S5"
labels = ["size:S", "kind:app"]
epic = "E3"
blocked_by = ["E3-08"]
+++

## Context

Which build of the coursework model to serve is open (section 4.2). The publisher's 8-bit build is
the default, but it is not one of the two builds vLLM's recipe ran on the G7e GPU: those were the
16-bit weights and a 4-bit build published by NVIDIA, 21.9 GB (evidence E28). By the arithmetic of
section 4.7 the 4-bit build is about 40% faster and leaves 8 GiB more for the cache; what 4 bits cost
in the accuracy of tool calls is unmeasured. Spikes S3 and S5 run both builds, and the 4-bit build
becomes the default if it does the tasks as well (sections 4.2, 4.7 and 12).

The design does not name the 4-bit build's repository. It is the one vLLM's recipe for Qwen3.8-27B
lists for the RTX PRO 6000, in the file `models/Qwen/Qwen3.8-27B.yaml` of
https://github.com/vllm-project/recipes (evidence E28); this issue takes the name from there and
invents none. The recipe ran it with the same settings as the coursework pool's (section 4.2).

The copy job copies a pool's weights only from a committed manifest of the pool's model, at the
pool's revision (issue E3-09). The manifest is written by the program of issue E3-08, which takes
each file's SHA-256 from Hugging Face's LFS metadata when the file is in LFS and computes it from
the downloaded file otherwise, and is named after the repository as that issue's format says. Running a spike on the 4-bit build is then the owner's change of the pool's
model and revision in the configuration, which the copy job and a release follow (section 4.3).

## Deliverables

- [ ] `models/manifests/` gains the manifest of NVIDIA's 4-bit build of Qwen3.8-27B that vLLM's recipe lists for the RTX PRO 6000, at a pinned revision that is a full commit hash, written by the program of issue E3-08, chat template included.
- [ ] The manifest names the repository exactly as the recipe gives it, and its file name follows from that repository.

## Acceptance criteria

- [ ] `tests/unit/test_weights.py::test_committed_manifests_are_valid` accepts the new manifest.
- [ ] With the coursework pool's model and revision set to this build, the copy job of issue E3-09 leaves exactly this manifest's files under the pool's prefix (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.2, 4.3, 4.7, 12
- `docs/design/evidence.md`, E28
