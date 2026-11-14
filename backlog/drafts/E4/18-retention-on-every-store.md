+++
id = "E4-18"
title = "Retention set on every log group of the stage and the workspace, and no per-student CloudWatch metric"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E2-12", "E2-30", "E2-48", "E2-52", "E2-57", "E2-60"]
+++

## Context

What is kept is usage metadata per student, and source addresses in load balancer, WAF and gateway
logs; all of it is personal data (section 6.6). Retention is set on every store, not assumed:
DynamoDB expiry, CloudWatch log retention and the Prometheus workspace's retention, all at course
end plus 90 days. CloudWatch metrics cannot be deleted for 15 months, so nothing per-student goes
there (evidence E27).

Rows carry an expiry date, so they expire exactly at course end plus 90 days (issue E4-35). Logs and
the workspace do not take a date: their retention is a period counted from when each record is
written. A period of 90 days is what makes "course end plus 90 days" hold for them: whatever was
written before course end is gone 90 days after it, and nothing is kept longer than 90 days. The
course is about nine weeks (assumption A2), so a month-to-date panel still has its month. The load
balancers' access logs, in a bucket, are issue E4-37.

**A log group that no template holds is kept forever.** A Lambda function that has no log group
creates one at run time, with no retention, outside every template. So every function of the stage
logs to a log group of its own stack, named in the function's logging configuration, and that log
group has the retention. This holds for the functions CDK adds for its custom resources too. A
function added later is covered by the same aspect, and the test catches one that escapes it.

**Which headers carry a key.** A student's key arrives in `Authorization: Bearer` or in `x-api-key`
(section 6.2, step 2): the web ACL's logs redact those two headers.

**Which dimension identifies a student.** A dimension or a metric filter's dimension named alias,
student or github_id, whatever its case; the per-student series live in Prometheus only.

The pipeline's stack is outside the stage: its logs hold no student data, and its own log group's
retention is issue E4-30's. This issue's aspect and test cover the stage's stacks.

This issue creates `tests/policy/test_retention.py`. `tests/policy/` is a protected path (ADR 0001,
decision 10): the builder may write this file because this issue names it.

## Deliverables

- [ ] `src/rhi/retention.py`, applied to every stack of the stage from `src/rhi/stage.py`, gives every function of the stage, the functions of custom resources included, a log group of its stack named in the function's logging configuration.
- [ ] The same aspect gives every log group of the stage a retention of 90 days.
- [ ] `src/rhi/network/waf.py` makes the web ACL log to a log group of its stack whose name starts with aws-waf-logs-, with the headers authorization and x-api-key redacted.
- [ ] `tests/policy/test_retention.py` fails, on the stage's templates, when a log group has no retention or one longer than 90 days, when a function's logging configuration names no log group of its stack, when the workspace keeps data longer than 90 days, and when a CloudWatch metric, metric filter or alarm has a dimension that identifies a student.
- [ ] Every cdk-nag finding `src/rhi/retention.py`, `src/rhi/network/waf.py` or `src/rhi/stage.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/retention.toml`, `tests/policy/acknowledged/network_waf.toml` or `tests/policy/acknowledged/stage.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_retention.py tests/synth/test_waf.py` passes on the app as it stands, with the web ACL's log group and its two redacted headers.
- [ ] `tests/policy/test_retention.py::test_detects_a_log_group_kept_forever` fails a template, built in the test, with a log group that never expires, and one with a function whose logging configuration names no log group.
- [ ] `tests/policy/test_retention.py::test_detects_a_student_dimension` fails a template, built in the test, with an alarm whose metric has an alias dimension.

## References

- `docs/design/system-design.md`, sections 6.2, 6.6, 10
- `docs/design/evidence.md`, E27
