# Coverage of the design by the backlog

What `docs/design/system-design.md` (sections 1 to 17) and `docs/adr/0001-software-factory.md`
ask for, and the draft under `backlog/drafts/` that delivers each part. The backlog has 6 epics
(E0 to E5) and 227 issues; `scripts/backlog.sh plan` lists them in order with their dependencies.

How to read it:

- An issue id (`E2-14`) is a draft under `backlog/drafts/E2/`. Numbering has holes: a missing
  number is unused, and an id is never reused.
- "Scaffolding" is what the tree already holds and no draft repeats: the vendored engine and its
  sandbox, the hygiene scanner, the git hooks, `scripts/check.sh`, the CI workflows, the ruleset
  definition and `docs/factory/green-light.md`.
- An "owner item" is an acceptance item that names a capability, `(needs aws:account)` and the
  like. The loop builds and proves the rest of the issue and leaves that item to the owner.
- The tables say where each part is delivered. The parts to read are at the end: **Gaps**, **Open
  questions and readings for the owner**, and **For whoever runs the loop**.

## Epics

| Epic | Rollout phase (section 14) | Issues | Exit criteria, and the issue that carries each as an owner item |
| --- | --- | --- | --- |
| E0 | Factory (ADR 0001) | 28 | None of its own. It carries phase 0's second exit criterion, a commit status on GitHub: E0-12 |
| E1 | Phase 0 | 17 | The pipeline deploys an empty stage: E1-10. A commit status appears on GitHub: E0-12 |
| E2 | Phase 1 | 88 | A student key works end to end against the mock pool: E2-64. The scaler's scenarios and the forbidden-route tests pass in the pipeline's verification: E2-64, which runs the scenarios in the synth step and the forbidden routes at the gateway's hop only (open question 1) |
| E3 | Phase 2 | 44 | Measured cold start: E3-15. Hold behaviour: E3-16. Harness results: E3-41 and E3-35. What one instance serves: E3-42. The weekly canary green twice: E3-13 |
| E4 | Phase 3 | 39 | A dry-run class on the coursework model, within the objectives of section 7.3 and the ceilings: E4-26 |
| E5 | Phase 4 | 7 | A scheduled session served by the upgrade pool: E5-03 |

## Requirements (section 1)

| ID | Requirement | Issues |
| --- | --- | --- |
| R1 | An inference endpoint the harnesses point at | Gateway: E2-13 to E2-30, E2-73 to E2-79, E2-84. Edge: E2-45 to E2-48. Proven on the mock pool: E2-64; on a GPU: E3-40, E3-12 |
| R2 | Machines scale from 0 to n with load | Scaler: E2-05 to E2-12, E3-31, E4-05. Pools: E2-71, E2-72, E2-40. Hold, wake and demand: E2-25 to E2-28, E2-73, E2-78. On a GPU: E3-40 |
| R3 | An open-weight LLM chosen for the harnesses it serves | The coursework model: E3-10, E3-22, E3-03, E3-04. The request profile that makes it work in Claude Code: E3-23 to E3-27. Its test in each harness: E3-41, E3-35. The upgrade model: E5-03, E5-06, E5-08 |
| R4 | Students log in securely | E2-01, E2-14, E2-50 to E2-55, E2-83, E2-56; spike S9 on E2-52 |
| R5 | Telemetry in Grafana, with each pool's prediction beside its measurement | Collection: E2-57, E2-58, E2-79, E2-66, E3-05, E3-06. Grafana: E2-59, E2-60, E2-85, E2-80. Dashboards: E2-61, E2-81, E4-11 to E4-15, E4-28, E4-29. Expected values: E2-68, E3-10, E5-07 |
| R6 | Production grade; CodePipeline, Python CDK, uv, ty | E1-01 to E1-12, E1-16, E1-17; the verification E2-64; the deployment hold E4-07; the status reporter E0-09 to E0-14 |
| R7 | The repository can be public without leaking AWS information | The scanner and the hooks are scaffolding. E1-03, E1-04, E1-10, E1-15, E0-04, E0-16, E0-20, E0-24, E2-62, E3-21 |
| R8 | A cheap model first; bigger models when needed | The coursework pool is the cheap model, with no cheaper GPU tier below it (section 4.2): decision 2, on E3-10. The bigger model: E5-03 |
| R9 | Know what the model and machines serve, and measure it | The estimate as each pool's expected values: E2-68, E3-10, E5-07. Measured: S5 (E3-19, E3-42, with the load generator E3-36) and S6 (E5-05). Shown: E4-12, E4-29, E4-28 |

## Components (section 2)

| Component | Issues |
| --- | --- |
| Edge: public load balancer, ACM, Route 53, WAF | E2-47, E2-48; the network and security groups E2-45, E2-46 |
| Gateway | E2-13 to E2-29, E2-73, E2-74, E2-77 to E2-79, E2-84; entry point and image E2-76; the Fargate service E2-30; its target group, health check and listener rule E2-90 |
| Key issuer | E2-50, E2-51, E2-83, E2-52 |
| Scaler | E2-05 to E2-12; E3-31; E4-01, E4-02, E4-05, E4-34, E4-39; E5-01 |
| State: DynamoDB | E2-03, E2-67, E2-49, E2-88; expiry E4-35 |
| Model pools: Auto Scaling, launch templates, warm pools | Entry: E2-04, E2-68, E2-69. Group: E2-71, E2-72, E2-40, E2-41, E2-42, E2-43. Instance: E2-33 to E2-38, E2-70. GPU: E3-01, E3-02, E3-38, E3-39. Pools: E3-10, E3-40, E5-03 |
| Pool routing: internal load balancer | E2-44; stickiness E3-29 |
| Model store: S3 | E3-07, E3-08, E3-09 |
| Metrics: Managed Prometheus | E2-57, E2-58, E2-79, E3-05 |
| Alarms and logs: CloudWatch | E4-09, E4-10, E4-34; logs E2-58 (instances), E4-18 (retention), E4-30 (pipeline events), E4-37 (load balancers) |
| Dashboards: Grafana, Cognito | E2-59, E2-60, E2-85, E2-65, E2-80, E2-61, E2-81, E4-11 to E4-15, E4-28, E4-29 |
| Delivery: CodePipeline, CodeBuild, CodeConnections, ECR | E1-09, E1-19, E1-10, E2-87, E2-64, E3-12, E3-13, E4-07, E0-12 |
| Guardrails: ceilings, Budgets, SNS | E4-01 to E4-04, E4-32, E4-36; the fleet cap E2-39; the price limit E2-68 |

Not in the table of section 2, and drafted: the mock engine (E2-31, E2-82, E2-75), the contract
tests (E2-32), the lifecycle agent (E2-33 to E2-37, E2-66, E5-02), the `rhi` CLI (E2-53 to E2-55),
the admin CLI (E2-56, E4-03, E4-19, E4-27, E3-14, E5-01) and the status reporter (E0-09 to E0-14,
E0-21, E0-22, E0-26, E0-27).

## The rest of the design, by section

| Section | Issues |
| --- | --- |
| 3 Request flow | Warm path: E2-14, E2-15, E2-16, E2-24, with affinity E3-30. Cold path: E2-25, E2-26, E2-73, E2-78, and the scaler's wake E2-05 |
| 4.1 Engine contract | Routes, health and canned streams: E2-31. Metrics: E2-82 (the per-engine table and the mock's row), E3-06 (vLLM's row), E2-66 (cache capacity as a number). Reachability: E2-44 (four routes, 404 for the rest), E2-43 (bearer secret), E2-46 (port 8000 from the internal load balancer only). Identity: E2-35. Shutdown: E2-82, E2-36. Contract tests: E2-32. vLLM's contract settings: E3-04, with the environment in the image, E3-03 |
| 4.2 The model and the pools | The pool entry: E2-04, E2-68 (prices, price limit, expected values), E2-69 (validation, the default pool, the example's mock pool). The coursework pool: E3-10, E3-40, E3-22 (its release's serving settings), E3-09 and E3-34 (the 8-bit and 4-bit manifests). The upgrade pool: E5-03, E5-06, E5-08. Students choose a pool by model name: E2-16, E2-27. The xl pool: not drafted (Gaps) |
| 4.3 What happens on an instance | Machine image parameter and promotion: E3-01, E3-02, E3-39. Boot unit and digest check: E2-38. Units: E2-70, E2-89 (the lifecycle agent's program), E3-38 (GPU units on GPU instances only), E3-05 (DCGM), E2-58 (collector). Lifecycle agent: E2-33 (release check), E2-34 (launch), E2-35 (heartbeat, liveness by inference), E2-36 (termination), E2-37 (stage timings), E5-02 (multi-GPU starts). Hooks and their timeouts: E2-40. Zone rebalancing suspended, scale-in protection, IMDSv2, no public address: E2-71. Instance role and Session Manager policy: E2-41. Engine image: E3-03. Refresh on a release or an image: E2-42, E3-02 |
| 4.4 Model weights | Bucket and grants: E3-07. Allow-list and manifests: E3-08. Copy job: E3-09. Model code off, and the streamer: E3-04. Download-then-load fallback: E3-44 |
| 4.5, 4.6 Why Auto Scaling groups, why vLLM alone | Reasons for choices; nothing to build |
| 4.7 What a pool can serve | Expected values: E2-68, E3-10, E5-07. A session's number of instances, and the checks that follow from it: E4-33, E4-05. Levers: the 4-bit build E3-34; reasoning effort E3-24, E3-27; the 8-bit cache, the step budget and the draft head (off by default) E3-22; a shared cache salt E3-37; more or larger instances and the faster sibling are configuration; a smaller window is not drafted (Gaps). Measured: E3-19, E3-42 |
| 5.1 The scaler | Inputs and records: E2-03, E2-67, E2-28 (gateway heartbeat), E2-35 (instance heartbeat). Decision: E2-05 (wake, stop, zero), E2-06 (1 to n), E2-07 (n to n-1), E2-08 (silent gateway, launch loop), E3-31 (rebalance), E4-05 (pre-warm), E4-02 (ceilings). Scenarios: E2-09. AWS calls: E2-10. One run: E2-11. One writer: E2-12. Spend: E4-01. Outside the scaler: E2-39 and E2-71 (maximum), E2-72 (nightly action), E4-09 (alarms). Scheduled-only pools: E2-25, E5-01. S7: E3-20 |
| 5.2 Cold-start budget | Stage timings: E2-37, shown by E2-81. Assumed settings: the 5-second health check E2-44, the compile cache on the EBS volume E2-70, the release check before the engine E2-33. S1: E3-15. A baked image: not drafted (Gaps) |
| 5.3 What the client sees | Hold and keep-alives: E2-25, E2-26. Readiness from target health every 5 seconds: E2-73. Errors at the deadline: E2-02, E2-26. Idle timeouts: E2-44, E2-47. Cheap calls: E2-26, E2-27. `GET /status` and `POST /wake`: E2-27, E2-78. The Codex profile: E2-54. `rhi wait`: E2-55. S2: E3-16 |
| 5.4 When capacity is not there | Three zones: E2-45, E2-71. Fallback types and their prices: E2-68, E4-01. Quota: E1-13, E4-33. An instance never healthy: E2-40, E4-09. A launch loop: E2-08, E2-10, cleared by E4-03. Class day: E4-05, E4-06, E4-27 |
| 5.5 Keeping a student on one instance | E3-28 to E3-32; the mock's prefix cache E2-75; S8: E3-33 |
| 6.1 Wire formats | Allow-lists: E2-17, E2-18, E2-19. The three defects: E3-24, E3-25, E3-26, put together for the coursework pool in E3-27. Served context: E3-22. Captures: E3-21, E3-43. S3: E3-17, E3-41, E3-35 |
| 6.2 What the gateway does | Step 1: E2-13. Step 2: E2-14. Step 3: E2-15, E2-77, E3-30. Step 4: E2-16 to E2-19, E3-23 to E3-27, E3-37. Step 5: E2-25, E2-26, E2-73, E2-78. Step 6: E2-20 to E2-24. Step 7: E2-24, E2-77. Step 8: E2-02, E2-77, E4-32. Logs: E2-29. Metrics: E2-74, E2-79, E2-84. The service: E2-76, E2-30. Replays: E2-62, E3-43 |
| 6.3 Why not LiteLLM | Decision 7, on E2-13. The fallback is not drafted (Gaps) |
| 6.4 Student login | Key format: E2-01, and the scanner's rule for it E0-16. Issuer: E2-50, E2-51, E2-83, E2-52. CLI: E2-53, E2-54, E2-55. Roster and direct keys: E2-56. What students are told (confirm only a code their own terminal printed, convert a PDF to text, the limits of web search, the hold and `rhi wait`): the student guide, E4-21 |
| 6.5 Edge and network | E2-45, E2-46, E2-47, E2-48. Outbound IPv6: not drafted (Gaps) |
| 6.6 Privacy | Per-student cache salt: E2-16, with a shared salt as an option E3-37. Metadata-only logs: E2-29. Alias labels and nothing per student in CloudWatch: E2-79, E4-18, E4-34. Retention: E4-18, E4-35, E4-37. Export and erase: E4-19. Notice: E4-20 |
| 7.1 Observability pipeline | E2-57, E2-58, E2-79 (the gateway's sidecar), E2-11 (the scaler); CloudWatch metrics for alarms: E4-34 |
| 7.2 What is collected | Engine: E2-82, E3-06, E2-66. GPU: E3-05. Per student: E2-79. What students experience: E2-74, E2-84. Fleet: E2-11, E2-81. Cold start: E2-37, E2-81. Cost: E4-01, E4-14. Deployments: E4-30, E4-15. The six dashboard rules: E2-61 (1 to 3), E3-22 (4), E4-12 (5), E2-58 (6) |
| 7.3 Dashboards, alerts, objectives | Dashboards: E2-81, E4-11, E4-12 with E4-29, E4-13, E4-14, E4-15; the session view E4-28; built from the deployed configuration E2-61, E2-80. Alerts: E4-09, E4-10, E4-04, E4-06, E4-36; pipeline failures by SNS E0-22. Objectives: E4-16, with the provisional targets E4-31 |
| 7.4 Which Grafana | E2-59, E2-65, E2-60, E2-85, E2-80; decision 11 on E2-60 |
| 8.1 Repository layout | E1-01, E1-02; packaging E1-16; the students' `uv tool install` command, in the student guide E4-21 |
| 8.2 Pipeline | 1 Source and 3 self-update: E1-09. 2 Synth: E1-10 (configuration from SSM, leak check), E1-17, E1-05 (cdk-nag). 4 Assets: E1-16, E2-87, E2-42, E2-76, E2-80, E3-03. 5 Deploy: E1-09; held around sessions E4-07. 6 Verify: E2-63, E2-64 (mock pool), E3-12 (coursework pool), E3-13 (weekly canary). 7 Report: E0-09 to E0-14, E0-21, E0-22, E0-26, E0-27 |
| 8.3 Checks on a pull request | Synthesis without credentials: E1-04. cdk-nag: E1-05. cfn-lint, as a test every gate runs: E1-06. Container tests: E0-28 (gate step), E0-17 (CI job). Lock, ruff, ty, the scanner, workflow linting and gitleaks: scaffolding |
| 8.4 The trust boundary | Below |
| 8.5 Production without staging | The mock pool on every deployment: E2-64. The deployment hold: E4-07. The gateway's 1800-second drain: E2-30, E2-90. State protection: E2-49, E2-88. Forward fix or revert: E0-07, E0-29 |
| 8.6 What the owner does once | The walkthrough: E1-13, with E1-15 (local values), E1-17 (the SSM writer) and E1-18 (the Apps and the reporter's secret). Step 1 quotas: E1-13, E4-33, E5-03. Step 2 domain: E1-13, E2-47. Step 3 GitHub Apps: E1-13, E1-18, E0-03, E0-12. Step 4 configuration: E1-13, then E2-71, E3-10, E3-27, E4-33, E5-03. Step 5 billing alerts and subscriptions: E1-13, E4-36, E0-22, E4-06. Step 6 bootstrap and pipeline: E1-08, E1-10. Step 7 connection: E1-10. Step 8 ruleset, secret scanning, commit address: `docs/factory/green-light.md`. Step 9 roster: E2-56. Recurring: approvals E1-09, owner review E0-15, preflight E4-22 |
| 9 Operations | Runbooks below; patching E4-25 |
| 10 Public-safe | Below |
| 11.1, 11.2 Cost | Accrual and the cost dashboard: E4-01, E4-14. `parked`: E4-17, E4-38. One gateway task: the key `gateway_tasks`, E2-76, E2-30. One warm instance: `warm_instances`, E2-04, E2-72 |
| 11.3 Guardrails | Hard caps: E2-39 and E2-71 (maximum), E2-69 (allowed types), E2-68 (price limit), the quota E1-13. Ceilings: E4-01, E4-02, E4-32. One operating state: E4-03. Budgets: E4-04. Per student: E2-15, E2-14, E2-50 (expiry). Attribution: E4-08, E4-39. The outside-sessions alarm: E4-09. Detection: E4-36, E4-14 |
| 12 Why this will work | Spikes below |
| 13 Risks, 16 Alternatives | Nothing of their own to build |
| 14 Rollout | Epics above |
| 15 Decisions | Below |
| 17 Configuration defaults | Below |

## Configuration defaults (section 17)

E1-03 states the loader's rule: the owner's keys are the only required ones, and every key or pool
field added later has a default or is optional, so that no merge needs a value the deployed
configuration lacks. Each default is stated in the issue that adds its key.

| Setting | Default | Issues |
| --- | --- | --- |
| Rate limits at the edge, on the issuer's routes, on Grafana | 2,000, 100 and 300 requests per 5 minutes per source address | E2-48 |
| Issuer's reserved concurrency | 5 | E2-52 |
| Key issuances per student | 5 a day | E2-52 (key), E2-51 |
| Request body limit | 8 MB | E2-76 (key), E2-16 |
| Longest request | 30 minutes | E2-76 (key), E2-24, E2-77 |
| Student's daily token budget | 100 million tokens, cached input included | E2-56 (the admin CLI's default, copied into roster and key items), E2-15, E2-21 to E2-23 |
| Student's concurrent requests | 4 | E2-56, E2-15 |
| Hold | 420 s for a stream, 240 s for a non-streaming call | E2-04, E2-25 |
| Keep-alive interval | 10 s | E2-76 (key), E2-25 |
| `Retry-After` | 30 s for `warming`, 60 s for `capacity_unavailable`, 5 s for `concurrency_limit` | E2-02, E2-76 (keys) |
| Heartbeats | Every 5 s, ignored after 20 s | E2-03, E2-28, E2-35 |
| Idle time before a pool scales to zero | 20 minutes | E2-04, E2-05 |
| Pre-warm lead | 60 minutes for the coursework pool | E4-33 (pool field), E4-05 |
| Drain | 300 s for a pool, 1800 s for the gateway | E2-04, E2-40, E2-90 |
| Stickiness on a pool's target group | 3600 s, renewed by every request | E3-28, E3-29 |
| Launch hook timeout until S1 has measured a start | 15 minutes for the coursework pool; the mock pool uses the same | E2-40, E2-34 |
| Margin added to the drain for the termination hook | 60 s | E2-40, E2-36 |
| Limits the body filter clamps to | The pool's context length and largest output | E2-04, E2-16 |
| Hourly price above which a pool cannot wake on demand | $5 | E2-68, E2-69 (key) |
| GPU-hour ceilings, coursework pool | 16 a day, 150 a month | E3-10 (daily), E4-02 (monthly) |
| `target_concurrency`, coursework pool | 8, the engine's limit on running requests | E3-10; required of every pool by E2-04; passed to the engine by E3-22 |
| Nightly stop | 02:00 in the configured time zone | E2-72 |
| "In service long outside sessions" alarm | 3 hours | E4-09, E4-34 |
| Gateway error-rate alarm | More than 5% of requests failing for the gateway's own fault over 5 minutes | E4-10 |
| Budget notifications | At 50%, 80% and 100% of the monthly amount | E4-04 |
| Cooling-off before an automated digest update | 7 days, none for a security fix | E4-25 |
| Floors for S5 and for the objectives | Time to first token under 5 s at the 95th percentile, first turns apart; median stream 15 tokens/s or more; slowest tenth 10 or more | E4-31 (keys), E3-42, E4-16 |
| Cached share below which a pool is flagged | 85% | E4-31, E4-12 |
| Students moved by one rebalance number, and the least time between two | About one in ten; 60 s | E3-28, E3-31, E3-32 |
| Instances a session window pre-warms | 1, unless the schedule names more | E4-33, E4-05 |
| Reasoning effort sent to the coursework model | `low` and `medium` unchanged; `high`, `xhigh` and `max` as `xhigh` | E3-27, E3-24 |

## The trust boundary (section 8.4)

| Control | Issues |
| --- | --- |
| 1. A bounded deployment role | E1-08 (execution policy and permissions boundary, `tests/policy/test_bootstrap_policies.py`); the pipeline's roles carry the boundary: E1-09. Trust to another account is held by E1-07's template test only (Gaps) |
| 2. Invariants as tests under `tests/policy/` | No ingress from the internet: E2-46. The TLS policy: E2-47. The fleet cap: E2-39. No wildcard IAM, no identity provider, no cross-account trust: E1-07. The pipeline's roles and steps: E1-19 (the reviewed list), extended by every issue that adds a step or job (E1-10, E0-12, E0-13, E0-21, E0-22, E0-26, E0-27, E2-64, E2-87, E3-02, E3-09, E3-12, E3-13, E3-45, E4-07). Protections and logical IDs of stateful resources: E2-88, with E2-49, E2-57, E2-85, E3-07 and E4-37 adding theirs. Retention: E4-18. Acknowledged cdk-nag findings, per construct module under `tests/policy/acknowledged/`: E1-05, E1-07. Beyond the design's list: the bootstrap policies (E1-08), one writer of capacity (E2-12), the instance role (E2-41), admin surfaces not public (E2-60), the ruleset's check source (E0-18) |
| 3. A separate identity for agents | The App's token: E0-03. A workflow change handed to the owner: E0-23. The sandbox: scaffolding |
| 4. A ruleset on `main` with no bypass, whose required check only GitHub Actions can satisfy | The definition is scaffolding (`setup/pr-only-main.json`), applied by the owner (`docs/factory/green-light.md`, step 4). The check's source: E0-18 |
| 5. Owner approval for the boundary itself | E0-15 and E0-25, published only with a yes to decision 8. The protected paths: scaffolding, and E0-02 for the pipeline stack and the bootstrap policies |
| 6. The approval on widened permissions, as a tripwire | E1-09; its waiting state has its own status (E0-09), and the engine releases the landing claim while it waits (E0-07) |
| A public status from fixed vocabularies, never text from AWS | E0-09 |
| A structured failure report, as further statuses made of allow-listed parts | E0-11 (the parts), E0-12 (logical IDs checked against the commit's own template), E0-21 (failed smoke tests), E0-26 (the last lifecycle stage); the engine's fix-forward reads statuses only: E0-29 |
| Superseded executions end with a verdict | E0-13 |
| Spikes and canaries post their numbers; a change records them | E0-14, E0-27, E1-11; the jobs E3-14, E3-45 |
| Anything beyond that goes to the owner by SNS | E0-22 |
| The reporter's credential writes commit statuses and nothing else | E0-10, E0-12, E1-18 |
| The engine reads the verdict | E0-07 (the deploying phase), E0-29 (the failure summary), E0-08 (the contexts, and what a verified deployment discharges) |

## Keeping the repository public-safe (section 10)

| Risk | Where it is handled |
| --- | --- |
| `cdk.context.json` | No lookups and synthesis with lookups off: E1-04, E1-10. The ignore rule: scaffolding |
| Hard-coded environments | E1-04; S11's second half on E1-12 |
| ARNs, domain, e-mail, schedules | The loader and the example: E1-03. SSM: E1-17. The synth step's untracked file: E1-10 |
| Synth output, logs, badges | Ignore rules: scaffolding. Console links and build badges refused by the scanner: E0-24. A status carries no link: E0-09 |
| An identifier slips into a change | The scanner at commit, in the verifier and in CI: scaffolding. A student key: E0-16 |
| The real domain or e-mail slips in | The owner's list outside the repository: E1-15. The engine scans with it before every push: E0-20. The pipeline's leak check: E1-10 |
| A credential slips in | gitleaks in CI: scaffolding. Push protection: the owner's, green-light step 4 |
| Run logs and pull-request text | What the engine publishes is redacted: E0-04 |
| Commit metadata | The owner's, green-light step 2; the doctor checks the engine's commit address (ADR amendment 7) |
| Tests and captures | Tests use the example configuration: E1-03, E1-04. The capture scrubber: E2-62. The recorder replaces the recording machine's home path and user name: E3-21. Keys in tests are generated at run time (E0-16 makes a literal fail the scan) |
| Messages to students and to GitHub | E2-02, E0-09, E0-11 |
| Coding agents | Scaffolding (the sandbox, the example configuration) |
| AWS-owned account IDs | No draft commits a synthesized template. The load balancers' access logs are delivered by service principal, and a test refuses a 12-digit account ID in any template: E4-37 |

## Spikes (section 12)

What every spike shares: the record format (E1-11), the code and canary students (E3-14), the jobs
the owner starts from an explicit list (E3-45), the event that carries a result to the reporter
(E0-27), and the statuses (E0-14).

| Spike | Issues | Who runs it |
| --- | --- | --- |
| S1 cold start | E3-15 | The owner, on the coursework pool; cold runs with `warm_instances` set to 0 |
| S2 client hold, and how each client shows a 402 | E3-16, with E3-46 (the mock's start delay) | A job on the deployed mock pool, started by the owner |
| S3 harness task | E3-17 (the task), E3-41 (the run and verdict), E3-35 (the second-session run); captures E3-21, the 4-bit manifest E3-34 | The owner, on the coursework pool, with both builds; a waiver is the owner's, in writing |
| S4 weights loading | E3-18 | The owner, on the coursework pool |
| S5 what one instance serves | E3-19 (the runs), E3-42 (the verdict against E4-31's floors); the load generator E3-36 | The owner, on the coursework pool, with both builds, at the class size |
| S6 the upgrade model | E5-05 | The owner, paid, with an admin window opened and closed by hand |
| S7 drain and scale-in | E3-20 | A job on the deployed mock pool, started by the owner |
| S8 stickiness | E3-33 (with E2-75) | A job on the deployed mock pool, started by the owner |
| S9 GitHub App device flow | E2-52 | Observed on the deployed issuer by the owner |
| S10 Grafana behind sign-in | E2-59, E2-65 (the CI assertions), E2-60 (the deployed check) | CI's job `container-tests`; the deployed check is the owner's |
| S11 ty and environment-agnostic stages | E1-04 (a clean ty gate), E1-12 (the record) | The first half by agents; the second half is the phase 0 deployment |

## Runbooks (section 9)

| Runbook | Issue |
| --- | --- |
| Class-day preflight | E4-22 |
| Capacity failure during a session | E4-22, with E4-27 (switch the default model) |
| Ceiling or kill switch tripped, and re-arming | E4-22, with E4-03 |
| Leaked key | E4-23, with E2-56 and E2-83 |
| Bad deployment | E4-23 |
| Secret and token rotation | E4-23 |
| Park and unpark | E4-24, with E4-17 and E4-38 |
| Machine image and engine upgrade | E4-24, with E3-02 and E4-25 |
| Course-end teardown and data deletion | E4-24, with E4-19 |

"Each tested once" is an owner item on each of the three issues.

## Decisions for the owner (section 15)

No draft makes a decision. Each is carried as an owner item on the issue named, and the drafts
are written for the design's default meanwhile.

| Decision | Carried by | Written for |
| --- | --- | --- |
| 1 Dates, and what students use if the endpoint is not ready | E4-33 (both halves) | An empty list of session windows until the owner writes them |
| 2 The coursework model, and that it answers "a cheap model first" | E3-10 | Qwen3.8-27B at 8 bits on `g7e.2xlarge` |
| 3 Scheduled sessions on the upgrade pool or on several coursework instances; the upgrade model's licence | E5-03; E5-08 (the repository is the build the decision is about) | The upgrade pool built, scheduled-only, and absent from the account until the owner adds it |
| 4 Harnesses | E3-41 | Claude Code, Codex and an SDK script |
| 5 Class size and homework | E1-13 (the quota), E4-33 (each window's instances), E3-42, E4-26. Homework hours: no item (open question 13) | The defaults, two instances and 16 GPU-hours a day, for a class of twenty working hands-on |
| 6 Money | E1-13 (the monthly budget in SSM), E4-02, E4-04, E4-17, E5-03, E5-05 | Ceilings and amounts read from configuration |
| 7 Gateway | E2-13 | The custom gateway |
| 8 Trust boundary | First half: whether E0-15 and E0-25 are published. Second half: E4-07 | Owner review of the protected paths; deployments held around sessions only |
| 9 Region and privacy | E1-08 (the region bootstrapped), E1-13, E4-20 (who signs the notice) | The region of the deploying credentials |
| 10 Domain | E1-13; E2-47 turns its listener on once the domain resolves | A domain read from configuration |
| 11 Grafana | E2-60 | Self-hosted behind the load balancer's sign-in |
| 12 Continuity: a managed model standing in for a session | E4-22 | Nothing built (Gaps) |

## ADR 0001

| Item | Issue |
| --- | --- |
| Decision 1, the vendored engine; 2, the roles; 3, the sandbox; 7, the backlog as drafts; 8, the dollar ceilings; 10, the protected paths; 11, inert until the green light | Scaffolding |
| Decision 4, hygiene as a gate before anything is pushed | Scaffolding; added rules E0-16 (student key) and E0-24 (console links, badges); the owner's values before every push E0-20 |
| Decision 5, the deployment outcome from commit statuses | The engine: E0-07, E0-29, E0-08. The reporter: E0-09 to E0-14, E0-21, E0-22, E0-26, E0-27 |
| Decision 6, AWS-only work as owner items | Scaffolding (`[host] lacks` in `loop.toml`); discharged from a verified deployment E0-08, from the container job E0-19 |
| Decision 9, invariants as tests | E1-05, E1-07, E1-08, E2-12, E2-39, E2-41, E2-46, E2-47, E2-60, E2-88, E4-18, E0-18; the invariant names in `loop.toml`: scaffolding |
| Amendment 1, the pipeline stack and the bootstrap policies are protected paths | E0-02 |
| Amendment 2, the factory's own GitHub identity | E0-03 (the token), E0-23 (a workflow change handed to the owner) |
| Amendment 3, owner review of the safety surface | E0-15 and E0-25, only with a yes to decision 8 |
| Amendment 4, the deployment report is commit statuses only | E0-09, E0-10, E0-11; the engine's summary from statuses E0-29 |
| Amendment 5, checks that need a container run in CI only | E0-28 (gate step), E0-17 (CI job), E0-19 (the engine discharges the obligation); first users E2-65, E2-86 |
| Amendment 6, the agents' configuration directories are protected | Scaffolding; no draft creates or edits a file under them |
| Amendment 7, a leak in a branch's history ends the issue | Scaffolding; the same outcome for a value of the owner's list: E0-20 |
| Follow-up: measure the examiner on seeded defects | E0-05 |
| Follow-up: move AWS-only items to status contexts | E0-08, in part (Gaps) |
| Follow-up: redact published evidence | E0-04 |
| Follow-up: revisit a scheduler that needs no launch | Not drafted: the ADR asks to revisit, not to build |
| Consequences: ceilings from the first epic's measured cost | E0-06 |

## Gaps

### Left out on purpose

- **DeepInfer** (section 16), **Ray Serve** (section 4.6) and **SGLang** (sections 4.1 and 16):
  E2-04 accepts the engines `mock` and `vllm` only.
- **LiteLLM as the fallback gateway** (section 6.3). Decision 7 is carried on E2-13; a no would
  need the gateway work redone around LiteLLM, with the body filter as a plugin.
- **Managed Grafana** (section 7.4). Decision 11 is carried on E2-60; the swap is not drafted.
- **Outbound IPv6 instead of the NAT gateway** (sections 6.5 and 11.2), an optional spike.
- **Decision 12, a managed model standing in for a session.** The design asks the question and
  designs nothing for a yes; E4-22 carries the question.
- **The xl pool** (DeepSeek-V4.1-Flash on `p5en.48xlarge`), built only if the owner asks
  (sections 4.2 and 14). A yes needs: a pool issue and a P-family quota (section 8.6); a reviewed
  exception for model code (section 4.4); the recipe's other vLLM front end; a request profile of
  its own, since its template refuses an effort value Claude Code sends (section 6.1); a 510 GB
  manifest and a disk that holds it; and an S6 run on it.
- **What follows a failed spike.** A baked image after S1; a shorter hold for one client after
  S2; another profile entry, engine version or the fallback model after S3; fewer streams per
  instance or the upgrade pool after S5; sessions on more coursework instances after S6; a shorter
  delay or balancing in the gateway after S7; a pool kept at one instance after S8; an OAuth app
  after S9 (admin-issued keys exist: E2-56); Managed Grafana after S10; ty rules or stages with an
  environment after S11. Each depends on a number nobody has yet. Some fallbacks are drafted as
  options because S4 and S5 measure them: the download loader (E3-44) and the levers of section
  4.7 (E3-22, E3-24, E3-34, E3-37). The rest is configuration.
- Capacity Blocks and Spot instances (section 16) are alternatives the design rejects.

### Asked for, and delivered only in part or not at all

1. **The compaction-window lever** (section 4.7: "`rhi login` can write it"). E2-54 leaves it out:
   the design gives no value and the setting's name is unverified. S5 can only emulate shorter
   transcripts with the load generator's synthetic sessions (E3-36).
2. **Decision 8, second half, answered yes** (deployments in course weeks wait for a person).
   Nothing is designed for it; E4-07 builds the hold around sessions only.
3. **Homework hours** (decision 5, second half). No item asks for them (open question 13).
4. **AWS-only items moved to status contexts** (ADR 0001, follow-up). E0-08 discharges an item
   needing `aws:deployed-environment` when it names a test under `tests/smoke/` and the
   verification passed. Of the 39 such items in the backlog, only E2-64's first exit item names
   one. The others stay owner items after a verified deployment.
5. **The forbidden-route tests through the internal load balancer** (section 4.1, phase 1 exit).
   E2-64 deselects them until the owner decides how the verification reaches that load balancer
   (open question 1).
6. **The vLLM image is never built in CI.** CI builds the gateway and mock-engine images (E2-86)
   and the Grafana image (E2-65); the engine image is built by the pipeline only, and its first
   proof is E3-03's GPU item.
7. **The ruleset while the repository is private** (section 8.4, control 4). On a free personal
   plan GitHub enforces no ruleset on a private repository, so until the repository is public the
   PR-only rule rests on the hooks, the loop's gates and the `pr-only.yml` audit. E0-18 checks the
   definition, not what GitHub enforces; E0-15's code-owner review is in the same position. The
   probe of green-light step 4 shows which case the account is in.
8. **The advisory workflow's first scheduled run** (E4-25) has no acceptance item: no capability
   says "seen on GitHub after the merge".

### Where the design's text does not hold as written

The drafts follow the workable reading; the design (and the ADR) should say the same.

1. **Section 8.6, step 3** (and green-light step 11) stores the secrets of all three GitHub Apps
   in Secrets Manager. The factory holds no AWS credentials, so its App's key lives on the
   factory's host (E0-03). The login app needs no secret for the device flow. Only the status
   reporter's goes to Secrets Manager (E1-18, E0-12).
2. **Section 8.4, control 1**: a merged change "cannot create ... trust to another account,
   whatever the template says". No IAM condition key reads a role's trust policy, so neither the
   execution policy nor the boundary can deny it; only E1-07's template test holds it, and
   E1-08's README says so. A deploy-time control, such as a CloudFormation hook, would be a design
   addition.
3. **Section 5.1**: the nightly action is what stops a fleet when the scaler is broken. New
   instances are protected from scale-in (section 4.3, E2-71), and Auto Scaling lowers the desired
   capacity without terminating protected instances, so the nightly action (E2-72) stops nothing
   the scaler has not unprotected. The backstop is the alarms of E4-09.
4. **Sections 4.1 and 6.5** disagree: tests call the forbidden routes "through both hops", but
   only the gateway may reach the internal load balancer (open question 1).
5. **Section 6.6**: "the admin CLI can export and erase one student's data". Managed Prometheus
   has no API that deletes series, and CloudWatch Logs cannot delete one record. E4-19 deletes the
   student's rows and roster entry, which cuts the alias's link to the person, and says what only
   retention removes; the notice (E4-20) says the same.
6. **Section 6.6**: retention "at course end plus 90 days" for logs and the workspace. Those
   stores take a period, not a date; E4-18 keeps 90 days from each record, so everything is gone
   by course end plus 90 days and some of it earlier.
7. **Section 8.1** says `spikes/` holds the measurement records; sections 4.7, 8.4 and 14 put them
   under `docs/measurements/`, which E1-11 uses.
8. **Sections 5.4 and 8.6** disagree on the upgrade pool's quota (open question 7).
9. **ADR 0001, amendment 3** says the engine "already has the outcome" for a merge that waits for
   the owner's review. It does not: a refused merge goes back to verification three times, then
   waits as a GitHub failure. E0-25 builds the wait.

## Open questions and readings for the owner

What the current drafts still leave open, in one list. Items 1 to 13 need an answer; the drafts
hold a placeholder or a choice that may be wrong. Items 14 to 40 are readings the drafts took
where the design is silent; each is stated in the issue's Context and can be changed there.

### To answer

1. **How the verification reaches the internal load balancer** (E2-64, E2-44; phase 1 exit).
   Either a verification build inside the network whose security group the internal load balancer
   admits (amends section 6.5; a new issue), or the exit criterion reads "at the gateway's hop"
   (amends sections 4.1 and 14). Until then those tests stay deselected. The same item reads the
   exit's "scaler's scenarios pass in the verification" as the scenarios passing in the synth
   step, and proves "a student key works end to end" with the admin-issued canary key, not a key
   from a login.
2. **Actions permission of the factory App** (E0-03). The engine re-runs a failed CI job through
   `rerun-failed-jobs`, which needs Actions write; E0-03 and green-light step 7 grant read. Grant
   write, or accept that a re-run fails and the issue waits.
3. **The owner's own pull requests on protected paths, with a yes to decision 8** (E0-15, E0-25;
   E0-06 and E0-08, and green-light steps 10 and 11, have the owner change `loop.toml` through a
   pull request of their own). With code-owner review required and an empty bypass list, GitHub
   never lets an author approve their own pull request, so the owner could not land that change.
   Options: the owner pushes and the factory App opens the pull request (as E0-23 does for
   workflow files); the owner on the bypass list, which weakens control 4; or a no to decision 8.
4. **Does token counting spend the student's budget?** (E2-15, E2-24, E2-26). `POST
   /v1/messages/count_tokens` is a forwarded route, so as drafted it reserves and settles like a
   generation. Claude Code counts often, and charging a whole transcript per count would multiply
   a session's charge. Exempt it (it stays authenticated and rate-limited), or charge it.
5. **Does vLLM's Messages usage already include cached input?** (E2-21; E3-21). E2-21 follows
   Anthropic's definition, where uncached, cache-written and cache-read input add up. If vLLM
   reports the whole input in the uncached field as well, cached input is charged twice. A capture
   of E3-21, or the S3 run, should settle it before phase 2.
6. **Content blocks the request profile does not handle** (E2-17, E3-26). Blocks Anthropic's
   reference lists and vLLM refuses (search results, server-tool blocks), and a document given by
   URL (refused as non-inline media), stay in the history and make every later turn fail. Keep
   them as drafted, or drop or replace them as E3-26 does for documents.
7. **Quota for the upgrade pool** (E5-03, E4-33). Section 5.4 asks quota for one instance more
   than `max_instances`; section 8.6 asks 96 more G-family vCPUs, exactly one `g7e.24xlarge`.
   Request 192, or state that the rule does not apply to a scheduled-only pool with no warm
   instance.
8. **The launch hook before S6 measures an upgrade start** (E5-02, E2-40). Section 17 gives 15
   minutes for the coursework pool only. E5-02's heartbeats extend the hook once per completed
   stage, so each stage of a 186 GB start gets 15 minutes and a slower stage is abandoned. A
   larger default for multi-GPU pools?
9. **Disk for the upgrade pool's fallback loader** (E3-40, E3-44, E5-03). Every GPU pool gets a
   150 GB volume; the 186 GB model cannot be downloaded to it if S4 or S6 forces the download
   loader. Size the volume per pool?
10. **The upgrade pool's engine settings** (E5-03, E3-22). The design gives parsers, context and
    served name only. The drafts set `target_concurrency` 32 (the estimate's highest stream count)
    and a largest output of 32,000, and leave cache precision, step budget and draft head at
    vLLM's defaults.
11. **Two sessions on one day** (E4-33, E4-02). Section 4.7's check that the daily ceiling covers
    a session is made per window; two windows on one day share one daily ceiling. Check the sum?
12. **Day boundaries** (E2-15, E2-51, E3-30, E4-01, E4-02). The student's budget and the
    issuances count UTC days; the GPU-hour ceilings count days in the schedule's time zone. The
    affinity cookie lives on the student's usage item of the day, so each student pays one full
    prefill after midnight UTC. One boundary for all?
13. **Homework hours** (decision 5, second half). No item asks for them. They set the monthly
    GPU-hour ceiling (E4-02) and the budget (E4-04).

### Readings the drafts took

14. **Where a pool's entry lives** (E1-03, E2-69, E2-71, E3-10, E5-03). Section 4.2 says a new
    model is "a new pool entry and a merge". The drafts keep each entry in `config.example.toml`
    for tests and synthesis, and build only the pools the owner has written to SSM: a merge alone
    changes no deployed pool, and a model change is a merge plus an owner write.
15. **Defaults the design does not give**, each chosen to leave the deployed stage as it was:
    `https_listener` false (E2-47), `parked` false (E4-17), `cost_anomaly_detection` false
    (E4-36), `pipeline_events_retention_days` 90 (E4-30), a monthly ceiling of 150 for every pool
    but the mock, which has none (E4-02).
16. **Allowed instance types** (E2-69). The list defaults to the types the configured pools name,
    so the hard cap of section 11.3 binds only once the owner writes a list.
17. **The price limit covers fallback types** (E2-68). Every listed type is held to the $5 limit.
    If only the primary type was meant, one deliverable changes.
18. **Cost Anomaly Detection off by default** (E4-36). An account holds one AWS-managed services
    monitor, and an existing one would fail the deployment; the owner removes it, then turns the
    flag on. The design gives no threshold, so every anomaly alerts.
19. **The gateway's own fault** (E2-29, E4-10, E4-16): a 5xx that names no cause of section 6.2,
    plus the public load balancer's own 5xx, over the load balancer's request count.
20. **Pipeline failures are sent by E0-22's SNS message**, not a CloudWatch alarm (E4-10): the
    pipeline has no fixed name a stage's alarm could match.
21. **Go or no-go** (E4-06, E4-16): go means every pool the window names has the window's number
    of instances in service; the in-session gauge counts the window itself, not the pre-warm lead.
22. **The speed objective's scope** (E4-16): every pool that runs a GPU engine; the design states
    it for the coursework pool.
23. **The dry run reports the expected values without judging on them** (E4-26); the verdict is
    the objectives and the ceilings.
24. **A session window must name its pools** (E4-33); the design does not say what a window that
    names none would pre-warm.
25. **An admin's start of a scheduled-only pool is a window up to an end time** (E5-01), treated
    like a session window, so the idle rule cannot stop a slow start part-way. For S6 the owner
    opens and closes it by hand (E5-05).
26. **The deployment verdict** (E0-07, E0-08, E0-09): a timeout of 3600 s by default (the owner
    sets at least 1800); a rejected approval, or one that expires after seven days, is a failure
    and takes the broken-main path, fix-forward then revert.
27. **A workflow change is certified again only on a conflict** (E0-23). If `main` moves without
    a conflict, the owner may merge the certified head as it is; the verification and the
    deployment verdict check what lands.
28. **What the reporter says when something is missing** (E0-12, E0-21, E2-64): the stack refers
    to the secret `rhi/reporter/github-app` and does not create it, and while it is absent nothing
    is posted; a missing canary key and a listener still off are reported as "not run", never as
    passed or failed. Measurements go under `measure/<id>` on the commit the job ran from (E0-14,
    E0-27).
29. **Build badges** (E0-24): CodeBuild, GitHub Actions and badge-service build-status images,
    matched as URLs with a scheme only, so prose that names a host stays clean.
30. **The wildcard invariant** (E1-07) covers identity policies, and only the action `*` counts as
    every action; resource policies name their own principals and are not checked.
31. **Numbers chosen in the drafts**: the key is 40 random letters and digits with a CRC-32 in 6
    characters (E2-01); the concurrency lease is 2,280 s (E2-15); upstream timeouts are 10 s to
    connect, 60 s to write, 1,200 s to read, 10 s for a pooled connection, 5 s for an idle one
    (E2-24); `rhi wait` gives up after 20 minutes (E2-55); the gateway's health check is `GET
    /v1/models`, expecting 401 (E2-90).
32. **Keep-alives only after a hold** (E2-24, E2-26). A stream that was never held gets none while
    the engine computes a long transcript.
33. **Reasoning effort and system messages** (E2-17, E3-24, E3-25). The Messages effort field is
    taken from Anthropic's reference; a value outside the mapping (such as `minimal`) is left as
    sent, which the template refuses; a system message mid-conversation becomes a user message in
    place, and a developer message is treated like it. The captures of E3-21 confirm the shapes.
34. **Budget defaults belong to the admin CLI** (E2-56): 100 million tokens and 4 requests are
    copied into roster and key items, not stage configuration; changing a student's budget
    rewrites their active keys.
35. **The lifecycle agent** (E2-33, E2-38, E2-70) runs in a privileged container and acts on the
    host through `nsenter`; the boot program is bash and uses the machine image's AWS CLI. The
    mock pool's machine image is an AWS-published arm64 Amazon Linux 2023 image that has Docker
    (E2-71); whether it has the AWS CLI shows only in E2-40's deployed check.
36. **Pool values** (E2-69, E3-10): the mock pool has a maximum of 2 (S8 needs two), one warm
    instance, `target_concurrency` 8 and a 262,144-token context; the mock and coursework pools
    have a largest output of 32,000, Claude Code's own, so its requests are not clamped.
37. **The 4-bit build** (E3-34, E3-41, E3-19): its repository is taken from vLLM's recipe;
    running a spike on it is the owner's change of the pool's model in SSM.
38. **Spike mechanics** (E3-13, E3-15, E3-19, E3-33): S1 stops the pool by idleness; S8 removes
    an instance through the scaler's scale-in; repetition counts are inputs; the S5 presets are one
    session per student hands-on and two with several agents; the weekly canary runs Sundays at
    04:00 UTC and is not held by session windows.
39. **Grafana** (E2-59, E2-60, E2-80, E2-85, E4-28): served under `/grafana/` on the endpoint's
    host; dashboards built at synthesis from the deployed configuration; the session view as rows
    and links inside the inference dashboard, keeping six dashboards; MFA by authenticator app
    only.
40. **Toolchain** (E1-16, E1-17, E3-16, E3-17): functions run Python 3.13 with their own boto3;
    SSM holds String parameters only; libraries and harnesses are pinned at the newest release at
    least seven days old; the S3 solver is HiGHS; `scripts/check.sh synth` bundles functions from
    the package index and so needs the network, which no test does.

## For whoever runs the loop

1. **Before publishing.** Decide decision 8 first: with a no, delete E0-15 and E0-25. Publishing
   creates 233 issues (6 epics and 227 sub-issues), 227 sub-issue links, about 600 dependency links and their labels, over a thousand
   writes; GitHub's limits on content creation may stop the publisher part-way, and a second
   `scripts/backlog.sh publish --apply` creates only the rest.
2. **The host.** Its default Node is 26; `.node-version` names 24 (E1-01) and nothing enforces it,
   while the synthesis tests start whatever `node` is on the path. Set Node 24 before E1. Until
   E0-03 lands, the fine-grained token of green-light step 7 cannot read check runs of a private
   repository: run under the owner's login, or land E0-03 first.
3. **E0, one issue at a time.** Run alone, before E1 (`/implement <n>` for each): E0-02, E0-03,
   E0-04, E0-06, E0-07, E0-29, E0-19, E0-20, E0-23, E0-28, E0-18, E0-24. They touch the engine and
   its configuration. E0-02 lands before E1-08 and E1-09 create the paths it protects. After E1-10:
   the reporter chain E0-09 to E0-14, E0-22, E0-27. After E2: E0-08, E0-21, E0-26, E0-05. E0-16
   needs only E2-01. With a yes to decision 8: E0-25, then E0-15. `/implement E0` as a whole would
   pull about 65 issues of E1 and E2 in through E0-05, E0-08, E0-21 and E0-26.
4. **Workflow files the owner lands.** E0-17 (`.github/workflows/ci.yml`, after E0-23 and E0-28)
   and E4-25 (`.github/workflows/advisories.yml` and `.github/dependabot.yml`, after E0-17 and
   E0-23). The engine certifies the change, pushes nothing and posts the push command; the owner
   pushes, the engine opens the pull request, the owner merges it. Do not use GitHub's button that
   brings a branch up to date: the head it makes is not certified. Until E0-17 is merged, the
   `host:container-runtime` items of E2-65 and E2-86 stay with the owner; after it, E0-19
   discharges them from the `container-tests` job.
5. **Owner steps before a merge can deploy, or before a phase can show its exit:**
   - Phase 0: request the G-family quota on day one (E1-13); bootstrap (E1-08); write the
     configuration to SSM (E1-13); deploy the pipeline stack from a laptop and authorize the
     connection (E1-10); create the Apps and the secret `rhi/reporter/github-app` (E1-18, E0-12),
     without which no status is posted; then list the deployment contexts in `loop.toml` through
     a pull request of the owner's own (E0-08). Until then the engine waits for no deployment.
   - Phase 1: add the mock pool to SSM (E2-71), or no pool is built; set `https_listener` to true
     once the domain resolves (E2-47), or the verification runs no test; run the canary command
     before E2-64 merges, and enter the roster (E2-56); approve widened permissions in the
     console, often in phases 1 to 3. A waiting approval releases the landing claim (E0-07), but
     after seven days it expires and the merge is reverted.
   - Phase 2: write the coursework pool's first image parameter by hand, then add the pool to SSM
     with `default_pool` set to it (E3-10); add its request profile (E3-27); create the spike
     students' keys before the first paid spike (E3-14); start the spike jobs (E3-45); switch the
     pool's model to the 4-bit build for the S3 and S5 runs (E3-41, E3-19).
   - Phase 3: write the session windows after E4-33 merges; set the ceilings (E4-02) and the
     monthly amount (E4-04); confirm the topics' subscriptions and set up text messages (E4-36,
     E0-22, E4-06); turn Cost Anomaly Detection on (E4-36). Do not park before E4-38 has landed.
   - Phase 4: raise the quota (E5-03); write the upgrade pool's image parameter, then add the pool
     with its ceilings (E5-03); open and close the admin window for S6 (E5-05).
6. **What stays with the owner after a merge.** 114 acceptance items name a capability: 39 need a
   deployed environment, 19 a GPU, 21 the account, 5 the console, 27 a decision, 3 a container
   runtime. Only E2-64's first exit item and the three container items are discharged by the
   engine (E0-08, E0-19); issues whose other items need the account end `delivered`, not `done`.
7. **Protected files.** Each construct issue names its own acknowledgement file,
   `tests/policy/acknowledged/<area>_<module>.toml` (the convention of E1-05 and E1-07), and each
   pipeline step names `src/rhi/pipeline/stack.py`, so the builder needs no approval for them. A
   change to any other protected file goes through the examiner's approval. E1-04 and E0-28 both
   change `scripts/check.sh` and name it.
8. **Two sentences to keep in any edit.** E0-20 and E0-04 run the scanner of the engine's own
   checkout, never the branch's copy: the engine runs them outside the sandbox, as the owner.
9. **Paths outside the layout of section 8.1**, on purpose: `tools/examiner-pilot/` (E0-05),
   `tools/measurements/` (E0-14), `tools/advisories/` (E4-25), `setup/bootstrap/` (E1-08) and
   `setup/owner-setup.sh` (E1-13).
