# Capacity failure during a session

AWS may have no GPU instance to give when a pool starts or grows. The design does what it can
(system design, section 5.4): each pool's group spans three zones and may list fallback instance
types with the same GPU, and a warm instance that fails to start is replaced by a cold launch. When
none of that finds an instance, the gateway holds a student's request until its deadline, then
answers `capacity_unavailable`. There is no fallback command and no other endpoint: students work
with their own subscriptions to hosted models until a pool is up, and nothing here stands in for
them (section 5.4; section 15, decision 12).

Like every check against the endpoint, this runbook assumes the public HTTPS listener is on, which
it is not by default.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. Anything else (the AWS console, GitHub, `aws`,
`gh`, `git`) is written out in full and is tested only by walking through the runbook.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer uv run --locked --no-dev python -m rhi_admin.main'
```

`rhi` is the students' CLI, logged in with your own roster entry. `coursework` below stands for the
pool that cannot start, named by its model name as `rhi status` prints it.

## When

During a session, a pool does not come up:

- `rhi wait` stops and prints the cause `capacity_unavailable`, or is still `warming` well past the
  13 minutes a cold launch is estimated to take (section 5.2);
- `rhi status` lists the pool as `unavailable` with the cause `capacity_unavailable`: its last
  launch found no capacity in the past 10 minutes (section 5.3);
- students report that their harness waited, then failed with `capacity_unavailable` (section
  6.2). How it shows depends on the kind of call. A streaming call, which harnesses use for chat,
  is answered HTTP 200 at once and kept alive while the gateway holds it; at the deadline (the
  pool's `hold_seconds`, 7 minutes by default) the stream ends with the error event of its wire
  format, which carries `capacity_unavailable`, so the harness shows an error after a stream that
  began well, not an HTTP status. A non-streaming call is held silently, and at its deadline
  (`hold_seconds_non_streaming`, 4 minutes by default) it is answered HTTP 503 with
  `Retry-After: 60` and `capacity_unavailable` in the error body. Either way, the cause at the
  deadline depends on the pool's last launches: `capacity_unavailable` when a launch of the pool
  found no capacity in the past 10 minutes, and `warming` when none did, which means the pool is
  still starting and is not this runbook (section 5.3).

## Check

1. **The cause, as the students see it:**

   ```sh
   rhi status
   ```

   A pool `unavailable` with `capacity_unavailable` is this runbook. `no_session` is the session
   switch (runbook [a session switched on and off](session-on-and-off.md)), and `pool_disabled` a
   pool the admin disabled (runbook [a disabled pool](disabled-pool.md)).

2. **What the scaler did.** The pool's mode, and whether the scaler has suspended its launches:
   after three abandoned launches in a row it stops launching and waits for the admin (section 5.4).

   ```sh
   rhi_admin pool status
   ```

   `launch_suspended` reads `true` when it has. That is a launch that fails every time (a bad image,
   a wrong prefix, a rotated secret), not missing capacity: it is the runbook of a bad deployment,
   not this one. While it reads `false`, the scaler keeps asking for capacity: each wake on a pool
   unavailable for capacity is passed to it, and it tries again.

3. **The quota is not the limit.** A request refused for the vCPU quota looks the same to a
   student. In the EC2 console, the Auto Scaling group of the pool lists its recent launch attempts
   under **Activity**; an attempt refused for the quota, rather than for capacity in the zones, is
   fixed by the quota step of the runbook [a session switched on and off](session-on-and-off.md),
   and not during the class.

## Act

1. **Tell the students to work with their own subscriptions** to hosted models, for now. Nothing in
   this repository stands in for a pool: there is no fallback command, and no other model is
   switched in. Leave the session on.

2. **Keep asking for the pool.** A wake on a pool unavailable for capacity still reaches the
   scaler, which launches again (section 5.3). Run `rhi wait` again every ten minutes or so: each
   run sends a wake. A launch that found no capacity stays visible for 10 minutes: until then the
   pool reads `unavailable` with `capacity_unavailable`, and `rhi wait` stops at once with that
   cause, even when a later launch has found an instance that is still starting. Only once those 10
   minutes have passed does a pool that is starting read `warming`, and `rhi wait` then waits until
   it is ready. So neither `capacity_unavailable` nor `warming` says whether a launch has
   succeeded; `ready` is the one sign that the pool is up (Verify).

   ```sh
   rhi wait coursework
   ```

3. **After the class, if capacity failed often,** consider listing fallback instance types for the
   pool: the next sizes of its family, with the same GPU. They cost more, so each needs its price.
   Edit the pool's `instance_types` and `prices`, and `allowed_instance_types`, in your
   configuration file, kept outside the working tree, then validate and write it to SSM:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   It takes effect at the next pipeline run, which you start, while no session is on, with
   **Release change** on the pipeline's page in the CodePipeline console. The GPU quota must still
   cover one instance more than the pool's `max_instances`, of the largest type it lists.

## Verify

The pool is up again when `rhi wait` exits 0, having printed `ready`, or when `rhi status` lists
the pool as `ready`:

```sh
rhi status
```

Then tell the students they can switch back to the endpoint; their harness needs no change.
`rhi_admin pool status` still reads `launch_suspended` `false` for the pool.

Walked through on the deployed system: not yet.
