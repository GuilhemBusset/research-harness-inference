# A session booked, changed and stopped

The endpoint serves students during sessions only, and the instructor books each one: there is no
schedule in the configuration, and nothing starts by itself (system design, section 5.6). A
**session** is a booking of one pool: a number of instances, a start and an end, in one record that
the scaler and the gateway read. It is the one operating state. There is no switch beside it and no
mode per pool: a pool that should not run is a pool nobody books, and the kill switch is
`session stop`.

A request never starts an instance, and neither does a student's command. The scaler keeps a pool
at its session's number of instances from the session's start to its end, and at zero otherwise
(section 5.1). AWS may take half an hour or more to give a GPU instance (section 5.4), so the
practice is to book hours ahead of the class: the start of a session is when its instances are
asked for, not when the class begins, and the hours before the class are paid for (section 11.1).

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
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

`rhi` is the students' CLI. Log in once with your own roster entry; the endpoint's address, here
the placeholder `https://inference.example.com`, is given at the first login only:

```sh
rhi login --endpoint https://inference.example.com
```

A pool is named by its name in the configuration, which is also the model name `rhi status` prints.
`session start`, `session set` and `session stop` take `--pool`, and act on the configuration's
`default_pool` without it: `coursework` in the example, and below. `session status` takes no pool:
it lists every deployed pool. A time is `HH:MM`, the next time the clock of the configuration's
`time_zone` shows it, or a date and time, `YYYY-MM-DDTHH:MM`, in that zone. A length is a whole
number followed by `m` or `h`, as `90m` or `6h`. The times and the numbers below are examples, for
the example's coursework pool, whose `max_instances` is 2.

## When

- **Ahead of a class:** the day before, for the checks; then, hours before the class, the booking
  (Act, step 1), and the reading of what arrives (Act, step 2).
- **During a session:** the class needs more instances or fewer, or ends later or sooner (Act,
  step 3).
- **When the class is over,** or when a pool must stop now, whatever it serves: the stop (Act,
  step 4). The end gives the GPUs back, and getting them again may take as long as the first time.

## Check

Do these the day before the class.

1. **The GPU quota covers one instance more than each pool's `max_instances`** (section 5.4): an
   instance that drains and one that launches can overlap. The quota counts vCPUs, so a pool needs
   (`max_instances` + 1) times the vCPUs of one instance of the largest type its `instance_types`
   lists, since a fallback type may be the one launched. For the example's coursework pool, whose
   only type is `g7e.2xlarge` with 8 vCPUs, that is 8 × (`max_instances` + 1): 24 for the example's
   2, 32 for 3. Each instance family has its own quota: a `g…` type counts against "Running
   On-Demand G and VT instances", a `p…` type against "Running On-Demand P instances". Add the needs
   of every pool booked together whose types are of the same family, and compare each family's
   total with that family's quota. Read both quotas in the region of the endpoint:

   ```sh
   aws service-quotas list-service-quotas --service-code ec2 --query "Quotas[?QuotaName=='Running On-Demand G and VT instances' || QuotaName=='Running On-Demand P instances'].[QuotaName,Value]" --output text
   ```

   If it is short, request more, well ahead of the class, since an increase can take days. The
   quota step of the owner-setup script computes the request for a coursework pool of
   `g7e.2xlarge` alone, the example's: use the pool's `max_instances` as `N`, or the new maximum if
   step 2 raises it; the script adds the one instance more.

   ```sh
   setup/owner-setup.sh --step quotas --max-instances N
   ```

   If your pools list another type, or several pools of one family are booked together, the
   script's figure is not yours: request the total you computed above instead, in the Service
   Quotas console, under Amazon EC2, for the quota of that family. A launch the quota refuses is
   waited for like any other, and `session status` then reads the cause `quota` (Act, step 2).

2. **A pool's maximum, if the class needs more instances.** A session books from 1 to the pool's
   `max_instances`, as the running scaler holds it, and `session start` and `session set` refuse
   more. To raise it,
   edit `max_instances` of the pool in your configuration file, kept outside the working tree, then
   validate and write it to SSM:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   The quota of step 1 must cover the new maximum plus one. The new value takes effect only once a
   pipeline run has deployed it. Start a run with **Release change** on the pipeline's page in the
   CodePipeline console, and note the execution ID the console shows for it. Check that execution
   itself, by its ID, from here on: an edit of SSM changes no commit, so a `deploy/<stage>` or a
   `verify/<stage>` status on the latest commit can be an earlier run's, and only this execution
   says what happened to the edit. The pipeline runs in superseded mode: a newer merge can
   supersede an execution waiting between stages, the one you started with **Release change**
   included, for instance while it waits in front of the stage `Rhi` for a session to end. That
   execution then ends `Superseded` in the pipeline's execution history, and the newer one, which
   synthesizes from SSM again, deploys its own commit and the configuration SSM holds, your edit
   included: from then on, check the newer execution by its ID instead. The new value is deployed
   once every deployment action of the stage has succeeded in the execution you check, and it is
   verified once the stage's action Verify has succeeded too and its log shows that the smoke
   tests ran: Verify also succeeds when it has nothing to test, the endpoint having no HTTPS
   listener or the canary no key yet, and its log then has the line `verify: no test was run:`
   with the reason. An execution that failed, or was stopped, is the runbook
   [bad deployment](bad-deployment.md).

3. **Every change has landed, and no deployment is under way.** A deployment waits while a session
   is active on any pool (section 5.6), so that a release changes nothing under a class. The wait
   is a check before each stage's deployment, not a pause of one: a deployment that has passed it
   runs to its end, also when a session starts meanwhile. Such a deployment replaces no instance
   (section 4.3): an instance that starts while it runs may read the release of before or the new
   one, and keeps what it started with; one of the gateway drains its tasks. A session that is
   booked and has not started holds nothing. So, before a session starts, no execution is in
   progress on the pipeline's page in the CodePipeline console; if one is, wait for it to end. And
   what is merged or edited while a session is active is deployed only after its end: the
   execution in the hold deploys its own commit, then the newest merge's execution deploys its
   commit and the configuration SSM holds. Each newer merge supersedes the one that waited in
   front of the stage `Rhi` before it, so the merges in between get no deployment of their own:
   their commits take the verdict of the newest one's execution.

4. **What is booked already.** A pool has one session at a time, and `session start` is refused
   while one is booked or active: `set_by` says who booked it, an admin or one of the pipeline's
   checks, which book a pool for the minutes they need and stop their own session.

   ```sh
   rhi_admin session status
   ```

## Act

1. **Book the pool,** hours ahead of the class: a number of instances, from now, to an end given as
   a time or as a length.

   ```sh
   rhi_admin session start 2 --until 18:00
   rhi_admin session start 2 --for 6h
   ```

   One of the two, not both: the second is refused while the first is booked. To book now a session
   that starts later, give its start. The instances are asked for at the start, so the start is
   still hours before the class:

   ```sh
   rhi_admin session start 2 --from 07:00 --until 13:00
   ```

   Another pool is named:

   ```sh
   rhi_admin session start 1 --pool mock --for 90m
   ```

   The command prints the session it wrote: the instances, the start and the end in the configured
   zone, and your identity. It then asks the scaler for a run, so the booking applies within
   seconds; when a run is already under way it says so, and the next run is within a minute.

   Every session has an end, at most `session_max_hours` (12 by default) after the later of its
   start and the moment of the command, so a forgotten session ends by itself. Book an end with
   margin over the class: at the end the instances leave. A booking outside the bounds is refused
   with its reason, and nothing is written.

2. **Read what arrives.** The scaler asks AWS for all the instances at once, from the session's
   start; each is launched when AWS has capacity for it, and starts in an estimated 3 to 13 minutes
   (section 5.2).

   ```sh
   rhi_admin session status
   ```

   One line for each deployed pool:

   | Column | Reads |
   | --- | --- |
   | `session` | `none`, `booked` (its start has not come) or `active` |
   | `instances`, `starts_at`, `ends_at` | The booking, its times in the configured zone |
   | `set_by`, `set_at` | Who last wrote it, and when |
   | `requested` | What the session asks for now: its instances while it is active, zero otherwise |
   | `asked` | The instances the scaler has asked of AWS |
   | `waiting`, `waiting_since` | Asked of AWS with no instance yet, and since when |
   | `starting` | Launched, not yet in service |
   | `in_service` | Healthy behind the load balancer: these serve students |
   | `cause` | While instances are waited for, why the last launch failed, if one did |

   The columns up to `set_at` are the session's record, read as the command runs. Those from
   `requested` on are what the scaler wrote at its last run: it runs every minute, and once more
   when a command `session` asks it to, so they follow a command at once when the command said that
   the scaler has run, and within a minute when it said that a run was already under way.

   The session is filled when `in_service` reads the session's `instances`. While it is not:

   - `waiting` above zero, with no cause or the cause `insufficient-capacity`: AWS has no instance
     yet. Nothing gives up, and Auto Scaling keeps asking by itself: wait. If the class starts
     before the instances, it is the runbook [a session short of capacity](capacity-failure.md);
   - the cause `quota`: the account's quota refuses the launches (Check, step 1);
   - the cause `launches-suspended`, or `requested` above `asked`: the scaler has suspended the
     pool's launches, the runbook [a pool whose launches are suspended](suspended-launches.md);
   - the cause `launch-failed`: a launch failed for another reason. If it goes on, three launches
     abandoned in a row suspend the pool's launches, and it is the same runbook.

   The dashboard "Fleet and scaling" in Grafana shows the same numbers over time, in the pool's
   row.

3. **Change the session.** `session set` changes what it is given and keeps the rest. The
   instances are any number from 1 to the pool's `max_instances`: fewer than the session has, here
   one of the two booked in step 1, or more, here the two again.

   ```sh
   rhi_admin session set --instances 1
   rhi_admin session set --instances 2
   ```

   Fewer: what leaves is, in this order, capacity that has not arrived, an instance still starting,
   an instance in service that has nothing running and nothing waiting, then the one with the
   fewest requests, which drains (section 5.1). More: they are asked of AWS at once and arrive as
   the first ones did, which may take as long. A number above the pool's `max_instances` is
   refused, and the session stays as it was (Check, step 2, raises the maximum).

   A later end, or a sooner one, as a time or as a length counted from the later of the session's
   start and now:

   ```sh
   rhi_admin session set --until 19:00
   rhi_admin session set --for 90m
   ```

   The end is again at most `session_max_hours` after the later of the start and the moment of the
   command: a session is kept longer than that by moving its end again, later.

   A start that has not passed can be moved; one that has passed cannot:

   ```sh
   rhi_admin session set --from 08:00
   ```

   The command writes only if the session still reads what it read: when another admin changed it
   meanwhile, it reads the session again and applies your change to that one.

4. **Stop the session** when the class is over, or at once when the pool must stop. This is the
   kill switch: one pool's session, or every pool's.

   ```sh
   rhi_admin session stop
   rhi_admin session stop --all
   ```

   The gateway refuses new requests of the pool within seconds, requests already running have the
   pool's `drain_seconds` (300 by default) to finish, and the instances leave (section 5.1). A pool
   with no session booked or active has nothing to stop, which the command says, and that is no
   failure. Booking the pool again asks AWS for instances anew.

## Verify

1. **The session reads as you wrote it,** after each command: `booked` or `active` with its
   instances, its start and its end after a booking or a change, and `none` after a stop. That
   much is the record, and reads so at once.

   ```sh
   rhi_admin session status
   ```

   `requested` and `asked` follow once the scaler has run on the record (Act, step 2), and are two
   numbers. `requested` is what the session asks for: its instances while it is active, zero before
   its start and after a stop. `asked` is what the scaler has asked of AWS for the pool, and the
   run sets it to `requested`, with one exception: a pool whose launches are suspended is not
   raised. Its `asked` goes down with the session and never up, so a session that asks for more
   than the pool's group is asked for leaves `asked` where the run found it, below `requested`.
   Where that is depends on what the group was asked for just before: zero when the pool had no
   session and the scaler had already lowered its group, and the number of the earlier session
   when the pool is booked again within the minute after that session ended, before a run has
   lowered the group, or when a session under way is raised. No later run raises it. It is the
   runbook [a pool whose launches are suspended](suspended-launches.md).

   When the command said that a run was already under way, both may still read the numbers of
   before: read the status again a minute later. Numbers that still do not follow, or a command
   that ends by saying that the scaler's run failed, are the runbook
   [a scaler that does not run](scaler-not-running.md).

2. **What students see at each moment.** The gateway reads the sessions every 5 seconds, so a
   command shows here within seconds. `rhi status` lists each pool with a state and, when it is
   unavailable, a cause; while the pool's session is active it also gives the time the session
   ends (section 5.3).

   ```sh
   rhi status
   ```

   | Moment | `rhi status` | A request |
   | --- | --- | --- |
   | No session, or one booked whose start has not come | `unavailable`, `no_session` | Answered at once with `no_session` |
   | The session is active, and no instance is in service or starting: the pool waits for capacity | `unavailable`, `capacity_unavailable` | Answered at once with `capacity_unavailable`; harnesses retry a few times |
   | The session is active, an instance is starting and none is in service | `warming` | Answered at once with `warming`; harnesses retry a few times |
   | The session is active and an instance is in service | `ready` | Served, also while the session's other instances are still to come |
   | The session has ended or was stopped | `unavailable`, `no_session` | A new one is answered `no_session`; one already running has `drain_seconds` to finish, and is cut after them |

   A student who arrives before the instances waits for them with `rhi wait`, which starts nothing:
   it reads the status every 10 seconds, prints each change, returns when the pool is `ready`, and
   stops with the cause on `no_session`.

   ```sh
   rhi wait coursework
   ```

   When the class starts with some of its instances, it starts on those: the others join as they
   come, and the scaler moves a few students at a time to a new instance (section 5.5). When it
   starts with none, students work with their own subscriptions to hosted models; nothing here
   stands in for a pool.

3. **After a stop, or after the end,** the scaler's next run, within a minute, asks AWS for
   none: `session status` then reads zero under `requested` and `asked` for the pool, and zero
   under `in_service` once its instances have left the load balancer.

   ```sh
   rhi_admin session status
   ```

   That line does not count an instance that drains: one that still has requests running leaves
   once they have finished, and the instances are gone within about eleven minutes of the run that
   released them (section 5.1). The pool's group, which is named `rhi-pool-` followed by the pool's
   name, then lists no instance, and the command prints nothing:

   ```sh
   aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names rhi-pool-coursework \
       --query 'AutoScalingGroups[].Instances[].[InstanceId,LifecycleState]' --output text
   ```

   A group that still lists an instance after twenty minutes, with none in service, raises the
   alarm on instances running with nothing in service (section 5.1).

Walked through on the deployed system: not yet.
