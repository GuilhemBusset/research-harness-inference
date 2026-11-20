# Pipeline scripts

This folder holds the scripts that the pipeline's steps run (system design, section 8.1), one
module per script.

It is not a package: there is no `__init__.py`. The folder is on the import path of pytest, Ruff
and ty (`pyproject.toml`, `ruff.toml`, `ty.toml`), so a test imports a script by its module name:
`scripts/pipeline/leak_check.py` is `import leak_check`.

The scripts of the synth step (`src/rhi/pipeline/synth.py`), which gives each one the folders of
pytest's import path in `PYTHONPATH`:

- `fetch_config.py` reads the configuration from SSM and writes it to the file `RHI_CONFIG_FILE`
  names, outside the source tree. It prints no value.
- `leak_check.py` searches the source tree for the value of every private key of that file, in any
  letter case, and fails with the file and line of each hit, never with the value. It refuses to
  run when that file lies inside the tree it searches.

The script of the verification step (`src/rhi/pipeline/verify.py`), which runs after the stage's
deployment and needs no import path:

- `verify.py` reads the canary's key from the secret `rhi/canary-key` and runs the smoke tests of
  `tests/smoke` against the deployed endpoint, in a process of their own that takes the key from
  its environment. It deselects, by their IDs, the tests that must reach the internal load
  balancer, and exits non-zero when a test fails. While the endpoint has no HTTPS listener, or the
  secret is missing or holds no key, it runs no test, exits 0 and writes its reason, `listener-off`
  or `canary-missing`, to the file `RHI_VERIFY_REASON_FILE` names, which the step's post-build
  phase copies to the artifact bucket. When a test fails, it adds the test's ID
  (`tests/smoke/ids.py`) as a line of the file `RHI_VERIFY_FAILED_TESTS_FILE` names, which that
  phase copies too. When the run of the tests fails, it reads the heartbeat rows of the instances of
  the pools it tested from the table `RHI_VERIFY_HEARTBEATS_TABLE` names, and leaves the last
  lifecycle stage each instance reached, one per line and nothing else of a row, in the file
  `RHI_VERIFY_LAST_STAGES_FILE` names, which that phase copies as well. It never prints the key,
  and what the tests print reaches the build's log with `[canary key]` in place of the key and of
  any piece of it.

The rule for model weights (system design, section 4.4), which the job that copies each pool's
model into the model bucket follows:

- `weights.py` decides which files of a model repository are copied: safetensors files, JSON files
  and the tokenizer's files `merges.txt`, `tokenizer.model` and `chat_template.jinja`. Pickle
  formats, Python files and anything else are refused. It reads a repository's manifest,
  `models/manifests/<name>.json`, and verifies a downloaded file against the SHA-256 the manifest
  gives for its path. The manifest's format is in the module's docstring. Run with a repository and
  a revision, it writes that repository's manifest from Hugging Face's API, read anonymously with
  httpx (the `gateway` group's pin, which the `dev` group includes):

      uv run --locked python scripts/pipeline/weights.py Qwen/Qwen3.8-27B-FP8 <full commit hash>

  A file stored in LFS takes its size and SHA-256 from Hugging Face's listing; any other admitted
  file is downloaded at that revision and hashed. Moving a model to another revision is running
  the program again and committing the manifest it writes.

The script of the step that copies the weights (`src/rhi/pipeline/weights.py`), which runs before
the stage's deployment and is given the same import path as the scripts of the synth step:

- `copy_weights.py` is the model bucket's only writer. It reads the configuration from SSM and, for
  each GPU pool, copies the files of the manifest of the pool's model from Hugging Face, at the
  pool's revision, to `<pool>/<revision>/` in the bucket `RHI_MODEL_BUCKET` names. A file is
  downloaded, held to the manifest's size and SHA-256, and only then written. The job never reads
  an object of the bucket: it lists the prefix, skips a file already there at the manifest's size,
  and removes from that prefix what the manifest does not list. It leaves every other prefix
  alone, another revision's among them. A pool whose model has no committed manifest, or whose
  revision is not its manifest's, fails the job before anything is copied for any pool; a
  configuration with no GPU pool copies nothing and passes. So moving a pool to another revision
  or another build is a change of its configuration and a committed manifest.
