# The owner's one-time steps

What only the owner can do, once, in this order (system design, section 8.6).
[`setup/owner-setup.sh`](../../setup/owner-setup.sh) walks through the nine steps. This page says,
for each one, what it needs, the command, how to check it, and the decision of section 15 of the
design that it rests on.

**The owner runs the script, from a shell no agent runs in.** No agent runs it, and nothing here
is claimed as done until the owner has seen the check.

Every value on this page is a placeholder: `123456789012` for the account, `us-east-1` for the
region, `example.com` for the domain. The real ones go on the command line, into your file outside
the working tree and into SSM. They are never written into a file of this repository, an issue, a
pull request or a commit message, and neither is anything AWS prints.

## The script

```sh
setup/owner-setup.sh --dry-run                                  # print every step and every command
setup/owner-setup.sh                                            # the walkthrough
setup/owner-setup.sh --step configuration --config <file>       # one step on its own
```

- It shows each command before it runs it, and runs it only after a `y`. Any other answer skips
  the command.
- `--dry-run` reads no answer and calls neither AWS nor GitHub: `aws`, `gh` and `cdk` are never
  started. It prints `123456789012`, `us-east-1` and `<quota code>` where a real run reads the
  value from your credentials. Start with it.
- `--step <name>` runs one step: `quotas`, `domain`, `apps`, `configuration`, `billing`,
  `bootstrap`, `connection`, `repository` or `roster`. A step can be repeated.
- It writes nothing to the working tree. What it learns (the account, a quota's code, an address)
  goes to the terminal, and the configuration goes to SSM. The tools it starts write nothing there
  either: Python keeps no bytecode, and uv's project environment and the CDK's output go to a
  temporary folder that is removed when the script ends. So the script needs no `uv sync` first
  and leaves no `.venv` and no `cdk.out` behind.

It needs, on the machine: the AWS CLI with your credentials and their region set, `gh` logged in
as the repository's admin, `uv`, and for step 6 the CDK CLI (`npm ci --ignore-scripts`,
[`setup/README.md`](../../setup/README.md)).

## 1. Request the GPU quotas

**Needs.** The coursework pool's `max_instances`, whether the upgrade pool is wanted, and whether
the optional xl pool is. The quota is per region, so the credentials name the region the endpoint
lives in.

**Command.**

```sh
setup/owner-setup.sh --step quotas --max-instances 2          # add --upgrade-pool, --xl-pool
```

The G-family request is 8 vCPUs for each coursework instance, for one instance more than
`max_instances`: a draining instance and a cold launch can overlap (system design, section 5.4).
The upgrade pool adds 96 vCPUs. So `max_instances = 2` asks for 24 vCPUs, and 120 with the
upgrade pool. A P-family quota, 192 vCPUs for one xl instance, is requested only with `--xl-pool`.
The step looks up each quota's code by its name, then asks before it sends the request:

```sh
aws service-quotas list-service-quotas --service-code ec2 \
  --query "Quotas[?QuotaName=='Running On-Demand G and VT instances'].[QuotaCode,Value]" --output text
aws service-quotas request-service-quota-increase --service-code ec2 \
  --quota-code <quota code> --desired-value 24
```

Phases 0 and 1 need no GPU quota, but nobody documents how long a request takes (section 14):
send it the day the design is approved. The quota is also a ceiling: no bug can run more than it
allows.

**Check.** The request and its status, then the quota once it is granted:

```sh
aws service-quotas list-requested-service-quota-change-history-by-quota --service-code ec2 \
  --quota-code <quota code> --query 'RequestedQuotas[].[Status,DesiredValue]' --output text
aws service-quotas get-service-quota --service-code ec2 --quota-code <quota code> --query Quota.Value
```

**Decision.** 5, class size: it sets `max_instances`, and with it the quota. The defaults of
section 4.2, 2 instances, fit a class of twenty working hands-on. 3, for the upgrade pool. 9, for
the region. The xl pool is built only if the owner asks for it (section 4.2).

## 2. Register the domain

**Needs.** The choice of a domain or a subdomain.

**Command.** None: register the domain, or delegate the subdomain, so that a public hosted zone of
Route 53 in this account answers for it. `setup/owner-setup.sh --step domain` prints this. The
domain and the ID of its hosted zone then go into the configuration file of step 4, as `domain`
and `hosted_zone_id`.

**Check.** The zone exists and is public, and the domain's name servers are the zone's:

```sh
aws route53 list-hosted-zones-by-name --dns-name example.com --max-items 1 \
  --query 'HostedZones[].[Name,Id,Config.PrivateZone]' --output text
dig +short NS example.com
```

**Decision.** 10, the domain.

## 3. Create the three GitHub Apps

**Needs.** A browser: a script cannot set a GitHub App's permissions.

**Command.** `setup/owner-setup.sh --step apps` prints, for each App, the permissions to set, and
the page where an App is registered: `https://github.com/settings/apps/new` (GitHub, Settings,
Developer settings, GitHub Apps, New GitHub App). For each: Webhook not active, installable only
on this account.

| App | Permissions | Installed on | What it leaves you with |
| --- | --- | --- | --- |
| Student login | None. Device flow on | Nobody installs it | Its client ID, which goes into the configuration as `login_client_id` (step 4) |
| Status reporter | Commit statuses, read and write. Nothing else | This repository only | Its private key, kept outside every checkout until the reporter's own step stores it in the reporter's secret |
| The factory's identity | Contents, Pull requests and Issues, read and write; Commit statuses, Checks and Actions, read-only. No Workflows, no Administration | This repository only | Its private key, which stays on the factory's host. [`green-light.md`](../factory/green-light.md), step 7, creates this App |

**Check.** Each App's settings page shows these permissions and no other. For the factory's App,
`scripts/implement.sh doctor` shows `github identity` as `PASS`. A later step of the walkthrough,
which arrives with the reporter, reads each App back from GitHub.

**Decision.** None of section 15. The Apps are those of sections 6.4 and 8.4 of the design and of
ADR 0001, amendment 2.

## 4. Write the configuration to SSM

**Needs.** A configuration file that you keep outside the working tree, for instance
`$HOME/.config/rhi/config.toml`, readable by you alone. Start from a copy of
[`config.example.toml`](../../config.example.toml) and replace every placeholder:

| Key | What it holds |
| --- | --- |
| `domain`, `hosted_zone_id` | The domain of step 2 and the ID of its hosted zone |
| `admin_email` | The address of the one admin user, which the alerts go to |
| `instructor_phone` | The phone the class-day notice also goes to |
| `github_owner`, `github_repo` | The repository the pipeline deploys from |
| `login_client_id` | The client ID of the student login App of step 3 |
| `monthly_budget_usd` | The monthly budget, in whole US dollars |
| `time_zone`, `course_end` | The IANA time zone of the schedule, and the day the course ends |
| `session_windows`, `pools` | Empty lists for now: `[]` |

The loader requires every one of these keys. It refuses a session window and a pool entry until
the changes that give them their shape, so both lists stay empty. The windows and each pool, with
its GPU-hour ceilings, are added later, by running this step again.

**Command.**

```sh
setup/owner-setup.sh --step configuration --config "$HOME/.config/rhi/config.toml"
```

The step refuses a file inside the working tree, and a link in the tree to a file outside it.
It then runs two commands, the second after a `y`:

```sh
env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_config check <file>
env PYTHONPATH=src uv run --locked --no-dev python -m rhi.owner_config write <file>
```

The first is the configuration loader (`src/rhi/config.py`), reading the file as the pipeline
will read it: a file it refuses stops the step, with a message that names the key and never its
value. The second is the configuration writer: one String parameter per key under `/rhi/config/`,
over those that exist. It deletes none. A dry run runs the first command too, since it reads the
file and calls nothing.

**To change the configuration later**, edit the file and run the same command again. Other
runbooks and the owner's steps of later changes use this step for that.

**Check.** The names of the parameters, not their values: one per key of the table, except
`pools`, which has a parameter per entry and so none yet.

```sh
aws ssm get-parameters-by-path --path /rhi/config/ --recursive --query 'Parameters[].Name' --output text
```

**Decision.** 10, for the domain. 1, dates, for the course end date and, later, the session
windows. 6, money, for the monthly budget and, later, each pool's GPU-hour ceilings. 9, for the
region the parameters are written in. The first deployment of step 6 reads this configuration.

## 5. Turn on billing alerts

**Needs.** The account's root user, or a user allowed to change the billing preferences.

**Command.** None: it is a setting of the Billing console. Under Billing preferences, Alert
preferences, turn on the CloudWatch billing alerts. `setup/owner-setup.sh --step billing` prints
this. Each SNS subscription is confirmed later, from the e-mail it sends to the admin address,
when the stack that creates its topic is deployed.

**Check.** The preference shows as on. Some hours later the account publishes its billing metric,
which AWS keeps in one region whatever the endpoint's own:

```sh
aws cloudwatch list-metrics --namespace AWS/Billing --region us-east-1 \
  --query 'Metrics[].MetricName' --output text      # EstimatedCharges
```

**Decision.** None of its own. The amounts are those of decision 6, in the configuration of step 4.

## 6. Bootstrap the account, then deploy the pipeline stack once

**Needs.** The CDK CLI (`npm ci --ignore-scripts`), and credentials for the region the endpoint
lives in. [`setup/bootstrap/README.md`](../../setup/bootstrap/README.md) describes the two
policies and what they deny.

**Command.**

```sh
setup/owner-setup.sh --step bootstrap
```

It deploys the two policies, reads the account and the region from your credentials, and
bootstraps with the execution policy and the permissions boundary:

```sh
aws cloudformation deploy --stack-name rhi-bootstrap-policies \
  --template-file setup/bootstrap/policies.yaml --capabilities CAPABILITY_NAMED_IAM
aws sts get-caller-identity --query Account --output text
RHI_CONFIG=example node_modules/.bin/cdk bootstrap aws://123456789012/us-east-1 \
  --cloudformation-execution-policies arn:aws:iam::123456789012:policy/rhi-cfn-execution \
  --custom-permissions-boundary rhi-permissions-boundary --output <a temporary folder>
```

The first deployment of the pipeline stack, once, from this machine, follows. The pipeline stack
is not in the repository yet: its command arrives with it. It needs this bootstrap and the
configuration of step 4.

**Check.** Both read, and change nothing. The first lists `rhi-cfn-execution` and nothing else,
the second names `rhi-permissions-boundary`. The script offers both.

```sh
aws iam list-attached-role-policies --role-name cdk-hnb659fds-cfn-exec-role-123456789012-us-east-1
aws iam get-role --role-name cdk-hnb659fds-cfn-exec-role-123456789012-us-east-1 --query Role.PermissionsBoundary
```

**Decision.** 9, the region: it is the one of the credentials you bootstrap with.

## 7. Authorize the GitHub connection

**Needs.** The first deployment of step 6: the pipeline stack creates the connection, and a
connection that CloudFormation creates starts pending.

**Command.** None: authorize it once in the console (Developer Tools, Settings, Connections), and
install the AWS connector on this repository only. `setup/owner-setup.sh --step connection` prints
this.

**Check.**

```sh
aws codeconnections list-connections --provider-type-filter GitHub \
  --query 'Connections[].[ConnectionName,ConnectionStatus]' --output text      # AVAILABLE
```

**Decision.** None of section 15. That the connector reaches this repository alone is part of the
trust boundary (system design, section 8.4).

## 8. Apply the ruleset and the repository's settings

**Needs.** `gh` logged in as the repository's admin, and `jq`. These are the steps 2 and 4 of
[`green-light.md`](../factory/green-light.md): if they are done, there is nothing to run.

**Command.**

```sh
setup/owner-setup.sh --step repository
```

It offers the ruleset, secret scanning with push protection, and the no-reply address to commit
with, which you then set yourself with `git config user.email`:

```sh
bash setup/apply-ruleset.sh
gh repo edit --enable-secret-scanning --enable-secret-scanning-push-protection
gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'
```

**Check.** As in `green-light.md`, step 4: the ruleset is `active` and refuses a probe push to
`main`, and both scanners report `enabled`. `git log --format='%an <%ae>' -1` shows the no-reply
address.

**Decision.** None of section 15.

## 9. Enter the roster

**Needs.** The students' numeric GitHub IDs, and the admin CLI, which is not in the repository
yet.

**Command.** It arrives with the admin CLI. `setup/owner-setup.sh --step roster` says so. The
roster is never written to the repository.

**Check.** With the admin CLI, when it exists.

**Decision.** None of section 15.
