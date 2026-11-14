+++
id = "E3-08"
title = "Weights: an allow-list of file types and a SHA-256 manifest"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E1-02", "E1-16"]
+++

## Context

A pipeline job copies each pool's model, at its pinned revision, from Hugging Face into the model
bucket (section 4.4). It copies only an allow-list of file types (safetensors, JSON, tokenizer
files) and refuses pickle formats and Python files. It verifies every file against a SHA-256
manifest committed to the repository. This issue is the rule, the manifest's format, and the
program that writes a manifest; the job is issue E3-09.

Tokenizer files include the model's chat template. The coursework model keeps its template in a file
of its own, chat_template.jinja (evidence E28), and the template is what the harnesses' requests are
rendered with, so a copy without it would serve a different model. The tokenizer's own files are
JSON files, its merges file and, for some models, its model file.

**The manifest.** One JSON file per model repository, `models/manifests/<name>.json`, where the name
is the repository's id in lower case with its slash replaced by two hyphens: Qwen/Qwen3.8-27B-FP8
gives `models/manifests/qwen--qwen3.8-27b-fp8.json`. It holds exactly three keys: repository, the id
as Hugging Face writes it; revision, a full commit hash of 40 lower-case hexadecimal characters; and
files, a list of objects with exactly the keys path, size (in bytes) and sha256 (64 lower-case
hexadecimal characters), every path admitted by the allow-list and none twice. A pool names a
repository and a revision, and the copy job finds the manifest by the repository; the manifest's
revision must be the pool's (issue E3-09). Moving a model to another revision is a change of its
manifest.

**Where the digests come from.** Hugging Face publishes a SHA-256 only for files stored in LFS (the
weights); for the others (the JSON files, the merges file, the chat template) it gives a git blob
id, which is not a SHA-256 of the content. So the program takes a file's SHA-256 from its LFS
metadata when the file is in LFS, and otherwise computes it from the file downloaded at that
revision. It reads Hugging Face anonymously, over HTTPS, with the HTTP client issue E1-16 pins.

## Deliverables

- [ ] `scripts/pipeline/weights.py` decides, for a list of a model repository's files, which are copied: safetensors files, JSON files, and the tokenizer files merges.txt, tokenizer.model and chat_template.jinja are; pickle formats, Python files and anything else are refused by name.
- [ ] It verifies a downloaded file against the manifest's SHA-256 for that path, and refuses a file the manifest does not list, a digest that differs, and a repository that has no manifest.
- [ ] It reads and validates a manifest in the format the Context gives, and refuses one whose file name does not follow from its repository, whose revision is not a full commit hash, or whose keys differ from the format's.
- [ ] Run as a program with a repository and a revision, it writes that repository's manifest under `models/manifests/` from Hugging Face's API: the files the allow-list admits, each with its size and its SHA-256, from the LFS metadata when the file is in LFS and otherwise computed from the file downloaded at that revision.
- [ ] `tests/unit/test_weights.py` covers the allow-list, the verification, the format and the program, the program against recorded answers of Hugging Face's API with no network, and its test test_committed_manifests_are_valid holds every file under `models/manifests/` to the format.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_weights.py` passes: a repository listing with a pickle file and a Python file copies neither, chat_template.jinja is copied, and a file whose content differs from the manifest by one byte is refused.
- [ ] `tests/unit/test_weights.py::test_a_branch_name_is_not_a_revision` refuses a manifest whose revision is not a 40-character commit hash.
- [ ] `tests/unit/test_weights.py::test_committed_manifests_are_valid` passes on the tree as it stands, and fails a manifest, built in the test, whose file name does not follow from its repository.
- [ ] `tests/unit/test_weights.py::test_digests_come_from_lfs_or_the_file` gives the program a recorded listing with one file in LFS and one not, and finds the first file's SHA-256 taken from its LFS metadata and the second's computed from its recorded content.

## References

- `docs/design/system-design.md`, sections 4.4, 8.1
- `docs/design/evidence.md`, E28
