# Park and unpark

`parked` is how the whole endpoint is switched off (system design, sections 5.6 and 11.2). It is a
flag of the owner's configuration, and a deployment through the pipeline applies it: parked, the
stage drops what costs money at idle, about 170 dollars a month, and keeps its data and the
pipeline, which then cost a few dollars a month. Unparked, the same deployment brings everything
back. While the endpoint is parked nothing answers students, and they use their own subscriptions.

Parking is not the session switch, and not a pool's mode. The session switch (runbook
[a session switched on and off](session-on-and-off.md)) is a record that says whether students are
served; it removes nothing and deploys nothing. A pool's mode (runbook
[a disabled pool](disabled-pool.md)) stops one pool. Both are records in the pool-state table, and
parking keeps them as they are.

**What parking removes.** The public load balancer with its certificate, its HTTPS listener, its
alias record and its web ACL; the NAT gateway, so the private subnets have no way out; the internal
load balancer; the three Fargate services (the gateway, the key issuer and Grafana); each pool's
group, its warm pool, its lifecycle hooks and its release; the scaler and its schedule; the fleet's
alarms; and, beside the pipeline, the weekly GPU canary job and its schedule. The pools' instances,
warm ones included, are terminated with their groups.

**What parking keeps.** Every table, with what it holds: the roster, the keys, the usage rows, the
pools' modes and the session switch, the heartbeats. The model bucket, the bucket of the load
balancers' access logs, the Prometheus workspace, and the log groups with fixed names (`/rhi/gateway`,
`/rhi/scaler`, `/rhi/issuer`, `/rhi/pools/<pool>`, the web ACL's): every record and sample there still
expires 90 days after it was written. The VPC, its subnets, its security groups and its S3 endpoint;
each pool's instance role and secret; the admin's sign-in; the alerts topic, the cost budget and
Cost Anomaly Detection. The pipeline, with its reporter, its spike jobs and the image promotion job.
The canary's key in the secret `rhi/canary-key`, which the admin CLI wrote and no stack holds.

So a key issued before parking is still valid after unparking: parking deletes no key, and a key
stops working only when it is revoked or when it expires at the course end (section 6.4). The admin
CLI works while the endpoint is parked, since its tables are there.

**What does not work while parked.** `rhi login`, `rhi status`, `rhi wait` and every request: the
endpoint's address no longer resolves. Grafana, which is a Fargate service. The image promotion
job, which refuses a pool that has no group (`no-group`). And the verification: every execution of
the pipeline, a merge to `main` included, deploys the parked stage and runs no test, so what a merge
changes in the endpoint is first tested by the execution that unparks.

**What this runbook rests on.** The deployment hold lands with issue E4-42: a check before each
stage's deployment that waits while the session switch reads on (section 8.2). Once it is deployed,
an execution that parks or unparks during a class waits for the session to be switched off. Until
it is deployed, nothing holds a deployment: confirm that no session is on before you start the
execution (Check, step 1), and switch none on until it has ended.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. Anything else (the AWS console, GitHub, `aws`,
`gh`, `git`) is written out in full and is tested only by walking through the runbook.

`OWNER/REPOSITORY` below stands for this repository on GitHub, `<pipeline>` for the pipeline's
name and `<execution>` for the ID of an execution of it. The admin CLI is run from the repository's
root, with your own AWS credentials; define its name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

## When

- **Park between cohorts:** once the course has ended and the runbook [course end](course-end.md)
  has been followed, or for any break of weeks.
- **Park between sessions** only when you accept a deployment to unpark before each class (section
  11.2). An unpark is a whole pipeline execution, a new certificate validated through the hosted
  zone, and a GPU start for each warm instance as the warm pools fill again: start it the day before
  the class, not shortly before.
- **Unpark** before the next session or the next cohort, with that much time ahead.

## Check

1. **No session is on.** A deployment that parks removes the gateway and the pools under a class;
   one that unparks fills the warm pools. Read the switch:

   ```sh
   rhi_admin session status
   ```

   It must read `off`. Until the deployment hold of issue E4-42 is deployed, this check is the only
   thing that keeps the execution away from a class: start the execution only while the switch
   reads off, and switch no session on until it has ended (Verify). Once the hold is deployed, an
   execution started while the switch reads on waits before the stage's deployment until it reads
   off.

2. **No execution of the pipeline is in progress,** on the pipeline's page in the CodePipeline
   console. The pipeline runs its executions one after the other: an execution in progress deploys
   before yours, and a merge of it may change what yours deploys. Let it end first.

3. **Before unparking only:**

   - **The course end is still ahead, or this is a new cohort.** Every key, the canary's too,
     expires at the course end. Unparking for the cohort that was parked keeps its keys, the
     canary's included. Unparking for a new cohort needs its `course_end` and a new key for the
     canary before the execution starts (Act, step 2): the verification of the unpark reads the
     canary's key, and the old one expired with the old course.

   - **The GPU quota covers each pool's `max_instances` plus one** (runbook
     [a session switched on and off](session-on-and-off.md), Check, step 1). Unparking launches
     each pool's warm instances at once.
   - **A machine image change waits for the unpark.** The promotion job refuses a parked pool;
     promote the image after the unpark (runbook
     [machine image and engine upgrade](image-and-engine-upgrade.md)).

## Act

1. **To park,** set the flag in your configuration file, kept outside the working tree:

   ```toml
   parked = true
   ```

   then validate the file and write it to SSM:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   An edit of SSM changes no commit, so nothing starts on its own: start an execution with
   **Release change** on the pipeline's page in the CodePipeline console. Note the execution's ID:
   it is the first row of the pipeline's execution history, with the trigger
   `StartPipelineExecution` and your identity:

   ```sh
   aws codepipeline list-pipeline-executions --pipeline-name <pipeline> --max-items 5 \
       --query 'pipelineExecutionSummaries[].[pipelineExecutionId,status,startTime,trigger.triggerType]' \
       --output text
   ```

   **The pipeline's self-update.** The flag also changes the pipeline itself (the weekly canary
   goes, and the verification no longer runs tests), so the execution's self-update (the stage
   `UpdatePipeline`, its action `SelfMutate`) updates the pipeline. The pipeline is not set to
   restart an execution when it is updated: the execution you started can end at or after the
   self-update without reaching the stage `Rhi`, and no other execution starts on its own. Read
   its actions (Verify, step 1). When none of the stage `Rhi` ran, and the history above shows no
   newer execution in progress, start one more with **Release change**: the pipeline already has
   its new shape, so that one runs through. `<execution>` below is the execution whose actions of
   the stage `Rhi` ran, whichever it is.

   The stage's stacks deploy in the order parking needs (`src/rhi/stage.py`): the stacks that
   import from others drop their imports first, then the network stack removes the load balancer
   and the NAT gateway. The gateway's tasks drain before they stop, for up to 30 minutes (section
   8.5), so the execution can take that long and more.

2. **To unpark,** set the flag back, or remove its line, which reads `false`:

   ```toml
   parked = false
   ```

   For a new cohort, set its course end in the same edit (the date below is the example's):

   ```toml
   course_end = 2099-12-31
   ```

   and write it to SSM the same way:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   **For a new cohort, give the canary a new key now,** before any execution: the command reads
   the course end from SSM, so the key it writes expires at the new one, and the verification of
   the unpark reads it. It works while the stage is parked, since its tables are there:

   ```sh
   rhi_admin canary
   ```

   The new cohort joins the roster as runbook [owner setup](owner-setup.md), step 9, says, before
   or after the unpark.

   Then start an execution with **Release change**, and find `<execution>` as in step 1: the
   pipeline updates itself again, since the weekly canary and the verification's tests come back,
   and the execution you started may end at the self-update with none started after it.

   **No approval.** Unparking adds back the roles, policies and security group rules that parking
   removed. The pipeline holds no approval (system design, section 8.4, control 6), so the
   execution deploys them without waiting for you: they are the ones the stage's code declares,
   which the examiner and `tests/policy/` held when that code merged, under the bounded
   bootstrap's boundary.

   The deployment creates the load balancers, the certificate (validated through the hosted
   zone), the NAT gateway, the services, and each pool's group, whose warm pool fills at once: each
   warm instance runs for the minutes it takes to pull the engine's image and check its release,
   billed at its GPU price, then stops.

## Verify

1. **The execution ended.** Read every action of `<execution>`, with its status, the latest attempt
   first:

   ```sh
   aws codepipeline list-action-executions --pipeline-name <pipeline> \
       --filter pipelineExecutionId=<execution> \
       --query 'actionExecutionDetails[].[stageName,actionName,status,startTime]' --output text
   ```

   Every action reads `Succeeded`, `Verify` included. A failed action is the runbook
   [a bad deployment](bad-deployment.md): an SSM edit is reverted by the same configuration step.

2. **After parking, nothing answers and the data is there.** The verification ran no test: the
   commit at the head of `main` carries a `verify/<stage>` status whose description reads "No test
   ran: the system is parked and has no endpoint. The deployment is not verified." A status names a
   commit, not an execution, so read it beside step 1:

   ```sh
   gh api repos/OWNER/REPOSITORY/commits/main/statuses \
       --jq '.[] | "\(.created_at) \(.context) \(.state) \(.description)"'
   ```

   No pool has a group any more (an empty list), and the stage's NAT gateway is gone (no line reads
   `available`):

   ```sh
   aws autoscaling describe-auto-scaling-groups --filters Name=tag-key,Values=rhi-pool \
       --query 'AutoScalingGroups[].AutoScalingGroupName'
   aws ec2 describe-nat-gateways --filter Name=tag:aws:cloudformation:stack-name,Values=Rhi-Network \
       --query 'NatGateways[].State' --output text
   ```

   The CLI no longer reaches the endpoint, and the roster is still there:

   ```sh
   rhi status
   rhi_admin roster list
   ```

   `rhi status` fails to reach the endpoint's address; `rhi_admin roster list` lists every entry
   as before.

3. **After unparking, the verification's tests ran and passed with the canary's key.** For the
   cohort that was parked, that key was issued before parking, so this also shows that such a key
   still works; for a new cohort, it is the key of Act, step 2. In the execution's detail page, in the stage the endpoint deploys
   from, open the details of the action Verify, which link to its CodeBuild log, and read the end of
   the log:

   - a line of pytest's summary that reports tests `passed` and none `failed` or `error`: the smoke
     tests ran against the endpoint with the canary's key, and passed;
   - the line `verify: no test was run:` followed by `listener-off` or `canary-missing`: the
     endpoint is not verified, even though Verify succeeded. For `canary-missing`, run
     `rhi_admin canary`, start another execution and read its log again; for `listener-off`, the
     configuration's `https_listener` is off;
   - a summary with tests `failed`: the verification failed. On a new cohort's unpark, first make
     sure the canary's key is the new one: run `rhi_admin canary`, start another execution and
     read its log again. If its tests fail too, the runbook [a bad deployment](bad-deployment.md)
     applies.

   The unpark creates each pool's group anew, and an instance refresh is what tells the
   verification that a GPU pool's release changed, so the log also reads
   `gpu-verification: the smoke tests run on no GPU pool`: this execution tested the mock pool,
   not the coursework pool. The coursework pool is first tested at its next start (step 5).

4. **A student's key still works.** For the cohort that was parked: with a key issued to your own
   roster entry before parking, the status of the pools is answered, which the gateway does only
   for a valid key:

   ```sh
   rhi status
   ```

   For a new cohort, whose keys are issued after the unpark, log in first with your own roster
   entry, which issues a key and keeps it for the CLI, then ask for the status with that key:

   ```sh
   rhi login
   rhi status
   ```

   It lists each pool with its state. A refusal reads `invalid_key` whatever its cause: the
   gateway never says whether a key expired, was revoked or was never issued. Parking deletes no
   key, so look for the cause elsewhere. If the course end has passed (Check, step 3), every key
   has expired. Otherwise the key may have been revoked. Log in again for a new key and ask for
   the status once more.

5. **The pools are back.** In Grafana, on the dashboard "Fleet and scaling", in each pool's row,
   the panel "Instances: desired, in service, warm" reads the pool's `warm_instances` for warm once
   the warm pool has filled. At the pool's first start, the dashboard "Deployments and releases",
   panel "Release each instance serves: image digest and model revision", shows the release it
   serves.

Walked through on the deployed system: not yet.
