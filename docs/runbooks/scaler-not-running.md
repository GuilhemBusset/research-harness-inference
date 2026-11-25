# A scaler that does not run

One function, the scaler, is the only writer of a pool's size (system design, section 5.1). It runs
every minute, and once more when the admin CLI has written a session. Each run sets every pool's
group to what its session asks for, and to zero when it has none.

**A scaler that does not run leaves every pool as it is.** Nothing else changes a group: it has no
scaling policy and no scheduled action, there is no nightly stop, and its instances are protected
from scale-in until the scaler lifts that protection. So:

- **a session's instances stay past its end.** After the end, or after `session stop`, the gateway
  answers `no_session` within seconds, since it reads the sessions by itself. The instances stay in
  service all the same, serve nobody and are billed, until the scaler runs again or the group is
  emptied by hand (Act, step 2);
- **a session that starts gets no instance,** and one that is raised gets no more: nothing asks AWS
  for them;
- **an active session keeps what it has.** The instances in service go on serving, and the group
  goes on asking AWS for the instances it was already asked for.

What stays outside the scaler is what shows the fault: the alarm on the scaler itself, which is the
one to act on first.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A step that
needs the account is the `aws` command you run with your own credentials, in the region of the
endpoint; it is tested only by walking through the runbook. A name or an ID the account gives is a
placeholder, and the step says which command prints it: `<group>` is the Auto Scaling group of a
pool, `<instance>` the ID of one of its instances, and `<scaler-log-group>` the scaler's log group.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

`coursework` below stands for a pool, by its name in the configuration.

## When

- **The alarm on the scaler:** an e-mail from the alerts topic for the alarm that says the scaler
  has completed no run for 5 minutes. The scaler publishes one data point after each completed
  run, and the alarm fires when five minutes in a row have none. It is sent once, and nothing is
  sent when it clears.
- **A command of the admin CLI says so.** `session start`, `session set`, `session stop` and
  `pool run` ask the scaler for a run, and fail with a line that says the scaler's run failed and
  names its log group. A command `session` has written its change before it asks: the session is
  booked, changed or stopped, and only the scaler's part is missing.
- **The numbers of `rhi_admin session status` do not follow a command,** a start or an end: the
  columns from `requested` on still read what the last completed run wrote, minutes later.
- **Not the alarm on a pool in service outside a session.** That alarm reads what the scaler
  publishes, so it keeps its state while the scaler publishes nothing: instances that stay past
  their session's end do not raise it. Do not wait for it.

A parked endpoint has no scaler, no group and none of these alarms (runbook
[park and unpark](park-and-unpark.md)): `pool run` is then refused because no pool is deployed,
and this runbook does not apply.

## Check

1. **Whether the scaler runs now.** Ask for one run:

   ```sh
   rhi_admin pool run
   ```

   - One line for each pool, with the instances its group is asked for and the reason of the
     decision: this run completed. The fault may be over, or the scaler may not be started every
     minute any more: step 2 says which.
   - The scaler is already running: a run is under way. Ask again a minute later.
   - The scaler's run failed: it runs and fails. The line names its log group, which is
     `<scaler-log-group>` in step 3.

2. **Since when it has completed no run.** The alarm's state, and when it last changed:

   ```sh
   aws cloudwatch describe-alarms-for-metric --namespace rhi/scaler --metric-name RunCompleted \
       --query 'MetricAlarms[].[StateValue,StateUpdatedTimestamp]' --output text
   ```

   `ALARM` since a time: no run has completed since about five minutes before it. The completed
   runs of the last half hour, one data point a minute with the number of runs that completed in
   it, and no line for a minute without one:

   ```sh
   aws cloudwatch get-metric-statistics --namespace rhi/scaler --metric-name RunCompleted \
       --statistics Sum --period 60 \
       --start-time "$(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
       --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
       --query 'sort_by(Datapoints, &Timestamp)[].[Timestamp,Sum]' --output text
   ```

   A run that step 1 completed, with no other minute in the list, is a scaler that works when it
   is asked and is no longer started by its schedule.

3. **Why it fails.** The scaler's log group holds what each run logged. Its name is the one the
   failed command of step 1 printed, and `LOG_GROUP_NAME` in `src/rhi/pools/scaler.py` fixes it.
   The last half hour:

   ```sh
   aws logs tail <scaler-log-group> --since 30m
   ```

   A run that completed logs one record for each pool, `"event": "decision"`, with the pool, the
   reason, and the capacity before and after. A run that failed logs its error, and no decision
   when it failed before deciding. The error is the function's own text or AWS's: read it in your
   terminal, and paste it nowhere. What it points to:

   | The log shows | The cause | Where it is fixed |
   | --- | --- | --- |
   | The settings refused: a setting missing, or a pool's entry that does not parse | A deployment gave the function settings it cannot read | The runbook [a bad deployment](bad-deployment.md) |
   | A call to AWS refused for lack of permission | A deployment changed the function's role | The runbook [a bad deployment](bad-deployment.md) |
   | A record of a pool that does not read, with the field it names | An item of the pool-state table that neither the admin CLI nor the scaler wrote so | The item the error names, in the stage's pool-state table: the runbook [a pool whose launches are suspended](suspended-launches.md), Check, step 3, finds the table |
   | `"event": "cloudwatch-not-published"` after the decisions | The run applied its decision and could not publish what the alarms read: the pools are right, and the alarm is not | AWS's fault if it passes; a deployment's if it began with one |
   | A call to AWS that timed out or was throttled, records that were not all read, or a run cut at its time limit | AWS's fault, which passes | Act, step 1: try again |
   | Nothing at all for the minutes of the alarm | The function is not started | The runbook [a bad deployment](bad-deployment.md), if a deployment preceded it |

4. **What changed last.** A deployment that ended just before the alarm's time is the first
   suspect. Each merge commit carries its `deploy/<stage>` status, and the runbook
   [a bad deployment](bad-deployment.md), Check, step 1, reads them.

5. **What each pool holds, and what should hold it.** The sessions, as the records read now, which
   is right whatever the scaler does:

   ```sh
   rhi_admin session status
   ```

   Read the columns `session`, `instances` and `ends_at` only: those from `requested` on are the
   scaler's, and stale. Then each pool's group as AWS has it. The group of a pool carries the tag
   `rhi-pool` with the pool's name; this prints its name, `<group>` below, and the capacity it is
   asked for:

   ```sh
   aws autoscaling describe-auto-scaling-groups --filters Name=tag:rhi-pool,Values=coursework \
       --query 'AutoScalingGroups[].[AutoScalingGroupName,DesiredCapacity]' --output text
   ```

   and its instances, each with its state and whether it is protected from scale-in:

   ```sh
   aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <group> \
       --query 'AutoScalingGroups[].Instances[].[InstanceId,LifecycleState,ProtectedFromScaleIn]' --output text
   ```

   | The pool's session | Its group | What to do |
   | --- | --- | --- |
   | `none`, or `booked` and not started | A capacity above zero, or an instance listed | Empty the group by hand (Act, step 2): it is billed and serves nobody |
   | `none`, or `booked` and not started | Zero, and no instance | Nothing |
   | `active` | The session's number | Nothing now: it serves. Its end will not release it while the scaler does not run (Act, step 3) |
   | `active` | Fewer than the session's number | The class runs on what is in service, or on the students' own subscriptions when nothing is: the runbook [a session short of capacity](capacity-failure.md), Act, steps 5 and 6 |

## Act

1. **Try a run again,** when the cause of Check, step 3, is one that passes:

   ```sh
   rhi_admin pool run
   ```

   A run that completes sets every pool to what its session asks for: go to Verify. Otherwise go
   on. The repair itself is the runbook the cause pointed to, and it can take long: steps 2 and 3
   stop the money first.

2. **Empty by hand the group of a pool that has no active session.** Do it in this order, for each
   such pool that Check, step 5, found with instances.

   First make sure nothing asks for the pool, so that the scaler does not raise it again at its
   first run. The command writes the stop, then asks the scaler for a run: while the scaler
   fails, it ends on that failure, which is expected here, and the stop is written all the same.
   A pool with nothing to stop says so:

   ```sh
   rhi_admin session stop --pool coursework
   ```

   Then lift the scale-in protection of the group's instances, every one that Check, step 5,
   listed as protected. While an instance is protected, the group does not terminate it, whatever
   capacity it is asked for:

   ```sh
   aws autoscaling set-instance-protection --auto-scaling-group-name <group> \
       --instance-ids <instance> <instance> --no-protected-from-scale-in
   ```

   Then set the group's desired capacity to zero:

   ```sh
   aws autoscaling set-desired-capacity --auto-scaling-group-name <group> --desired-capacity 0
   ```

   The group stops asking AWS for what had not arrived, and takes its instances out of service.
   Each one drains: the load balancer sends it nothing more, a request it still runs has the
   pool's `drain_seconds` (300 by default) to finish, and the instance is gone within about eleven
   minutes (section 5.1).

   An instance that was still starting had no protection to lift. If the group keeps it, it enters
   service protected: read the group's instances again a few minutes later (Check, step 5), and
   lift the protection of any instance still listed. The capacity is already zero, so the group
   then takes it out.

3. **A pool whose session is active.** It serves, and nothing needs doing until the class is over.
   Then stop the session, which the gateway applies within seconds, and empty the pool's group by
   hand as in step 2. Do not empty the group of a session the class is using.

   A session cannot be booked or raised while the scaler does not run: the record is written and
   no instance is asked for. If a class is about to start on a pool that is empty, students work
   with their own subscriptions to hosted models, and nothing here stands in for a pool.

4. **Repair the scaler,** with the runbook that Check, step 3, pointed to. A repair that is a
   deployment waits while a session is active on any pool (section 5.6): stop every session first,
   after its class, and empty its group (step 3).

## Verify

1. **A run completes,** and prints one line for each pool:

   ```sh
   rhi_admin pool run
   ```

2. **The scaler runs by itself again.** The alarm reads `OK`, a minute or two after the first
   completed run, and the list of completed runs has a line for each minute since (Check, step 2).
   Nothing is sent when the alarm clears.

   ```sh
   aws cloudwatch describe-alarms-for-metric --namespace rhi/scaler --metric-name RunCompleted \
       --query 'MetricAlarms[].[StateValue,StateUpdatedTimestamp]' --output text
   ```

3. **Every pool is what its session asks for.** `requested` and `asked` read the session's
   instances for a pool whose session is active, and zero for every other; they follow a command
   again.

   ```sh
   rhi_admin session status
   ```

   The scaler's first run sets each group to that, whatever was done by hand: a group emptied in
   Act, step 2, stays at zero because its session was stopped, and a pool booked meanwhile gets its
   instances asked for now.

4. **A group emptied by hand lists no instance,** about eleven minutes after its capacity was set
   to zero. The command prints nothing:

   ```sh
   aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <group> \
       --query 'AutoScalingGroups[].Instances[].[InstanceId,LifecycleState]' --output text
   ```

5. **No pool's launches are suspended,** as before the fault:

   ```sh
   rhi_admin pool status
   ```

Walked through on the deployed system: not yet.
