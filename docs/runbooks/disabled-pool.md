# A disabled pool, and re-enabling it

Disabling a pool is the admin's kill switch (system design, section 11.3). A disabled pool is never
woken, its idle instances are released, and the gateway answers its requests at once with
`pool_disabled`, a 503 (section 6.2). The pool's mode is one record of the pool-state table that
survives deployments, and only the admin CLI changes it: nothing disables a pool on its own. There
are no GPU-hour ceilings and no budget latch; the monthly budget only sends alerts (section 11.3).
A pool that reads disabled was disabled by a person, and stays so until a person re-enables it.

Like every check against the endpoint, this runbook assumes the public HTTPS listener is on, which
it is not by default.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`; this runbook needs none. Anything else (the
AWS console, GitHub, `aws`, `gh`, `git`) is written out in full and is tested only by walking
through the runbook.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer uv run --locked --no-dev python -m rhi_admin.main'
```

`rhi` is the students' CLI, logged in with your own roster entry. `coursework` below stands for the
pool, by its name in the configuration, which is also the model name `rhi status` prints.

## When

- **A pool shows as disabled:** `rhi status` lists it as `unavailable` with the cause
  `pool_disabled`, `rhi wait` stops at once with that cause, and students report `pool_disabled`
  from their harness.
- **A pool must stop now, whatever its load,** and stay stopped across deployments: it misbehaves
  (wrong answers, an engine that keeps failing), its release is suspected, or its GPU time must end
  before its idle rule would end it. To stop serving every student at once, switch the session off
  instead (runbook [a session switched on and off](session-on-and-off.md)); to remove what costs
  money at idle, park the endpoint.

## Check

1. **Every pool's mode, who set it and when:** a pool whose mode no admin has set reads its
   configured mode, with `configured` under `set_by`; a disabled one reads `disabled`, with the
   identity that disabled it and the time; a re-enabled one reads its configured mode, with the
   identity that re-enabled it and the time.

   ```sh
   rhi_admin pool status
   ```

2. **What the students see,** while a session is on:

   ```sh
   rhi status
   ```

   With the session off, once the gateway enforces the switch (issue E4-41), every pool reads
   `unavailable` with the cause `no_session`, whatever its mode: the students' view then says
   nothing about a disabled pool, and step 1 is the check.

3. **Before re-enabling,** the reason the pool was disabled is gone: ask whoever `set_by` names.

## Act

To disable a pool. The scaler wakes it no more and releases its instances as they fall idle;
requests already running finish.

```sh
rhi_admin pool disable coursework
```

To re-enable it. The pool goes back to its configured mode, which `rhi_admin pool status` then
shows: `enabled` for a pool configured `wake = "on-demand"`, `scheduled-only` for one configured
`wake = "scheduled-only"`.

```sh
rhi_admin pool enable coursework
```

Then, only for a pool that reads `enabled`, and while a session is on, wake it so that it is ready
for the students rather than started by the first request:

```sh
rhi wait coursework
```

A pool that reads `scheduled-only` is not woken this way: students cannot wake it (section 5.3),
so `rhi wait` stops at once with the cause `pool_scheduled_only` and asks for no capacity. It starts
only when the admin asks for its capacity, which is not part of this runbook.

## Verify

1. The mode reads `disabled` after disabling, with your identity under `set_by` and the time under
   `set_at`. After re-enabling it reads the configured mode, `enabled` or `scheduled-only`, again
   with your identity and the time: this is the check of either kind of re-enable, and of the
   disable, whether a session is on or off.

   ```sh
   rhi_admin pool status
   ```

2. While a session is on, `rhi status` lists a disabled pool as `unavailable` with the cause
   `pool_disabled`. While the session is off, it lists every pool with the cause `no_session`
   instead, once the gateway enforces the switch, even when the disable succeeded: step 1 is then
   the check, and this one waits for the next session. After re-enabling, again with a session on:

   - a pool back in mode `enabled` reads `cold` while it has no instance; once `rhi wait` has woken
     it, during a session, it reads `warming`, then `ready`, and `rhi wait` exits 0;
   - a pool back in mode `scheduled-only` that has no instance reads `unavailable` with the cause
     `pool_scheduled_only`, not `pool_disabled`: the re-enable worked, and the pool waits for the
     admin to start it.

   ```sh
   rhi status
   ```

3. After disabling, in Grafana, the panel Instances of the fleet and scaling dashboard shows the
   pool's desired capacity at zero once its last request has finished.

Walked through on the deployed system: not yet.
