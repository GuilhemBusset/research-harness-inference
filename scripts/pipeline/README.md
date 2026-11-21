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

The scripts of the verification step (`src/rhi/pipeline/verify.py`), which runs after the stage's
deployment and needs no import path:

- `gpu_verification.py` runs first and decides whether the execution verifies on the coursework
  pool: yes when an instance refresh of a GPU pool's group, found by its tags `rhi-pool` and
  `rhi-engine`, began after the execution started, which it reads from the pipeline CodeBuild
  names in `CODEBUILD_INITIATOR`. It waits for such a refresh to end, fails when it did not end
  `Successful`, and leaves `coursework`, or nothing, in the file `RHI_VERIFY_GPU_POOLS_FILE` names.

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
  any piece of it. When the decision's file names a pool, it then runs the same smoke tests once
  more with `RHI_SMOKE_POOL` naming that pool, `gpu-release-served` among them, which the mock
  pool's run leaves out, and `release-heartbeat`, which compares the release the pool's instances
  write in their heartbeat rows with the deployed one.

The script of the weekly GPU canary (`src/rhi/pipeline/canary.py`), a job its schedule starts on
Sundays at 04:00 UTC, with the repository's package roots in `PYTHONPATH`:

- `canary_result.py` reads the canary's key as `verify.py` does, and the stage's workspace from
  the settings of the coursework pool's release parameter, and runs the smoke tests of
  `tests/smoke` once against the deployed endpoint, on the coursework pool, through `verify.py`'s
  runner: the tests the verification runs on that pool after a refresh, less `release-heartbeat`,
  which reads the heartbeats table, a table the canary's role may not read. While they
  run, it reads the pool's state from `GET /status` to time the wake, from the pool's last `cold`
  to its first `ready`. It then sends the measurement `gpu-canary` (the verdict, `wake_seconds`,
  `smoke_tests_passed`, `smoke_tests_failed`) on the measurement bus, on the commit the job ran
  from, with the run's UTC date, YYYY-MM-DD, as its run label. It passes only when the run ended,
  no test failed and the wake was measured; no test run, a run pytest did not finish, or no wake
  is sent as not measured, and an event the bus refuses fails the job.

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

The script of the image promotion job (`src/rhi/pipeline/image_promotion.py`), a CodeBuild project
beside the pipeline, `rhi-promote-image`, which the owner starts by hand and nothing else starts:

- `promote_image.py` promotes a GPU pool's machine image after a canary start on it passes
  (system design, section 4.3). It finds the pool's group by its tag `rhi-pool`, and refuses the
  pool, before it starts anything, when the candidate's release notes do not list the family of
  each of the group's instance types, or when the candidate is not an available image. It then
  starts one instance outside the group, from the group's launch template with the candidate in
  place of the template's image, in one of the group's subnets, tagged `rhi-pool`,
  `rhi-release-parameter` and `rhi-promotion=canary`, and reads its serial console for the line
  of its lifecycle agent. It terminates the canary whatever the line says. Only after
  `rhi-canary served ...` does it write the candidate's ID to `/rhi/images/<pool>`, and only after
  that write does it start an execution of the pipeline that `RHI_PROMOTE_PIPELINE` names. Any
  other end writes nothing and exits non-zero with a cause of its enumeration `Cause`. It never
  changes a group's capacity.

The owner starts a promotion with the pool, the candidate's ID and the families its release notes
list, comma-separated (AWS's base GPU image with the open-source NVIDIA driver on Ubuntu 22.04,
evidence E10):

    aws codebuild start-build --project-name rhi-promote-image --environment-variables-override '[
        {"name": "RHI_PROMOTE_POOL", "value": "coursework"},
        {"name": "RHI_PROMOTE_IMAGE", "value": "ami-<candidate>"},
        {"name": "RHI_PROMOTE_FAMILIES", "value": "G6e,G7e,P5,P5e,P5en,P6"}]'

The build succeeds once the canary served, the parameter holds the candidate and the pipeline's
execution has started; the deployment that execution makes refreshes the pool, after which the
pool's warm instances run the new image (`aws ec2 describe-instances` on the instances tagged
`rhi-pool=coursework` shows its ID). The pool's first image is written by hand before the pool
enters the configuration: the job serves every later one.
