+++
id = "E4-35"
title = "Every row of personal data expires by course end plus 90 days"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E1-17", "E2-15", "E2-30", "E2-51", "E2-56", "E2-76"]
+++

## Context

Retention is set on every store, not assumed: DynamoDB expiry, among others, at course end plus 90
days (section 6.6). The usage rows, the roster and the keys are personal data. Every table already
has an expiry attribute, expires_at (issues E2-49 and E2-67); this issue makes every writer of those
three tables set it. Logs and the workspace are issue E4-18.

A key's own validity ends at course end (section 6.4), which is earlier: a key row may expire then,
or later, but never after course end plus 90 days. The course's end date is configuration (issue
E1-03), a required key the owner's configuration in SSM already holds.

**How each writer learns the date.** The issuer already receives it as RHI_COURSE_END (issue E2-52).
The admin CLI reads no configuration yet; it reads the date from the configuration in SSM with the
configuration loader's ssm source (issue E1-17) and the admin's own credentials. The gateway does not
have it either: its settings are the variables of
issue E2-76, read once at start. So the gateway's deployment (issue E2-30) gives the task the
variable RHI_COURSE_END, the configuration's course end date as YYYY-MM-DD, and the gateway's
settings reader requires it, like the other variables the deployment always
fills in. Both change in this issue, so no deployment runs a gateway that needs a variable its task
lacks.

The tests hold no student key: a key a test needs is generated at run time by the key module of issue
E2-01, because the hygiene scanner reports a literal key anywhere in the tree (issue E0-16).

## Deliverables

- [ ] `services/common/rhi_common/retention.py` computes, from the course's end date, the expiry of personal rows: course end plus 90 days, in the form DynamoDB's expiry reads.
- [ ] `src/rhi/gateway/service.py` gives the gateway's task the variable RHI_COURSE_END, the configuration's course end date as YYYY-MM-DD, and `services/gateway/rhi_gateway/settings.py` reads it, a required variable, which `services/gateway/rhi_gateway/main.py` hands to the usage writer.
- [ ] `services/gateway/rhi_gateway/budget.py` sets the expiry on every usage row it writes, from that date, and `admin/rhi_admin/roster.py` on every roster row it writes, from the date `admin/rhi_admin/main.py` reads from the configuration in SSM with the configuration loader's ssm source.
- [ ] `services/issuer/rhi_issuer/mint.py` and `admin/rhi_admin/keys.py` set on every key row an expiry no later than that date.
- [ ] Every cdk-nag finding `src/rhi/gateway/service.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/gateway_service.toml`.
- [ ] `tests/unit/test_retention.py` covers the date and each writer against stubbed tables, `tests/unit/gateway/test_settings.py` the new variable, and `tests/synth/test_gateway_service.py` the task's environment.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_retention.py` passes: for a course that ends on 15 January 2027 the expiry is 15 April 2027, every usage and roster row written carries it, and every key row written by the issuer or the admin CLI carries an expiry no later than it.
- [ ] `uv run --locked pytest tests/unit/gateway/test_settings.py` passes: an environment without RHI_COURSE_END raises an error that names it, and one with 2027-01-15 gives that date.
- [ ] `tests/synth/test_gateway_service.py::test_the_environment_is_what_the_gateway_reads` passes with RHI_COURSE_END in the task's environment, equal to the example configuration's course end date.

## References

- `docs/design/system-design.md`, sections 6.2, 6.4, 6.6
