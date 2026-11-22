# Course end: teardown and data deletion

What the system keeps of a student is personal data: the roster entry, the keys (their SHA-256 and
their times, never the key), the usage rows, the gateway's log records under the student's alias,
the series labelled with that alias in the Prometheus workspace, and source addresses in the load
balancers' and the web ACL's logs (system design, section 6.6). Every store holds it until the
course end plus 90 days, and no longer. This runbook is the end of a course: stopping every key,
exporting what the owner keeps, erasing each student's data, parking or deleting the stage, and
what retention removes afterwards.

**What happens without anyone.** Every key expires at the course end, the configuration's
`course_end` (section 6.4): a key row's expiry is the course end itself. The roster and usage rows
expire at the course end plus 90 days, and DynamoDB deletes an expired row within days after its
expiry. Each log record and each sample of the workspace expires 90 days after it was written, and
each object of the access-log bucket too. This runbook acts before that: it stops access the day the
course ends, and removes what can be removed at once.

**The order matters.** `rhi_admin privacy export` exports a student only while their roster entry
exists, and both `rhi_admin roster remove` and `rhi_admin privacy erase` delete the entry. So the
keys are revoked with `rhi_admin key revoke`, which leaves the entry; each export is made while the
entry exists; and only then does the erase delete it. Do not use `roster remove` here: a student
removed that way can no longer be exported, and their usage rows stay until retention removes them.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`. Anything else (the AWS console, GitHub, `aws`,
`gh`, `git`) is written out in full and is tested only by walking through the runbook. Deleting the
stage (Act, step 4) is a step you take with your own AWS credentials; no agent runs it.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

The admin commands name a student by their numeric GitHub ID, the key of the roster. `1234567` in
the code blocks below is an example: run each such command once for each student, with the IDs
that `rhi_admin roster list` prints.

## When

- **The course is over:** after its last session, on or before the configured `course_end`. A
  course that ends before that date still has working keys until it: revoke them (Act, step 1).
- **One student asks** for their data, or for its deletion, during the course: Act, steps 2 and 3,
  for that student alone. The erase revokes their keys too.

## Check

1. **No session is on,** and none will be:

   ```sh
   rhi_admin session status
   ```

   If it reads `on`, switch it off:

   ```sh
   rhi_admin session off
   ```

2. **The roster:** the GitHub ID and the alias of each entry.

   ```sh
   rhi_admin roster list
   ```

   Two kinds of entry are the owner's, not students': the canary, GitHub ID `0` and alias
   `canary`, whose key the pipeline's verification uses, and the spikes' students, whose GitHub
   IDs no account can have either. Leave them while the stage is kept for another cohort; deleting
   the stage removes them with everything else. Read the list in your terminal and keep no copy of
   it in the repository.

3. **What you keep, and where.** The student notice (section 6.6) says what is kept after the course
   and why: nothing at all, or, say, each student's usage when it counts toward grading. Export only
   the students whose data the notice says you keep. An export is personal data: it goes to a folder
   outside the working tree, readable by you alone, and never to the repository, an issue or a
   chat. Make that folder:

   ```sh
   mkdir -m 700 "$HOME/rhi-course-end"
   ```

## Act

1. **Revoke every key,** for each student's GitHub ID:

   ```sh
   rhi_admin key revoke 1234567
   ```

   It revokes every key of the student, on every installation, and prints how many. A revoked key
   stops working within 60 seconds, the gateway's lookup cache (section 6.4). The roster entry
   stays, so the export of step 2 still works. Until the erase of step 3 deletes the entry, or the
   parking of step 4 removes the key issuer, a student who logs in again gets a new key; the erase
   revokes that one as well.

2. **Export what you keep,** for each student of Check, step 3, into the folder made there:

   ```sh
   rhi_admin privacy export 1234567 > "$HOME/rhi-course-end/1234567.json.part" && mv "$HOME/rhi-course-end/1234567.json.part" "$HOME/rhi-course-end/1234567.json" && echo "1234567: exported" || echo "1234567: NOT exported"
   ```

   The export is written beside the kept file and takes its name only once the command succeeded,
   so a failed run, a first one or a retry, leaves any copy you already had as it was. It ends with
   `exported` or `NOT exported`: erase a student whose data you keep (step 3) only after the line
   for them read `exported`. After `NOT exported`, delete the `.part` file it left and run the line
   again.

   The export is one JSON document: the roster entry, each key's issue, expiry and revocation times
   (no key and no SHA-256), the usage rows, the gateway's log records under the student's alias,
   and the samples of every series labelled with the alias, read day by day over the workspace's 90
   days. It reads the logs and the workspace, so it takes a while for each student. A student who is
   no longer on the roster is refused: `Refused: that GitHub ID is not on the roster.`

3. **Erase each student's data,** once their export, if any, read `exported`:

   ```sh
   rhi_admin privacy erase 1234567
   ```

   The erase deletes the roster entry, revokes and deletes every key, and deletes every usage row
   with its affinity cookies. It waits about two minutes on each student, for the logins and the
   gateway's cache it outwaits, so it is run for one student after the other. It prints what it
   deleted, then what only retention removes: the workspace's series under the alias, the
   gateway's log records under it, and the load balancers' and web ACL's logs, which no student can
   be tied to. With the entry gone, the alias names nobody, and no new entry is given it until that
   data has expired. When it prints that rows of the student were written during the erase, run it
   again for that student.

   **Check the erases now,** before step 4: the admin CLI finds its tables through the stage's
   stacks, so once the stage is deleted it cannot run. The roster holds the canary's entry and the
   spikes' students only, or nothing at all:

   ```sh
   rhi_admin roster list
   ```

   and an export of each erased student is refused, since the entry is gone:

   ```sh
   rhi_admin privacy export 1234567
   ```

   It prints `Refused: that GitHub ID is not on the roster.`

4. **Park or delete the stage.**

   - **Park it** when another cohort will use the endpoint: runbook
     [park and unpark](park-and-unpark.md), Act, step 1. The data that is left expires by its
     retention (step 5); the pipeline, the tables and the workspace stay for the next cohort.
   - **Delete it** when the endpoint is not used again. The tables, the logs and the buckets go
     when they are deleted, rather than by retention; the workspace's series stop being readable
     when the workspace is deleted, and AWS deletes them for good within a month after (step d).
     It is written out in full, and you run it with your own credentials, in the region of the
     endpoint.

   **Deleting the stage.** The stacks are named as the CloudFormation console lists them: the
   pipeline stack `RhiPipeline`, and the stage's stacks `Rhi-Network`, `Rhi-Access`,
   `Rhi-Observability`, `Rhi-Pools`, `Rhi-Gateway` and `Rhi-Guardrails`.

   a. **Write down what the stacks retain.** The tables, the buckets, the admin's sign-in and the
      workspace are retained when their stack is deleted, so that no deployment can delete them by
      mistake (section 8.5). List them before the stacks go, since their stacks are what names them;
      read the list in your terminal and never paste it anywhere:

      ```sh
      for stack in Rhi-Access Rhi-Pools Rhi-Observability Rhi-Network; do aws cloudformation describe-stack-resources --stack-name "$stack" --query "StackResources[?ResourceType=='AWS::DynamoDB::Table' || ResourceType=='AWS::S3::Bucket' || ResourceType=='AWS::Cognito::UserPool' || ResourceType=='AWS::APS::Workspace'].[ResourceType,PhysicalResourceId]" --output text; done
      ```

      Each line is a type and an identifier: a table's name, a bucket's name, the user pool's ID,
      and, for the workspace, its ARN, whose last part, after `workspace/`, is the workspace's ID
      (the admin CLI reads it the same way). Steps d and the Verify section use these identifiers,
      and only these.

   b. **Delete the pipeline stack first.** Otherwise the next merge to `main` deploys the stage
      again:

      ```sh
      aws cloudformation delete-stack --stack-name RhiPipeline
      aws cloudformation wait stack-delete-complete --stack-name RhiPipeline
      ```

   c. **Delete the stage's stacks,** each after the stacks that import from it:

      ```sh
      for stack in Rhi-Guardrails Rhi-Gateway Rhi-Pools Rhi-Observability Rhi-Access Rhi-Network; do aws cloudformation delete-stack --stack-name "$stack" && aws cloudformation wait stack-delete-complete --stack-name "$stack" || break; done
      ```

      The loop stops at the first stack that does not delete; read its events in the
      CloudFormation console, fix the cause, and run the loop again. The log groups of the stage,
      the gateway's included, go with their stacks, and their records with them.

   d. **Delete what the stacks retained,** each name from step a in place of the placeholder. The
      five tables, each first without its deletion protection:

      ```sh
      aws dynamodb update-table --table-name <table> --no-deletion-protection-enabled
      aws dynamodb delete-table --table-name <table>
      ```

      A table with point-in-time recovery leaves a system backup when it is deleted, which
      DynamoDB keeps for 35 days. For each table of step a, and no other, list its system backups
      by the table's name, then delete each backup that lists:

      ```sh
      aws dynamodb list-backups --table-name <table> --backup-type SYSTEM --query 'BackupSummaries[].BackupArn' --output text
      aws dynamodb delete-backup --backup-arn <backup>
      ```

      A backup of a table that step a did not list is not the stage's: leave it.

      The workspace, by its workspace ID from step a (the end of its ARN, not the whole ARN):

      ```sh
      aws amp delete-workspace --workspace-id <workspace-id>
      ```

      Its series can no longer be queried once the workspace is deleted, and AWS deletes the data
      it ingested for good within a month after; nothing more is yours to do for it.

      The admin's sign-in, first without its deletion protection (should the second command refuse
      because the pool still has its sign-in domain, delete the domain with
      `aws cognito-idp delete-user-pool-domain` first):

      ```sh
      aws cognito-idp update-user-pool --user-pool-id <user-pool> --deletion-protection INACTIVE
      aws cognito-idp delete-user-pool --user-pool-id <user-pool>
      ```

      The two buckets, the access logs' and the model bucket, each emptied first:

      ```sh
      aws s3 rm s3://<bucket> --recursive
      aws s3api delete-bucket --bucket <bucket>
      ```

   e. **What is left is the owner's,** outside the stage, and holds no student's data: the
      configuration in SSM and the image parameters `/rhi/images/<pool>`; the secrets the admin
      CLI wrote, `rhi/canary-key` and `rhi/spike-keys`, and the GitHub Apps' secrets; the
      pipeline's artifact bucket; the CDK's bootstrap. Remove them, or keep them for a later
      deployment, as you choose.

5. **What retention removes afterwards,** on a parked stage, or of what an erase left:

   | What | Removed | By |
   | --- | --- | --- |
   | A key that was not revoked | Stops working at the course end; its row then expires | The keys table's expiry |
   | A roster entry, a usage row, a held alias | The course end plus 90 days, and DynamoDB deletes it within days after | The tables' expiry |
   | The gateway's, the scaler's, the issuer's, the pools' and the web ACL's log records | 90 days after each was written | The log groups' retention |
   | The load balancers' access logs | 90 days after each was written | The bucket's expiry |
   | The series labelled with an alias | 90 days after each sample was written | The workspace's retention |

   CloudWatch metrics, which cannot be deleted for 15 months, hold nothing per student (section
   6.6). The exports of step 2 are yours, and so is their deletion when the notice says.

## Verify

1. **Each export you keep is there,** outside the working tree, readable by you alone, and no
   `.part` file is left:

   ```sh
   ls -l "$HOME/rhi-course-end"
   ```

2. **On a parked stage, the erases held and the stage is parked.** The roster still holds the
   canary's entry and the spikes' students only, or nothing at all, as the check of Act, step 3
   found:

   ```sh
   rhi_admin roster list
   ```

   and the stage is parked: the runbook [park and unpark](park-and-unpark.md), Verify, step 2.

3. **On a deleted stage, nothing of it is left.** The admin CLI cannot run any more (its stacks
   are gone), so this reads each service. No stack of the endpoint is left (an empty list):

   ```sh
   aws cloudformation describe-stacks --query "Stacks[?starts_with(StackName, 'Rhi')].StackName"
   ```

   Each identifier of Act, step 4a, is gone. Each of these commands, run with the identifier, fails
   because what it names does not exist, and each `list-backups` lists nothing:

   ```sh
   aws dynamodb describe-table --table-name <table> --query Table.TableName
   aws dynamodb list-backups --table-name <table> --backup-type SYSTEM --query 'BackupSummaries[].BackupArn' --output text
   aws cognito-idp describe-user-pool --user-pool-id <user-pool> --query UserPool.Id
   aws s3api head-bucket --bucket <bucket>
   ```

   The workspace, by its workspace ID, either no longer exists or reads `DELETING`:

   ```sh
   aws amp describe-workspace --workspace-id <workspace-id> --query workspace.status.statusCode
   ```

4. **Retention is set on every log group that holds a student's records,** on a parked stage, the
   day you park it. Each fixed log group, and the web ACL's, whose name starts with
   `aws-waf-logs-`, reads `90`:

   ```sh
   aws logs describe-log-groups --log-group-name-prefix /rhi/ --query 'logGroups[].[logGroupName,retentionInDays]' --output text
   aws logs describe-log-groups --log-group-name-prefix aws-waf-logs- --query 'logGroups[].[logGroupName,retentionInDays]' --output text
   ```

   A group that reads `None` keeps its records for ever: set its retention, and file the fault as
   an issue:

   ```sh
   aws logs put-retention-policy --log-group-name <group> --retention-in-days 90
   ```

5. **The records are gone, after the course end plus 90 days and a week,** on a stage parked since
   the course end. CloudWatch Logs deletes an expired record some time after it expires, not at
   the instant, hence the week. In the AWS console, CloudWatch, Logs Insights, on the log group
   `/rhi/gateway`, with an absolute time range from the course's first day to the day the stage was
   parked, so that it covers every record the course wrote, the query `stats count(*)` counts no
   record. A count above zero: read the group's retention again (step 4). With `90` set, the
   records are still waiting for their deletion; look again a week later, and file the fault as an
   issue if they are still there. With none set, the step 4 fault applies.

Walked through on the deployed system: not yet.
