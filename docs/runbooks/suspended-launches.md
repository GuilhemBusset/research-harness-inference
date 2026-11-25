# A pool whose launches are suspended

A launch that fails every time (a bad image, a wrong prefix, a rotated secret) would loop for the
whole session: launch, wait, abandon, relaunch, each attempt billed (system design, section 5.4).
So the scaler counts the launches abandoned in a row, and after three it suspends the launches of
the pool's group, sets the mark `launch_suspended` in the pool's record, and waits for the admin
(section 5.1). While the mark stands the group launches nothing and the pool is not raised: what a
session asks beyond what the group had already been asked for is not passed on to AWS. Nothing
clears the mark but the admin's command, and the scaler never sets it back by itself.

A launch is **abandoned** when the instance started and did not enter service: a step of its start
failed, the release on the instance was not the pool's, the start's deadline passed, or the
instance's answer to the launch hook never reached Auto Scaling and the hook's timeout passed. A
launch that found no capacity, or that the quota refused, is not abandoned: it is waited for, it
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
- **`rhi_admin session status`** reads the cause `launches-suspended` for the pool, or `requested`
  above `asked`.
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

   The numbers from `requested` on are what the scaler wrote at its last run, within the last
   minute. The scaler does not raise a suspended pool: `asked`, what the pool's group is asked for,
   stays where the suspension found it, or goes down with the session. Two things show it, one of
   them or both:

   - `waiting` above zero, with the cause `launches-suspended`: instances the group was asked for
     before the suspension and has not got. `waiting` is `asked` less the instances starting and in
     service, and a cause is shown only while `waiting` is above zero;
   - `requested` above `asked`: a session booked, or raised, since the suspension. What it asks
     beyond `asked` is not asked of AWS at all, so it is not counted under `waiting`, and by itself
     it shows no cause.

   The instances in service stay and keep serving, and a session that is lowered or stopped still
   releases them.

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
     it went through, the one it was abandoned in included, as far as the instance reported them
     before it left, and has none after that one. The stages after the boot, in order, and what to
     suspect of a launch whose last stage is each:

     | Stage | What the instance does | What to suspect |
     | --- | --- | --- |
     | `image` | Reads the pool's release, and pulls the engine image it names | The image is not there, or cannot be pulled: a bad image |
     | `release-check` | Checks that the release on disk is the pool's current one | The instance holds another release than the pool's |
     | `container-start` | Reads the pool's secret and starts the engine | The secret, which cannot be read, or the engine's container, which does not start |
     | `weights` | Waits for the engine's health route, while the engine loads the weights | The weights are not where the release says, a wrong prefix, or the engine does not start with them |
     | `warm-up` | Sends the engine one request | The engine started and does not answer |
     | `hook` | Answers the launch hook, every step above having passed | Of a launch that was abandoned all the same: the answer did not reach Auto Scaling, so the hook waited out its timeout, which abandons the instance; or the instance could not record that it has served |

     The stage `hook` is the instance's attempt to answer the launch hook. It says that the
     instance's own steps passed, not that the launch completed: a completed launch has it last,
     and so has a launch abandoned at that stage. So the panel alone does not tell a completed
     launch from an abandoned one. Two things do, for each launch:

     - how its activity in the pool's group ended, below. An activity names its instance in
       `Description`, and each series of the panel is named by an instance and a stage;
     - the instance's own log. The pool's log group, `/rhi/pools/coursework` with the pool's name,
       has one stream for each instance, named by the instance, with what its units logged, as far
       as the instance shipped it before it left. The lifecycle agent, the unit `rhi-lifecycle`,
       ends a launch with a line that says how the launch transition ended: `continued` when Auto
       Scaling took its answer that the launch passed, `abandoned` when a step failed and the
       agent answered so, `not-completed` when no answer of the agent reached Auto Scaling. After
       the last of the three the instance stays as it is until the hook's timeout, 15 minutes or
       twice the pool's measured start when that is longer, and is abandoned then.

     Three launches that stop in the same stage point to one cause.
   - **Which launches were abandoned, and when.** The scaler counts them in the activities of the
     pool's group, which is named `rhi-pool-` followed by the pool's name. Its last activities, the
     newest first, each with what it was, how it ended and why:

     ```sh
     aws autoscaling describe-scaling-activities --auto-scaling-group-name rhi-pool-coursework \
         --max-items 20 --output json \
         --query 'Activities[].{what:Description,status:StatusCode,why:StatusMessage,started:StartTime,ended:EndTime}'
     ```

     A launch is an activity whose `Description` begins with the word launching, or names the warm
     pool as where the instance came from (below). It was abandoned when its `StatusCode` is
     `Failed` or `Cancelled` and its `StatusMessage` says that the launch's lifecycle hook was
     abandoned. A launch that failed for lack of capacity, or that the quota refused, says so
     there instead: it was not abandoned, and is the runbook
     [capacity failure](capacity-failure.md). A launch that reads `Successful` reached service,
     unless it was a launch into the warm pool (below), and the scaler starts its count again at
     the time it ended.

     Auto Scaling keeps a group's activities for weeks, so the list may still hold those of the
     warm pool, the stopped instances that each group kept until section 4.5 dropped them. The
     scaler reads them as it reads the others, with two differences, and so must you to find the
     same three launches:

     - an activity whose `Description` says that the instance came from the warm pool, or was
       moved out of it, is a launch too, whatever word it begins with. `Successful`, the instance
       reached service and the count starts again there; abandoned, it counts as one;
     - a `Successful` activity whose `Description` says that the instance was launched into the
       warm pool reached no service: it was launched to be stopped. It leaves the count as it is,
       so the abandoned launches before it and after it are counted in one row.

     Read the activities between two times that the scaler keeps in the pool's memory record, in
     the stage's pool-state table. The table's name, on the line whose logical ID holds
     `PoolState`:

     ```sh
     aws cloudformation list-stack-resources --stack-name Rhi-Pools \
         --query "StackResourceSummaries[?ResourceType=='AWS::DynamoDB::Table'].[LogicalResourceId,PhysicalResourceId]" --output text
     ```

     and the two times, `<pool-state-table>` being that name:

     ```sh
     aws dynamodb get-item --table-name <pool-state-table> --consistent-read \
         --key '{"pool": {"S": "coursework"}, "record": {"S": "memory"}}' \
         --query 'Item.{suspended_at: suspended_at.N, abandons_counted_from: abandons_counted_from.N}'
     ```

     Each is a time in seconds, which `date -u -d @<seconds>` prints as a date, in UTC. Read both
     before clearing anything: the run that finds the suspension cleared removes the first and
     moves the second.

     - `suspended_at` is the time of the run that counted the third abandoned launch and
       suspended the launches. The alarm and the dashboard support it and do not replace it. The
       alarm's e-mail gives the time the alarm changed state, a minute or two after that run. On
       the dashboard, the pool's panel of scale decisions by reason counts `launch-suspended` for
       the runs that kept a suspended pool from a raise, or that had nothing else to do on it. A
       run that lowers the pool instead, as when its session has just ended, counts under
       `release`, and one that only raises the rebalance number under `rebalance`. The run that
       suspended the launches can be one of those, so the first `launch-suspended` on the panel
       may be later than the suspension.
     - `abandons_counted_from` is where the count last started again by the admin's doing: the
       time of the run that found an earlier suspension cleared. That run comes up to a minute
       after the command `pool clear-suspension`, and a launch abandoned between the two is not
       counted. It reads `null` for a pool whose launches were never suspended before.

     The scaler counts by the time a launch ended, `EndTime`. So the launches that suspended the
     pool are the abandoned ones that ended by `suspended_at`, and after the later of two
     moments: the end of the last launch that reached service by `suspended_at`, and
     `abandons_counted_from` when the record holds one.

     Set aside what ended after `suspended_at`. A launch already under way when the scaler
     suspended the launches still runs to its end. One that is abandoned then is one more after
     the three. One that is successful then did not undo the suspension, which only the admin's
     command clears, and it is not where the count started: counted from it, the history holds no
     abandoned launch at all, while the three are before it. What it does say is that the fault
     does not fail every launch.

     The times of the three, against those of the deployments and the rotations above, say which
     change they followed, and are the times to read the dashboard and the pool's log group at.

4. **Whether a session is active,** on the pool and on any other (step 2). On the pool: the scaler
   raises it at its first run after the suspension is cleared, so a suspension cleared during a
   session launches within a minute. On any pool: a fix that is a deployment waits while a session is
   active (section 5.6). For such a fix, stop the sessions first, with the runbook
   [a session booked, changed and stopped](sessions.md), let their instances leave (its Verify,
   step 3), and tell the students to work with their own subscriptions meanwhile.

## Act

1. **Fix the cause,** with the runbook that Check, step 3, pointed to, and leave the suspension
   as it is until the cause is fixed: a suspended pool launches nothing, so the fault costs nothing
   more, and launches that resume on the same fault are abandoned again.

   When the fix is a deployment, the suspension stays through it too. With every session stopped
   (Check, step 4) the scaler holds the pool at zero, suspended or not, so the deployment has no
   instance of the pool to replace and needs no launch. Merge the fix, or start its execution, and
   follow that execution on the pipeline's page in the CodePipeline console. The cause is fixed
   once every deployment action of the stage has succeeded in it: from then on the pool's group
   launches the corrected release. Go to step 2 then, and not before.

   Do not wait longer either. The execution's verification follows its deployment: it books the
   pools it tests and waits `check_wait_minutes` (45 by default) for an instance of each, and an
   instance of a suspended pool does not come. A verification whose wait for the pool ended before
   the suspension was cleared has not tested the fix on that pool, and may have failed for it: read
   the execution with the runbook [bad deployment](bad-deployment.md), and take Verify, below, as
   the test of the fix.

2. **Clear the suspension,** once the cause is fixed. The command sets the mark back to false and
   writes who cleared it and when; it is refused for a pool whose launches are not suspended.

   ```sh
   rhi_admin pool clear-suspension coursework
   ```

   The scaler resumes the group's launches at its next run, within a minute. Ask for one now:

   ```sh
   rhi_admin pool run
   ```

   It prints one line for each pool: the pool, the instances its group is asked for, and the reason
   of the run's decision. For the pool just cleared, while a session is active on it:

   - `provision`: the session asks for more than the group had been asked for, and the run now asks
     AWS for the session's number;
   - `unchanged`: the group had been asked for the session's number before the suspension. The run
     changes no number, and resumes the group's launches all the same;
   - `launch-suspended`: the run still found the mark set. Read `rhi_admin pool status` again.

   `release` and `rebalance` are the run's other work on the pool, and say nothing of the
   suspension: Verify, step 1, does. With no session active on the pool, its line reads zero instances, and the launches wait for a
   booking (step 3). When the command answers that the scaler is already running, the launches
   resume at the next run, within a minute.

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

3. Students see the pool `ready` once an instance is in service, and `warming` before that, from
   the moment one is starting:

   ```sh
   rhi status
   ```

Walked through on the deployed system: not yet.
