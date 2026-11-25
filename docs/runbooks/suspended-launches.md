# A pool whose launches are suspended

A launch that fails every time (a bad image, a wrong prefix, a rotated secret) would loop for the
whole session: launch, wait, abandon, relaunch, each attempt billed (system design, section 5.4).
So the scaler counts the launches abandoned in a row, and after three it suspends the launches of
the pool's group, sets the mark `launch_suspended` in the pool's record, and waits for the admin
(section 5.1). While the mark stands the pool is not raised: a session asks for instances and gets
none. Nothing clears the mark but the admin's command, and the scaler never sets it back by itself.

A launch is **abandoned** when the instance started and did not become ready to serve: a step of
its start failed, the release on the instance was not the pool's, or the start's deadline passed.
A launch that found no capacity, or that the quota refused, is not abandoned: it is waited for, it
suspends nothing, and it is the runbook [capacity failure](capacity-failure.md).

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. Anything else
(the AWS console, Grafana, `aws`) is written out in full and is tested only by walking through the
runbook.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

`rhi` is the students' CLI, logged in with your own roster entry. `coursework` below stands for the
pool, by its name in the configuration, which is also the model name `rhi status` prints.

## When

- **The alarm:** an e-mail from the alerts topic for the alarm of the pool that says the scaler
  has suspended the pool's launches after three abandoned launches. It is sent once, when the
  scaler suspends them, and nothing is sent when the alarm clears.
- **`rhi_admin pool status`** reads `true` under `launch_suspended` for the pool.
- **`rhi_admin session status`** reads the cause `launches-suspended` for the pool, or a session
  that stays short with `requested` above `asked`.
- **Students** see a pool that does not come up during its session, or stays short of its
  instances. With none in service or starting, `rhi status` lists it as `unavailable` with
  `capacity_unavailable`: the students' side does not tell suspended launches from missing
  capacity. The checks below do.

## Check

1. **Whether the launches are suspended.** One line for each deployed pool: `true` when the scaler
   has suspended its launches, `false` otherwise.

   ```sh
   rhi_admin pool status
   ```

   `false` for a pool that does not come up is not this runbook: read the cause in step 2.

2. **What the session has, and the cause.**

   ```sh
   rhi_admin session status
   ```

   A suspended pool shows in one of two ways. While instances that were asked of AWS before the
   suspension have not arrived, `waiting` is above zero and `cause` reads `launches-suspended`. A
   session booked, or raised, after the suspension is not passed on to AWS at all: `requested`
   stays above `asked`, with nothing waiting and no cause. In both, the instances already in
   service stay and keep serving, and a session that is lowered or stopped still releases them.

3. **Why three launches were abandoned.** Find it before clearing anything: launches that resume
   on the same fault are abandoned again, each one billed.

   - **What changed last.** A deployment of a pool's release (a new engine image, a new model
     revision or weights prefix, a new machine image) points to the runbooks
     [bad deployment](bad-deployment.md) and
     [image and engine upgrade](image-and-engine-upgrade.md); a rotated secret to the runbook
     [secret and token rotation](secret-rotation.md).
   - **Where the starts stopped.** In Grafana, on the dashboard "Fleet and scaling", in the pool's
     row, the panel "Cold launch: seconds per stage, per launch" has one series for each stage of
     each launch, with the time range set over the launches. An abandoned launch keeps the stages
     it went through, as far as the instance reported them before it left, and has none after the
     one it stopped in. The stages, in order, and what to suspect of a launch that stops in each:

     | Stage | What the instance does | What to suspect |
     | --- | --- | --- |
     | `image` | Reads the pool's release, and pulls the engine image it names | The image is not there, or cannot be pulled: a bad image |
     | `release-check` | Checks that the release on disk is the pool's current one | The instance holds another release than the pool's |
     | `container-start` | Reads the pool's secret and starts the engine | The secret, which cannot be read, or the engine's container, which does not start |
     | `weights` | Waits for the engine's health route, while the engine loads the weights | The weights are not where the release says, a wrong prefix, or the engine does not start with them |
     | `warm-up` | Sends the engine one request | The engine started and does not answer |

     Three launches that stop in the same stage point to one cause. The pool's log group,
     `/rhi/pools/coursework` with the pool's name, holds what its instances logged around the time
     of each launch.
   - **When the launches ran.** The group of a pool is named `rhi-pool-` followed by the pool's
     name. Its last activities, with when each started and ended and how it ended:

     ```sh
     aws autoscaling describe-scaling-activities --auto-scaling-group-name rhi-pool-coursework \
         --max-items 10 --query 'Activities[].[StartTime,EndTime,StatusCode]' --output text
     ```

4. **Whether a session is active on the pool** (step 2). While one is, the pool is raised as soon
   as the suspension is cleared. And a fix that is a deployment waits while a session is active on
   any pool (section 5.6): stop the sessions first, with the runbook
   [a session booked, changed and stopped](sessions.md), and tell the students to work with their
   own subscriptions meanwhile.

## Act

1. **Fix the cause,** with the runbook that Check, step 3, pointed to. Until it is fixed, leave
   the suspension as it is: a suspended pool launches nothing, so the fault costs nothing more.

   A fix that is a deployment is the one case where the suspension is cleared first (step 2), once
   every session is stopped and just before the fix is merged or its execution started: with no
   session on the pool, clearing launches nothing, and the pipeline's verification, which books the
   pool it tests, needs the launches (runbook [bad deployment](bad-deployment.md), Act, step 1).

2. **Clear the suspension,** once the cause is fixed. The command sets the mark back to false and
   writes who cleared it and when; it is refused for a pool whose launches are not suspended.

   ```sh
   rhi_admin pool clear-suspension coursework
   ```

   The scaler resumes the group's launches at its next run, within a minute. Ask for one now:

   ```sh
   rhi_admin pool run
   ```

   It prints each pool's decision: the pool, the instances asked of AWS and the reason. A pool
   with an active session that was short reads `provision`; `launch-suspended` means the mark still
   stands. When the command answers that the scaler is already running, that run or the next one,
   within a minute, resumes the launches.

3. **Book the pool again,** if its session was stopped or has ended, with the runbook
   [a session booked, changed and stopped](sessions.md).

## Verify

1. The pool reads `false` under `launch_suspended`:

   ```sh
   rhi_admin pool status
   ```

2. With a session active on the pool, `asked` reads what the session requests, no cause reads
   `launches-suspended`, and the instances pass from `waiting` to `starting` to `in_service`:

   ```sh
   rhi_admin session status
   ```

   The scaler counts abandoned launches anew from the run that found the mark cleared. A pool that
   reads `true` again was suspended by three more: the fix did not fix the launch. Go back to
   Check, step 3.

3. Students see the pool `warming`, then `ready`:

   ```sh
   rhi status
   ```

Walked through on the deployed system: not yet.
