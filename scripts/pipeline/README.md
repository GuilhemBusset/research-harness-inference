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
deployment, with the folders of pytest's import path in `PYTHONPATH` for the booking:

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
  write in their heartbeat rows with the deployed one. Around each run it books the run's pool
  through `booking.py`, for twice the configuration's `check_wait_minutes`, waits until
  `GET /status` reads the pool ready or `check_wait_minutes` have passed, and stops the session it
  booked whatever the tests' result; a pool that already has a session is neither booked nor
  stopped. When the wait for a pool of the decision's file ends with the pool not ready and no
  instance's heartbeat row written since the booking, which it looks for through the wait and at
  its end, no instance was launched: it runs no test on that pool, stops its session, leaves the
  one line `no-capacity` in the file `RHI_VERIFY_GPU_REASON_FILE` names and exits with the result
  of its other runs. The post-build phase copies that file as the mark `gpu-not-run`, from a build
  that fails too; the reporter takes the mark for a success only when the run that succeeded wrote
  it, so that a retry's success says only what the retry ran. With such a row, or
  when the table's answers cannot tell, a read that failed among them, the tests run and their
  failure is the script's.
- `booking.py` is what a check books a pool through: `book` writes a session of one instance, for
  the minutes and under the name it is given, with the rules of `rhi_common.sessions`, only on a
  pool that has no session booked or active; `stop` ends that session only while the item still
  reads as the booking. It is a module, not a script: the canary and the spike jobs of the mock
  pool book the same way.

The script of the weekly GPU canary (`src/rhi/pipeline/canary.py`), a job its schedule starts on
Sundays at 04:00 UTC, with the repository's package roots in `PYTHONPATH`:

- `canary_result.py` reads the canary's key as `verify.py` does, and the stage's workspace and
  heartbeats table from the settings of the coursework pool's release parameter. When the
  coursework pool has no session, it books one instance on it with `booking.py`, under the name
  `pipeline/canary`, through `verify.py`'s routine: it waits until `GET /status` reads the pool
  ready or `check_wait_minutes` have passed, runs the smoke tests of `tests/smoke` once against
  the deployed endpoint, on the coursework pool, through `verify.py`'s runner, and stops the
  session it booked, after a failure too. A pool that already has a session is neither booked nor
  stopped: the tests run on the instances that are there. The tests are every test the
  verification runs on that pool after a refresh, `release-heartbeat` included, whose query of the
  heartbeats table the canary's role may make for the coursework pool's rows alone. It then sends
  the measurement `gpu-canary` (the verdict, `wait_seconds`, `smoke_tests_passed`,
  `smoke_tests_failed`, `no_capacity`) on the measurement bus, on the commit the job ran from,
  with the run's UTC date, YYYY-MM-DD, as its run label. One of three results: pass when the
  tests ran and passed; fail when a test failed, or an instance was launched and did not serve by
  the end of the wait; and error, not measured, with `no_capacity` 1 and no test run, when no
  instance was launched during the wait, read from the coursework pool's heartbeat rows as the
  verification reads it. `wait_seconds` is the time from the booking to the pool's first `ready`,
  and 0 on a pool that was ready when the job began. A run that could not measure for another
  reason, no test run or a run pytest did not finish, is sent as not measured with `no_capacity`
  0, and an event the bus refuses fails the job.

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

The script of the step that holds the deployment while a session is active on any pool
(`src/rhi/pipeline/hold.py`), which runs after the copy of the weights and before the stage's
deployment, with the same import path:

- `deploy_hold.py` finds the pool-state table by its logical ID, `RHI_HOLD_TABLE_LOGICAL_ID`, in
  the pools stack, `RHI_HOLD_POOLS_STACK`, and reads from it the session of each pool that
  `RHI_HOLD_POOLS` names, separated by commas, and the session switch. A session holds the
  deployment while it is active, with `RHI_HOLD_SESSION_MAX_HOURS` as the longest a session lasts.
  It exits 0 at once when no session is active and the switch reads off, which is how a missing
  item reads, or when the stack or the table does not exist yet; otherwise it reads them all again
  every 60 seconds until that is so. An empty `RHI_HOLD_POOLS` is a stage with no pool: only the
  switch is read. Any other failed read, an item that is not a session or a switch that does not
  read, fails the step, and the deployment waits for the next execution.

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
