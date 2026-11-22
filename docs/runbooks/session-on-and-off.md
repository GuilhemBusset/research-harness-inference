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
   draining instance and a cold launch can overlap. The quota counts vCPUs, so a pool needs
   (`max_instances` + 1) times the vCPUs of one instance of the largest type its `instance_types`
   lists, since a fallback type may be the one launched. For the example's coursework pool, whose
   only type is `g7e.2xlarge` with 8 vCPUs, that is 8 × (`max_instances` + 1): 24 for the example's
   2, 32 for 3. Look up the vCPUs of your pool's types in the EC2 console's instance types page if
   they differ. Each instance family has its own quota: a `g…` type counts against "Running
   On-Demand G and VT instances", a `p…` type against "Running On-Demand P instances". Add the needs
   of every pool the class uses whose types are of the same family, and compare each family's total
   with that family's quota. Read both quotas in the region of the endpoint:

   ```sh
   aws service-quotas list-service-quotas --service-code ec2 --query "Quotas[?QuotaName=='Running On-Demand G and VT instances' || QuotaName=='Running On-Demand P instances'].[QuotaName,Value]" --output text
   ```

   If it is short, request more, well ahead of the class, since an increase can take days. The quota
   step of the owner-setup script computes the request for a coursework pool of `g7e.2xlarge`
   (8 vCPUs an instance) alone, the example's: use the pool's actual `max_instances` as `N`, or the
   new maximum if step 2 raises it; the script adds the one instance more.

   ```sh
   setup/owner-setup.sh --step quotas --max-instances N
   ```

   If your pools list another type, a P-family type, or several pools of one family are used
   together, the script's figure is not yours: request the total you computed above instead, in the
   Service Quotas console, under Amazon EC2, for the quota of that family ("Running On-Demand G and
   VT instances" or "Running On-Demand P instances"), with **Request increase at account level**.

2. **A pool's maximum, if the class needs more instances.** Edit `max_instances` of the pool in
   your configuration file, kept outside the working tree, then validate and write it to SSM:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   The quota of step 1 must cover the new maximum plus one. The new value takes effect only once a
   pipeline run has deployed it. Once the script has written the configuration, start a run with
   **Release change** on the pipeline's page in the CodePipeline console, and note the execution
   ID the console shows for it: the run you started is the first row of the pipeline's execution
   history, with the trigger `StartPipelineExecution` and your identity. Check that execution
   itself, by its ID, from here on. An edit of SSM changes no commit, so the `deploy/<stage>` status
   of step 3 can still be an earlier run's success; only this execution says what happened to the
   edit. In the stage the class uses, it runs the deployment actions
   first and the verification (the action Verify, the smoke tests against the deployed endpoint)
   after them. Read the deployment first:

   - A deployment action of that stage failed, or the execution failed or was stopped before
     reaching them: the new value may not be deployed, or only partly. Fix the cause and start
     another run.
   - Every deployment action Succeeded and Verify failed: the new value **is** deployed, and the
     endpoint did not pass its smoke tests with it. Do not start the class on it. Fix the cause (or
     put the previous value back, by the same configuration step) and start another run.
   - Every action of that stage Succeeded, Verify included: the new value is deployed. This alone
     does **not** say that a smoke test ran. Verify also succeeds when it has nothing to test,
     without running one: when the endpoint has no HTTPS listener, or when the canary's key is
     missing because the canary command has not been run yet. Read what the verification did from
     the same execution, below.

   **Whether the smoke tests ran and passed.** Read it from the log of this execution's own Verify
   action, not from a commit status: an SSM edit deploys no new commit, so a `verify/<stage>` status
   on the commit can come from another execution of the same commit, posted before or after yours.
   In the execution's detail page (the execution ID you noted), in the stage the class uses, open
   the action Verify's details, which link to its CodeBuild log, and read the end of the log:

   - A line of pytest's summary that reports tests `passed` and none `failed` or `error`, for each
     run (one on the mock pool, and one more on each GPU pool whose release this execution
     deployed): the smoke tests ran against the endpoint with the new value, and passed.
   - The line `verify: no test was run:` followed by `listener-off` or `canary-missing`: the
     deployment is not verified, even though Verify Succeeded. For `canary-missing`, create the
     canary's key with the command below, then start another run with **Release change**, note
     its ID, and read its Verify log again; for `listener-off`, the HTTPS listener must be turned
     on first (it is off by default).
   - A summary that reports a test `failed` or `error`, or a Verify that failed: the verification
     failed, as in the case above where Verify failed.

   ```sh
   rhi_admin canary
   ```

   Switch the session on only once the execution you started after your last configuration change
   has succeeded through Verify **and** its own Verify log shows the smoke tests ran and passed.

3. **Every configuration change has landed, and no deployment is running.** A deployment that has
   passed the deployment hold's check runs to its end even after the session is switched on, so
   none may be under way when you switch it on. A change merged to `main` has landed when the
   latest commit of `main` carries a `deploy/<stage>` status that reads success; it has also been
   tested only when the commit's `verify/<stage>` status reads success with the description "The
   verification's tests ran and passed.", not one that begins "No test ran", which is the
   reporter's word for the Verify log's `verify: no test was run:` of step 2. A change made in SSM alone has landed only when the execution of step 2 succeeded, which
   the `deploy/<stage>` status cannot show. On GitHub:

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

   **A scheduled-only pool is not woken this way.** The admin CLI shows which pools are in mode
   `scheduled-only` (configured `wake = "scheduled-only"`, such as an upgrade pool) in the mode
   column of:

   ```sh
   rhi_admin pool status
   ```

   Students
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
   once with `no_session`; requests already running finish, and each pool falls to zero by its idle
   rule (section 5.6): `idle_minutes` after the last moment the scaler saw a request of the pool
   held or in flight, so a long request that was still running delays it by its whole remaining
   time. The deployment hold's
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

4. Each pool falls to zero once it has been idle for its whole `idle_minutes`, counted from when
   its last held or in-flight request cleared, not from when that request arrived: a request that
   was still running when you switched the session off (one may run up to 30 minutes) keeps the
   pool up until it ends, and the idle interval starts only then. So first wait until no request
   of the pool is running: in Grafana, on the dashboard "Fleet and scaling", in the pool's row, the
   panel "Engine requests: running and waiting, per instance" reads zero running and zero waiting
   for every instance. With the session off, the gateway takes no new request, but one it was
   already holding while the pool started stays held until the pool is ready or the hold's deadline
   passes (7 minutes for a stream, 4 for a call, section 5.3), and the scaler counts it as demand.
   So also wait until the gateway holds none for the pool: in Grafana's Explore, on the Prometheus
   data source, the query `sum by (pool) (rhi_gateway_requests_held)` reads zero, or no data, for
   the pool. Then allow the full `idle_minutes` (20 for the example's coursework), plus a few minutes
   for the scaler to act, before checking that each pool has no instance in service: on the same
   dashboard, in each pool's row, the panel "Instances: desired, in service, warm" reads zero for
   desired and for in service.

Walked through on the deployed system: not yet.
