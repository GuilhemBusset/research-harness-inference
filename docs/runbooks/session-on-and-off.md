# A session switched on and off

The endpoint serves students only while a session is on, and the instructor decides when it is:
there is no schedule in the configuration (system design, section 5.6). One record, the session
switch, says on or off; only the admin CLI writes it, and a missing record reads as off. This
runbook is the class day: what to check before, switching the session on and waking each pool,
and switching it off after.

Like every check against the endpoint, it assumes the public HTTPS listener is on, which it is not
by default.

**What this runbook rests on.** The admin CLI writes and reads the switch (`rhi_admin session`).
Two other parts act on it, and each lands with its own issue:

- **The gateway enforces the switch** once issue E4-41 is deployed: while the switch reads off, it
  answers a student's request and wake at once with `no_session`, and `rhi status` lists each pool
  as `unavailable` with that cause. Until that gateway is deployed, it serves students whether the
  switch reads on or off, and nothing answers `no_session`.
- **The deployment hold** lands with issue E4-42: a check that each pipeline stage runs before its
  deployment, which waits while the switch reads on (section 8.2). It is a check before the
  deployment, not a pause of one: a deployment that has passed it before the session is switched on
  runs to its end, refreshes the pools' instances and drains the gateway, in the middle of the
  class. So the session is switched on only once no pipeline execution is in progress (check 3
  below). Until the hold is deployed, nothing holds a deployment: merge nothing to `main` during a
  class.

The walk-through on the deployed system, at the foot of this runbook, needs both.

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

`rhi` is the students' CLI. Log in once with your own roster entry; the endpoint's address, here
the placeholder `https://inference.example.com`, is given at the first login only:

```sh
rhi login --endpoint https://inference.example.com
```

The pools are named by their model name, the name `rhi status` prints; `coursework` below stands
for each on-demand pool the class uses (configured `wake = "on-demand"`, mode `enabled`).

## When

- Before a class: the day before, for the checks; then, on the day, switching the session on, and
  waking each pool shortly before the class starts (Act, step 2, says how shortly).
- After the class, once the last student has finished: switch the session off.

## Check

Do these the day before the class, while no session is on.

1. **The GPU quota covers one instance more than each pool's `max_instances`** (section 5.4): a
   draining instance and a cold launch can overlap. A coursework instance takes 8 vCPUs, so a pool
   of `max_instances` instances needs 8 × (`max_instances` + 1) vCPUs of the G-family quota: 24 for
   the example's 2, 32 for 3. Read the quota in the region of the endpoint:

   ```sh
   aws service-quotas list-service-quotas --service-code ec2 --query "Quotas[?QuotaName=='Running On-Demand G and VT instances'].Value" --output text
   ```

   If it is short, request more with the quota step of the owner-setup script. Use the coursework
   pool's actual `max_instances` as `N`, or the new maximum if step 2 raises it; the script adds the
   one instance more. An increase can take days, so do this well ahead of the class:

   ```sh
   setup/owner-setup.sh --step quotas --max-instances N
   ```

2. **A pool's maximum, if the class needs more instances.** Edit `max_instances` of the pool in
   your configuration file, kept outside the working tree, then validate and write it to SSM:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   The quota of step 1 must cover the new maximum plus one. The new value takes effect only once a
   pipeline run has deployed it. Note the time once the script has written the configuration, then
   start a run with **Release change** on the pipeline's page in the CodePipeline console. Check
   that run itself, not a status left by an earlier one: in the pipeline's execution history, the
   execution whose start time is after the time you noted must have succeeded, with its status
   Succeeded and every stage up to and including the stage the class uses Succeeded. An edit of
   SSM changes no commit, so the `deploy/<stage>` status of step 3 can still be an earlier run's
   success; only this execution says the edit is deployed. If it failed or stopped, the new value
   is not deployed: fix the cause and start another run, and switch the session on only once one
   has succeeded.

3. **Every configuration change has landed, and no deployment is running.** A deployment that has
   passed the deployment hold's check runs to its end even after the session is switched on, so
   none may be under way when you switch it on. A change merged to `main` has landed when the
   latest commit of `main` carries a `deploy/<stage>` status that reads success; a change made in
   SSM alone has landed only when the execution of step 2 succeeded, which this status cannot show.
   On GitHub:

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/main/status --jq '.statuses[] | "\(.context) \(.state)"'
   ```

   On the pipeline's page in the CodePipeline console, no execution is in progress. Check this
   again on the day, just before Act, step 1; if one is, wait for it to finish.

4. **No session is on yet,** and nothing was left switched on from the last class:

   ```sh
   rhi_admin session status
   ```

## Act

1. **Switch the session on,** once check 3 shows no execution in progress. From then on the gateway
   serves every student on the roster, and the scaler takes each pool from zero to its
   `max_instances` by load (section 5.1). The switch alone starts no instance: a pool starts on
   demand, or when it is woken (step 2).

   ```sh
   rhi_admin session on
   ```

2. **Wake each on-demand pool** the class uses, shortly before the class starts. `rhi wait` asks
   for the pool, repeats the request every minute, and prints each change of state until the pool
   is ready. From the warm pool a start takes an estimated 2 to 6 minutes; a cold launch, when the
   warm instance cannot start, about 3 to 13 (section 5.2).

   ```sh
   rhi wait coursework
   ```

   **A scheduled-only pool is not woken this way.** `rhi_admin pool status` shows which pools are in
   mode `scheduled-only` (configured `wake = "scheduled-only"`, such as an upgrade pool). Students
   cannot wake such a pool, and neither can `rhi wait`: while nothing has asked for its capacity, a
   wake asks for nothing, and `rhi wait` stops at once with the cause `pool_scheduled_only` (section
   5.3). The admin starts it separately, by asking for its capacity; this repository has no command
   for that yet, so it is not part of this runbook. Once it has been started, confirm it with
   `rhi status` (Verify, step 2), which lists it as `warming`, then `ready`.

   **Time it by the pool's idle rule.** The moment a wake finds the pool ready counts as a request,
   and a pool falls to zero `idle_minutes` after its last request (section 5.6); once `rhi wait`
   has exited, nothing keeps the pool up until the students send their requests. So start the last
   `rhi wait` no earlier than the pool's `idle_minutes` before the class starts: with the example's
   20 minutes for coursework, about 15 minutes before covers a cold launch, and a pool that is ready
   by then stays up until at least the class's first minutes. `rhi status` creates no demand and
   keeps nothing up.

   It exits with the cause when the pool is unavailable: `capacity_unavailable` is the runbook
   [capacity failure](capacity-failure.md), `pool_disabled` the runbook
   [a disabled pool](disabled-pool.md), `pool_scheduled_only` a scheduled-only pool that nothing
   has started (above), and `no_session` means the switch does not read on.

3. **Just before the class starts, read each pool's state** (Verify, step 2). An on-demand pool
   that reads `cold` again fell to zero by its idle rule: run `rhi wait` for it again, as in step 2,
   and the pool is ready a few minutes later.

   ```sh
   rhi status
   rhi wait coursework
   ```

4. **After the class, switch the session off.** Students' requests and wakes are then refused at
   once with `no_session`; requests already running finish, and each pool falls to zero
   `idle_minutes` after its last request, by its idle rule (section 5.6). The deployment hold's
   check passes from now on, so a pipeline execution that was waiting at it goes on to deploy.

   ```sh
   rhi_admin session off
   ```

## Verify

1. After switching it on, the switch reads on, with your identity and the time:

   ```sh
   rhi_admin session status
   ```

2. `rhi status` shows each pool as `cold`, `warming`, `ready` or `unavailable`, with the cause when
   it is unavailable. When the class starts, every pool the class uses reads `ready`; an on-demand
   pool that reads `cold` is woken again (Act, step 3). A scheduled-only pool reads `unavailable`
   with the cause `pool_scheduled_only` until the admin has started it, then `warming` and `ready`:

   ```sh
   rhi status
   ```

3. After switching it off, the switch reads off, with your identity and the time, and `rhi status`
   lists each pool as `unavailable` with the cause `no_session`:

   ```sh
   rhi_admin session status
   rhi status
   ```

4. Within `idle_minutes` of the last request, plus a few minutes for the scaler to act, each pool
   has no instance in service: in Grafana, the panel Instances of the fleet and scaling dashboard
   shows no instance in service and a desired capacity of zero for each pool.

Walked through on the deployed system: not yet.
