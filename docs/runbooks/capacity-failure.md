# A session short of capacity

AWS does not always have a GPU instance to give. A session is **short** while fewer instances are
in service than it asks for: the pool waits for capacity, or for a launch that has not finished
(system design, section 5.4). Over one recorded day the coursework pool asked for an instance nine
times: three times it had one at once, and six times it waited between 21 and 37 minutes.

**An instance asked for stays asked for.** The scaler sets the pool's group to the session's number
of instances and leaves it there. Auto Scaling launches what AWS has capacity for and makes the
request again for the rest, by itself, until it arrives. Nothing gives up: not after an hour, and
not when the class has begun. So the first act is to wait, and nothing here asks again, since
nothing ever stopped asking. A request never starts an instance, and neither does a student's
command: what a pool waits for is AWS, and the session is what asks.

There is no fallback and no other endpoint. If no instance has arrived when the class starts,
students work with their own subscriptions to hosted models, and nothing here stands in for a pool
(section 5.4; section 15, decision 12).

Like every check against the endpoint, this runbook assumes the public HTTPS listener is on, which
it is not by default.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. A step that needs the account is the `aws`
command you run with your own credentials, in the region of the endpoint; it is tested only by
walking through the runbook. A name the account gives is a placeholder: `<group>` is the Auto
Scaling group of the pool, `<pipeline>` the pipeline, and each step says which command prints it.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

`rhi` is the students' CLI, logged in with your own roster entry. `coursework` below stands for the
pool that is short, by its name in the configuration, which is also the model name `rhi status`
prints.

## When

A session is active and has fewer instances in service than it asks for:

- **The alarm:** an e-mail from the alerts topic for the alarm of the pool that says its session
  has had fewer instances in service than it asks for 30 minutes, the configuration's
  `capacity_alarm_minutes`. It is sent once, when the alarm fires. Nothing is sent when it clears,
  and a session filled within the half hour sends nothing at all.
- **What students see,** when no instance of the pool is in service (section 5.3):

  | `rhi status` | The pool | A request |
  | --- | --- | --- |
  | `warming` | An instance is starting, and none is in service | Answered at once with `warming`; harnesses retry a few times |
  | `unavailable`, `capacity_unavailable` | No instance is in service or starting: the pool waits for capacity | Answered at once with `capacity_unavailable`; harnesses retry a few times |

  The gateway keeps no request waiting. `rhi wait` waits through both and prints each change. It
  returns when the pool reads `ready`, and gives up after the minutes of its `--timeout`, 20 unless
  given, which a wait for capacity outlasts (Act, step 6).
- **A session filled in part** reads `ready`, since one instance in service is enough to serve:
  students see nothing but a slower pool. The alarm and `rhi_admin session status` are what say
  that it is short.

`no_session` is not this runbook: the pool has no active session, and the runbook
[a session booked, changed and stopped](sessions.md) books one.

## Check

1. **What the session has: five numbers and a cause.** One line for each deployed pool:

   ```sh
   rhi_admin session status
   ```

   | Column | Reads |
   | --- | --- |
   | `requested` | What the session asks for now: its instances while it is active, zero otherwise |
   | `asked` | The instances the scaler has asked of AWS: the desired capacity of the pool's group |
   | `waiting`, `waiting_since` | Asked of AWS with no instance yet, and since when |
   | `starting` | Launched, not yet in service |
   | `in_service` | Healthy behind the load balancer: these serve students |
   | `cause` | While instances are waited for, why the group's last launch failed, if one did |

   The scaler writes them at every run, so they are at most a minute old. The session is short
   while `in_service` is below `requested`. The cause is one of four words, never AWS's own text:
   `insufficient-capacity`, `quota`, `launches-suspended`, or `launch-failed` for anything else.
   It is shown only while `waiting` is above zero, and reads `-` when no launch has failed.

   Numbers that do not follow a command after a minute are not this runbook: the scaler does not
   run, and it is the runbook [a scaler that does not run](scaler-not-running.md).

2. **Which case it is.**

   | What the line reads | What it is | Act |
   | --- | --- | --- |
   | `starting` above zero, `waiting` zero | The instances are launched and start, in an estimated 3 to 13 minutes each (section 5.2) | Step 1: wait |
   | `waiting` above zero, the cause `-` or `insufficient-capacity` | AWS has no instance for the pool yet | Step 1: wait |
   | The cause `quota` | The account's quota refuses the launches | Step 2 |
   | The cause `launches-suspended`, or `requested` above `asked` | The scaler has suspended the pool's launches after three abandoned launches | Step 3 |
   | The cause `launch-failed` | A launch failed for another reason | Step 4 |

3. **Since when, and how long before the class.** `waiting_since` is when the pool began to wait,
   and `starts_at` when the session began to ask. The dashboard "Fleet and scaling" in Grafana
   shows the five numbers over time, in the pool's row, with how long the pool has waited.

## Act

1. **Wait.** With no cause, or with `insufficient-capacity`, there is nothing to repair and
   nothing to ask again: the group keeps the request standing, and each instance launches when AWS
   has capacity for it. Read the status again every few minutes.

   ```sh
   rhi_admin session status
   ```

   What not to do meanwhile:

   - **Do not stop the session to book it again.** A stop gives back the instances that have
     arrived, and getting them again may take as long as the first time. A new booking asks for
     nothing the standing one does not already ask for.
   - **Do not lower the session to stop the alarm,** unless the class needs fewer instances. A
     lower number no longer asks for the instances that have not arrived (section 5.1).
   - **Do not change the pool's group by hand.** The scaler sets it back at its next run.

   An instance that reads `starting` for longer than its start takes is not yours to end either:
   its launch hook times out, after 15 minutes until a launch has been measured, the group
   abandons it and launches another. Three such launches in a row suspend the pool's launches
   (step 3).

2. **The cause `quota`.** A launch the quota refuses is waited for like any other, but it does not
   arrive by waiting: it arrives when the quota is raised, or when an instance of the same family
   is given back. Each instance family has its own quota, counted in vCPUs. Read both, in the
   region of the endpoint:

   ```sh
   aws service-quotas list-service-quotas --service-code ec2 --query "Quotas[?QuotaName=='Running On-Demand G and VT instances' || QuotaName=='Running On-Demand P instances'].[QuotaName,Value]" --output text
   ```

   - **Now:** if another pool of the same family holds instances the class does not need, lower
     its session or stop it, here a pool named `upgrade`:

     ```sh
     rhi_admin session set --pool upgrade --instances 1
     rhi_admin session stop --pool upgrade
     ```

     One of the two. The instances it gives back leave within about eleven minutes, and the
     launches the quota refused then pass.
   - **For the next class:** request the increase, which can take days. The quota must cover one
     instance more than each pool's `max_instances` (section 5.4). The runbook
     [a session booked, changed and stopped](sessions.md), Check, step 1, computes it; for a
     coursework pool of `g7e.2xlarge` alone, with the pool's `max_instances` as `N`:

     ```sh
     setup/owner-setup.sh --step quotas --max-instances N
     ```

   Meanwhile the class runs on what the quota allows (step 5).

3. **The cause `launches-suspended`, or `requested` above `asked`.** Whether the scaler has
   suspended the pool's launches:

   ```sh
   rhi_admin pool status
   ```

   `true` under `launch_suspended` is a launch that fails every time, not missing capacity: the
   group launches nothing until the cause is fixed and the suspension cleared, and waiting does
   not help. It is the runbook [a pool whose launches are suspended](suspended-launches.md).

4. **The cause `launch-failed`.** A launch failed for a reason that is neither capacity nor the
   quota. Two kinds:

   - **An instance that started and never entered service.** Its launch was abandoned, and the
     group launches another. If the next one enters service, there is nothing to do. If it goes
     on, the third suspends the pool's launches, and it is step 3.
   - **A launch AWS refused,** for instance in a zone of the pool that does not offer the pool's
     instance type. Read why in the group's own activities. The group of a pool carries the tag
     `rhi-pool` with the pool's name; this prints its name, `<group>` below:

     ```sh
     aws autoscaling describe-auto-scaling-groups --filters Name=tag:rhi-pool,Values=coursework \
         --query 'AutoScalingGroups[].AutoScalingGroupName' --output text
     ```

     Its last activities, the newest first, each with how it ended and why:

     ```sh
     aws autoscaling describe-scaling-activities --auto-scaling-group-name <group> \
         --max-items 10 --output json \
         --query 'Activities[].{what:Description,status:StatusCode,why:StatusMessage,started:StartTime,ended:EndTime}'
     ```

     `why` is AWS's own text: read it in your terminal, and paste it nowhere. A refusal that names
     a zone or an instance type is a fault of the pool's configuration, its `zones` or its
     `instance_types`: it is repaired after the class (step 7), and the launches Auto Scaling
     places in the pool's other zones still arrive.

5. **When the class starts, start it on the instances that have arrived.** With `in_service` at
   one or more, the pool reads `ready` and serves every student: tell the class to begin. The
   others join as they come, and the scaler then moves a few students at a time to each new
   instance (section 5.5). Until then the instances in service carry the whole class, so answers
   are slower. Leave the session as it is: it keeps asking for the rest.

   ```sh
   rhi status
   ```

6. **When none has arrived, students use their own subscriptions** to hosted models, for now.
   Nothing in this repository stands in for a pool: there is no fallback command, and no other
   model is switched in. Leave the session booked while the class may still use the pool: an
   instance can arrive at any minute, and a student who wants to know when waits for it, which
   starts nothing.

   Tell them how long to wait. `rhi wait` gives up after 20 minutes unless it is given another
   time: it then prints that the pool is not ready, and exits 1. A wait for capacity lasts longer
   than that, 21 to 37 minutes on the recorded day and with no upper bound, so that end says
   nothing new about the pool: the session still asks, and the instance may still come. Give the
   students a time that covers what is left of the class, in minutes, here two hours:

   ```sh
   rhi wait coursework --timeout 120
   ```

   A student whose wait has given up runs it again. The command ends at once, with `no_session`,
   when the session is stopped or over: no instance is coming then, and waiting again does not
   help.

   Once the class no longer wants the pool, stop its session, so that an instance that arrives
   late is not paid for until the session's end. Name the pool: without `--pool` the command acts
   on the configuration's default pool, which need not be the one that is short.

   ```sh
   rhi_admin session stop --pool coursework
   ```

7. **After the class, if capacity failed often.**

   - **Book earlier.** The start of a session is when its instances are asked for, and an instance
     in service holds its GPU until the session ends: hours ahead is the practice (section 5.6).
   - **Give the pool more places to look** (section 5.4): the zones that offer its instance types,
     in the pool's `zones`, and fallback instance types, which Auto Scaling tries in the order
     listed, in the pool's `instance_types`, each with its price in `prices` and its name in
     `allowed_instance_types`. A fallback type costs more. The quota must still cover one instance
     more than the pool's `max_instances`, of the largest type it lists. Edit your configuration
     file, kept outside the working tree, then validate it and write it to SSM:

     ```sh
     setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
     ```

     An edit of SSM changes no commit, so nothing deploys it by itself. With no session active,
     start an execution of the pipeline; the first command prints the pipeline's name,
     `<pipeline>`, and the second the ID of the execution it started:

     ```sh
     aws codepipeline list-pipelines --query 'pipelines[].name' --output text
     aws codepipeline start-pipeline-execution --name <pipeline> --query pipelineExecutionId --output text
     ```

     Check that execution, by its ID, as the runbook
     [a session booked, changed and stopped](sessions.md) says in its Check, step 2. More zones and
     more types make a wait rarer and shorter. They do not remove it.

## Verify

1. **The session is filled:** `in_service` reads the session's `instances`, `waiting` and
   `starting` read zero, and no cause is shown.

   ```sh
   rhi_admin session status
   ```

2. **Students see the pool `ready`,** from the first instance in service on:

   ```sh
   rhi status
   ```

   Tell the students who switched to their own subscriptions that they can come back to the
   endpoint; their harness needs no change.

3. **The alarm clears by itself,** and sends nothing when it does: when the session is filled,
   when it ends, or when it is lowered to what is in service. Its state, `OK` once it has cleared:

   ```sh
   aws cloudwatch describe-alarms-for-metric --namespace rhi/scaler --metric-name InstancesMissing \
       --dimensions Name=Pool,Value=coursework --query 'MetricAlarms[].StateValue' --output text
   ```

4. **No pool's launches are suspended:** `launch_suspended` reads `false` for the pool.

   ```sh
   rhi_admin pool status
   ```

Walked through on the deployed system: not yet.
