+++
id = "E5-08"
title = "The manifest of the upgrade model's 8-bit build, from the repository vLLM's recipe names"
labels = ["size:S", "kind:app"]
epic = "E5"
blocked_by = ["E3-08"]
+++

## Context

The upgrade pool serves Qwen3.8-Flash-Next in its publisher's 8-bit build of 186 GB (section 4.2).
The design names the model and the build, not the build's repository: evidence E28 cites the base
repository, Qwen/Qwen3.8-Flash-Next, for its licence, and vLLM's recipe for the model, run on four of
the G7e GPU, for its hardware. This issue takes the 8-bit build's repository id from that recipe, the
file for Qwen3.8-Flash-Next under `models/` of https://github.com/vllm-project/recipes, invents
none, and writes that build's manifest. The owner's item below then confirms that the repository the
recipe names is the 186 GB build decision 3 is about; the manifest is written either way, since the
upgrade pool is not built until the owner adds it to the configuration in SSM (issue E5-03).

The copy job copies a pool's weights only from a committed manifest of the pool's model, at the
pool's revision (issue E3-09). The manifest is written by the program of issue E3-08, which takes
each file's SHA-256 from Hugging Face's LFS metadata when the file is in LFS and computes it from
the downloaded file otherwise, and is named after the repository as that issue's format says. The
model needs no code from its repository (section 4.4; evidence E28), and the allow-list refuses any.

The design pins a commit and does not give one; this issue pins the commit the repository's main
branch points at when the manifest is built. The upgrade pool's entry names the same repository and
revision (issue E5-03). The model's licence is the owner's decision 3; this issue reads nothing into
it.

## Deliverables

- [ ] `models/manifests/` gains the manifest of the publisher's 8-bit build of Qwen3.8-Flash-Next whose repository vLLM's recipe for that model names, at a pinned revision that is a full commit hash, written by the program of issue E3-08.
- [ ] The manifest names the repository exactly as the recipe gives it, its file name follows from that repository, and the issue's pull request names the recipe file and commit it was read from.

## Acceptance criteria

- [ ] `tests/unit/test_weights.py::test_committed_manifests_are_valid` accepts the new manifest.
- [ ] The owner confirms that the repository the manifest names is the 186 GB 8-bit build that decision 3 of the design is about (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.2, 4.4
- `docs/design/evidence.md`, E28
