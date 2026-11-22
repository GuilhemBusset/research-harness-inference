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

   - **The course end is still ahead.** Every key, the canary's too, expires at the course end. For
     a new cohort, set its `course_end` in the same edit as `parked` (Act, step 2), then, once the
     execution has ended, give the canary a new key, which the verification of the next execution
     uses, and put the new cohort on the roster (runbook [owner setup](owner-setup.md), step 9):

     ```sh
     rhi_admin canary
     ```

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
   `StartPipelineExecution` and your identity. The flag also changes the pipeline itself (the
   weekly canary goes, and the verification no longer runs tests), so its self-update action
   updates the pipeline, and CodePipeline then runs the change again in a new execution, with the
   pipeline's update as its trigger: follow the newest execution from then on, `<execution>` below.

   The stage's stacks deploy in the order parking needs (`src/rhi/stage.py`): the stacks that
   import from others drop their imports first, then the network stack removes the load balancer
   and the NAT gateway. The gateway's tasks drain before they stop, for up to 30 minutes (section
   8.5), so the execution can take that long and more.

2. **To unpark,** set the flag back, or remove its line, which reads `false`:

   ```toml
   parked = false
   ```

   and write it to SSM the same way:

   ```sh
   setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
   ```

   Then start an execution with **Release change**, and follow the newest execution as in step 1:
   the pipeline updates itself again, since the weekly canary and the verification's tests come
   back. The deployment creates the load balancers, the certificate (validated through the hosted
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

3. **After unparking, the verification's tests ran and passed with a key issued before parking.**
   The canary's key is one: the canary command wrote it before the stage was parked, unless Check,
   step 3 gave it a new one. In the execution's detail page, in the stage the endpoint deploys
   from, open the details of the action Verify, which link to its CodeBuild log, and read the end of
   the log:

   - a line of pytest's summary that reports tests `passed` and none `failed` or `error`: the smoke
     tests ran against the endpoint with the canary's key, and passed;
   - the line `verify: no test was run:` followed by `listener-off` or `canary-missing`: the
     endpoint is not verified, even though Verify succeeded. For `canary-missing`, run
     `rhi_admin canary`, start another execution and read its log again; for `listener-off`, the
     configuration's `https_listener` is off.

   The unpark creates each pool's group anew, and an instance refresh is what tells the
   verification that a GPU pool's release changed, so the log also reads
   `gpu-verification: the smoke tests run on no GPU pool`: this execution tested the mock pool,
   not the coursework pool. The coursework pool is first tested at its next start (step 5).

4. **A student's key still works.** With a key issued to your own roster entry before parking, the
   status of the pools is answered, which the gateway does only for a valid key:

   ```sh
   rhi status
   ```

   It lists each pool with its state. A refusal names its cause: a key that expired means the
   course end has passed (Check, step 3), and a revoked one that the key was revoked, not that
   parking lost it.

5. **The pools are back.** In Grafana, on the dashboard "Fleet and scaling", in each pool's row,
   the panel "Instances: desired, in service, warm" reads the pool's `warm_instances` for warm once
   the warm pool has filled. At the pool's first start, the dashboard "Deployments and releases",
   panel "Release each instance serves: image digest and model revision", shows the release it
   serves.

Walked through on the deployed system: not yet.
