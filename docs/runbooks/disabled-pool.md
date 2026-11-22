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

1. **Every pool's mode, who set it and when:** a pool in its configured mode reads `configured`
   under `set_by`; a disabled one reads `disabled`, with the identity that disabled it and the time.

   ```sh
   rhi_admin pool status
   ```

2. **What the students see:**

   ```sh
   rhi status
   ```

3. **Before re-enabling,** the reason the pool was disabled is gone: ask whoever `set_by` names.

## Act

To disable a pool. The scaler wakes it no more and releases its instances as they fall idle;
requests already running finish.

```sh
rhi_admin pool disable coursework
```

To re-enable it. The pool goes back to its configured mode: `enabled` for a pool that wakes on
demand, `scheduled-only` for one that does not. While a session is on, wake it again:

```sh
rhi_admin pool enable coursework
rhi wait coursework
```

## Verify

1. The mode reads `disabled` after the first command, and the configured mode after the second,
   each with your identity and the time:

   ```sh
   rhi_admin pool status
   ```

2. `rhi status` lists a disabled pool as `unavailable` with the cause `pool_disabled`. After
   re-enabling it, and while a session is on, it reads `cold`, then `warming` and `ready` once
   `rhi wait coursework` has woken it.

3. After disabling, in Grafana, the panel Instances of the fleet and scaling dashboard shows the
   pool's desired capacity at zero once its last request has finished.

Walked through on the deployed system: not yet.
