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
- **One student asks for a copy** of their data during the course: Act, step 2 alone, for that
  student, into the folder of Check, step 3; then give them the file. Nothing is erased and no key
  is revoked: the student keeps their access, and their data stays until the course end like
  everyone's. Do not run step 1 or step 3 for a copy.
- **One student asks for the deletion** of their data during the course: Act, step 3, for that
  student alone, preceded by step 2 only if they asked for a copy as well. The erase revokes their
  keys too.

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

   **Students removed during the course** are not on this list, yet `rhi_admin roster remove`
   deleted only their entry: their revoked keys and their usage rows stay until retention removes
   them. They are erased too (Act, step 3), and cannot be exported. List every GitHub ID that has
   a key or a usage row: first the names of the stage's three tables (the logical IDs hold `Keys`,
   `Roster` and `Usage`),

   ```sh
   aws cloudformation list-stack-resources --stack-name Rhi-Access \
       --query "StackResourceSummaries[?ResourceType=='AWS::DynamoDB::Table'].[LogicalResourceId,PhysicalResourceId]" --output text
   ```

   then the GitHub IDs in the keys and the usage tables, `<keys-table>` and `<usage-table>` being
   their names:

   ```sh
   for table in <keys-table> <usage-table>; do
       aws dynamodb scan --table-name "$table" --consistent-read --projection-expression github_id --query 'Items[].github_id.N' --output text || echo "INCOMPLETE: $table was not read" >&2
   done | tr '\t' '\n' | sort -u
   ```

   The scan reads with strong consistency. A default scan reads with eventual consistency, so
   it can miss a row written a moment before. Each ID it prints that the roster does not list is a student removed during the course (or one
   whose earlier erase was interrupted). Note those IDs, in your terminal only, for Act, step 3. A
   line `INCOMPLETE` means the list is not whole: run it again.

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

3. **Erase each student's data,** once their export, if any, read `exported`, and the data of
   each student removed during the course (Check, step 2), whom no export reaches:

   ```sh
   rhi_admin privacy erase 1234567 && echo "1234567: erased" || echo "1234567: NOT erased"
   ```

   The erase deletes the roster entry, revokes and deletes every key, and deletes every usage row
   with its affinity cookies. It waits about two minutes on each student, for the logins and the
   gateway's cache it outwaits, so it is run for one student after the other. On success it exits
   with status 0, prints `Erased 1234567:` and what it deleted, then what only retention removes:
   the workspace's series under the alias, the gateway's log records under it, and the load
   balancers' and web ACL's logs, which no student can be tied to; and the line ends with
   `1234567: erased`. With the entry gone, the alias names nobody, and no new entry is given it
   until that data has expired.

   **An erase that did not end with `erased` is not complete,** whatever else looks done. The
   erase removes the roster entry first and deletes the keys and the usage rows only after its
   wait, so one that was interrupted (Ctrl-C, a closed terminal, expired credentials) or that
   printed `Refused:` (such as `rows of the student were written during the erase: run it
   again`) can leave keys and usage rows behind a roster entry that is already gone. Run the same
   line again for that student, even though `rhi_admin roster list` no longer shows them: an erase
   takes an interrupted one over and deletes what is left. Repeat until it ends with `erased`.

   **Check the erases now,** before step 4: the admin CLI finds its tables through the stage's
   stacks, so once the stage is deleted it cannot run. The roster and a refused export are not
   enough, since an interrupted erase passes both. For each student, those removed during the
   course included, run the erase once more; it
   must be refused because nothing of the student is left:

   ```sh
   rhi_admin privacy erase 1234567
   ```

   It prints `Refused: that GitHub ID has no roster entry, no key and no usage row.` and deletes
   nothing. Any other end means something of the student was left: this run erases it, and you
   run the check again until it is refused that way. Every student must pass this check before you
   park or delete the stage. The roster then holds the canary's entry and the spikes' students
   only, or nothing at all:

   ```sh
   rhi_admin roster list
   ```

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

   a. **Write down what the stacks retain.** Some resources are retained when their stack is
      deleted, so that no deployment can delete them by mistake (section 8.5): the tables, the
      buckets, the admin's sign-in and the workspace, and, besides them, a log group and a key. List
      every one before the stacks go, since their stacks are what names them. The list is read from
      each deployed template's deletion policies, so it holds whatever the deployed stacks retain;
      read it in your terminal and never paste it anywhere:

      ```sh
      (
        for stack in RhiPipeline Rhi-Network Rhi-Access Rhi-Observability Rhi-Pools Rhi-Gateway Rhi-Guardrails; do
          template=$(aws cloudformation get-template --stack-name "$stack" --query TemplateBody --output json) || { echo "INCOMPLETE: the template of $stack was not read" >&2; exit 1; }
          ids=$(printf '%s' "$template" | jq -r '.Resources | to_entries[] | select(.value.DeletionPolicy == "Retain" or .value.DeletionPolicy == "RetainExceptOnCreate") | .key') || { echo "INCOMPLETE: the template of $stack was not understood" >&2; exit 1; }
          for id in $ids; do
            aws cloudformation describe-stack-resource --stack-name "$stack" --logical-resource-id "$id" --query 'StackResourceDetail.[StackName,ResourceType,PhysicalResourceId]' --output text || { echo "INCOMPLETE: $id of $stack was not read" >&2; exit 1; }
          done
        done
      ) && echo "The list is complete."
      ```

      The list counts only when it ends with `The list is complete.` A read that fails stops it
      with a line that starts with `INCOMPLETE:` and a failure status, so that no stack is left out
      of it silently: run it again until it is complete, and do not go on to step b before it is.
      Each line is a stack, a type and an identifier. With the example configuration they are:

      | Stack | Type | What it is | Holds a student's data |
      | --- | --- | --- | --- |
      | `Rhi-Access` | `AWS::DynamoDB::Table`, three | The keys, roster and usage tables | Yes |
      | `Rhi-Pools` | `AWS::DynamoDB::Table`, two | The pools' state and heartbeats | No |
      | `Rhi-Access` | `AWS::Cognito::UserPool`, `AWS::Cognito::UserPoolUser` | The admin's sign-in to Grafana, and the admin in it | No |
      | `Rhi-Observability` | `AWS::APS::Workspace` | The Prometheus workspace; its identifier is an ARN, whose last part, after `workspace/`, is the workspace's ID (the admin CLI reads it the same way) | Yes |
      | `Rhi-Network` | `AWS::S3::Bucket` | The load balancers' access logs | Yes |
      | `Rhi-Pools` | `AWS::S3::Bucket` | The model bucket | No |
      | `Rhi-Pools` | `AWS::Logs::LogGroup` | The retained log group `/aws/lambda/...` of the function a custom resource of the stack runs, 90 days' retention | No |
      | `Rhi-Guardrails` | `AWS::KMS::Key` | The alerts key, which encrypts the alerts topic | No |
      | `RhiPipeline` | `AWS::S3::Bucket` | The pipeline's artifact bucket | No |

      A line that is not in this table is retained too: it is deleted or kept like the line of its
      type in step d. Steps d and the Verify section use these identifiers, and only these.

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
      the gateway's included, go with their stacks, and their records with them, all but the
      retained log groups of step a, which step d deletes.

   d. **Delete what the stacks retained,** each identifier from step a in place of the
      placeholder. Each one you decide to keep instead (the model bucket for a later deployment,
      say), write down with the reason: the Verify section expects it to be there, and only those.
      A resource that holds a student's data (step a's last column) is not kept.

      The five tables. For each, remove its deletion protection, and wait until the table is
      active again with the protection off: the update is asynchronous, and DynamoDB refuses to
      delete a table that is still updating. The wait returns once the table is active, and the
      description must then read `ACTIVE` and `False`:

      ```sh
      aws dynamodb update-table --table-name <table> --no-deletion-protection-enabled
      aws dynamodb wait table-exists --table-name <table>
      aws dynamodb describe-table --table-name <table> --query 'Table.[TableStatus,DeletionProtectionEnabled]' --output text
      ```

      Only then delete it, and wait until the deletion has completed, which is also asynchronous:

      ```sh
      aws dynamodb delete-table --table-name <table>
      aws dynamodb wait table-not-exists --table-name <table>
      ```

      A table with point-in-time recovery leaves a system backup when its deletion completes, which
      DynamoDB keeps for 35 days. Once the wait above has returned, for each table of step a, and
      no other, list its system backups by the table's name, then delete each backup that lists.
      Should a deleted table list none yet, list it again a few minutes later: the Verify section
      lists them once more.

      ```sh
      aws dynamodb list-backups --table-name <table> --backup-type SYSTEM --query 'BackupSummaries[].BackupArn' --output text
      aws dynamodb delete-backup --backup-arn <backup>
      ```

      A backup of a table that step a did not list is not the stage's: leave it.

      The retained log group, by its name from step a (the `/aws/lambda/...` group of
      `Rhi-Pools`). It holds no student's data; deleting it removes its records at once, rather
      than after its retention:

      ```sh
      aws logs delete-log-group --log-group-name <log-group>
      ```

      A pipeline stack once deployed with the approval on widened permissions, which the pipeline
      no longer holds (system design, section 8.4, control 6), retained the log group of that
      approval's function when the approval went. It belongs to no stack since, so step a does not
      list it. It holds no student's data. List it by its prefix, and delete each group that lists
      with the command above:

      ```sh
      aws logs describe-log-groups --log-group-name-prefix /aws/lambda/RhiPipeline-PipelinePipelinesSecurityCheck --query 'logGroups[].logGroupName' --output text
      ```

      The alerts key of `Rhi-Guardrails`, by its key ID from step a. Nothing uses it once its stack
      is deleted. KMS deletes a key only after a waiting period, at least 7 days, during which it
      can still be cancelled:

      ```sh
      aws kms schedule-key-deletion --key-id <key> --pending-window-in-days 7
      ```

      The workspace, by its workspace ID from step a (the end of its ARN, not the whole ARN):

      ```sh
      aws amp delete-workspace --workspace-id <workspace-id>
      ```

      Its series can no longer be queried once the workspace is deleted, and AWS deletes the data
      it ingested for good within a month after; nothing more is yours to do for it.

      The admin's sign-in, first without its deletion protection:

      ```sh
      aws cognito-idp update-user-pool --user-pool-id <user-pool> --deletion-protection INACTIVE
      aws cognito-idp delete-user-pool --user-pool-id <user-pool>
      ```

      Should the second command refuse because the pool still has its sign-in domain, read the
      domain's prefix, delete the domain, then run the second command again:

      ```sh
      aws cognito-idp describe-user-pool --user-pool-id <user-pool> --query UserPool.Domain --output text
      aws cognito-idp delete-user-pool-domain --user-pool-id <user-pool> --domain <domain-prefix>
      ```

      The three buckets, the access logs', the model bucket and the pipeline's artifact bucket,
      each emptied first:

      ```sh
      aws s3 rm s3://<bucket> --recursive
      aws s3api delete-bucket --bucket <bucket>
      ```

   e. **What is left is the owner's,** outside the stage, and holds no student's data: the
      configuration in SSM and the image parameters `/rhi/images/<pool>`; the secrets the admin
      CLI wrote, `rhi/canary-key` and `rhi/spike-keys`, and the GitHub Apps' secrets; the CDK's
      bootstrap. Remove them, or keep them for a later deployment, as you choose.

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

   Each identifier of Act, step 4a, is gone, but for those you wrote down as kept in Act, step 4d,
   which are still there. For each other one, its command below, run with the identifier, fails
   because what it names does not exist, each `list-backups` lists nothing, each
   `describe-log-groups` prints nothing, and the key reads `PendingDeletion`:

   ```sh
   aws dynamodb describe-table --table-name <table> --query Table.TableName
   aws dynamodb list-backups --table-name <table> --backup-type SYSTEM --query 'BackupSummaries[].BackupArn' --output text
   aws cognito-idp describe-user-pool --user-pool-id <user-pool> --query UserPool.Id
   aws s3api head-bucket --bucket <bucket>
   aws logs describe-log-groups --log-group-name-prefix <log-group> --query "logGroups[?logGroupName=='<log-group>'].logGroupName" --output text
   aws kms describe-key --key-id <key> --query KeyMetadata.KeyState --output text
   ```

   The workspace, by its workspace ID, either no longer exists or reads `DELETING`:

   ```sh
   aws amp describe-workspace --workspace-id <workspace-id> --query workspace.status.statusCode
   ```

4. **Retention is set on every log group that holds a student's records,** on a parked stage, the
   day you park it. The endpoint's log groups that hold a student's records have fixed names:
   `/rhi/gateway`, `/rhi/issuer`, `/rhi/scaler`, `/rhi/pools/<pool>` for each pool of your
   configuration, and the web ACL's, `aws-waf-logs-rhi`. Each reads `90`:

   ```sh
   aws logs describe-log-groups --log-group-name-prefix /rhi/ --query 'logGroups[].[logGroupName,retentionInDays]' --output text
   aws logs describe-log-groups --log-group-name-prefix aws-waf-logs-rhi --query "logGroups[?logGroupName=='aws-waf-logs-rhi'].[logGroupName,retentionInDays]" --output text
   ```

   The listing by `/rhi/` can show other groups of the endpoint as well; only the names above are
   read here. One of those names that reads `None` keeps its records for ever: set its retention,
   with that exact name and no other (never a group the listing shows under another name, which
   can belong to another application of the account), and file the fault as an issue:

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
