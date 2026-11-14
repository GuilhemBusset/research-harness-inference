+++
id = "E2-52"
title = "Deploy the key issuer behind the load balancer, throttled"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-05", "E1-16", "E2-47", "E2-49", "E2-51"]
+++

## Context

The key issuer is a Lambda behind the public load balancer, with privileges separate from the data
path (section 2). It is throttled (section 6.4): reserved concurrency, a WAF rate rule on its path
(issue E2-48), malformed input rejected before any call to GitHub, a limit on issuances per student.
Its routes all live under `/auth/`.

**Configuration.** Two keys are added, each optional with the default of section 17:
`issuer_reserved_concurrency`, 5, which sets the function's reserved concurrency, and
`key_issuances_per_day`, 5, which reaches the function as the environment variable
RHI_ISSUANCES_PER_DAY (issue E2-51). The function also receives RHI_CLIENT_ID and RHI_COURSE_END,
from the configuration's login app client ID and course end date (issue E1-03), and RHI_ROSTER_TABLE
and RHI_KEYS_TABLE, the names of the tables of issue E2-49 (issue E2-50). The login app's client ID
is discoverable but kept out of the repository (section 10); the owner creates the app and writes
the configuration (section 8.6, steps 3 and 4).

**Its rule and its logs.** The public load balancer has a listener only while the configuration's
key https_listener is true (issue E2-47), so the function's target group and its rule are created
only then. The listener's rules need distinct priorities: the issuer's is 20, beside the gateway's
10 (issue E2-90) and Grafana's 30 (issue E2-60); the design gives none. The function logs to a log
group of the access stack with the fixed name `/rhi/issuer`, named in its logging configuration, so
that Grafana's role names it without a reference to this stack (issue E2-60).

**Packaging.** The function's code and its libraries are built into an asset from `uv.lock` for the
function's architecture, with no Docker, by the packaging module of issue E1-16. The issuer runs on
arm64, the cheaper of the two architectures, which its group's libraries do not constrain.

Spike S9 asks whether a GitHub App with no permissions and no installation completes the device
flow; it needs no GPU and comes first (section 12). If it fails, the fallback is an OAuth app with
the phishing risk documented, or admin-issued keys (issue E2-56).

This issue changes `tests/policy/acknowledged/`, a protected path (ADR 0001, decision 10); the
builder may write the file named below because this issue names it.

## Deliverables

- [ ] `src/rhi/access/issuer.py` deploys the issuer as a function on arm64 packaged by issue E1-16, with the reserved concurrency issuer_reserved_concurrency gives, reached through one rule of the public load balancer's listener, at priority 20, for the path prefix /auth/, and logging to the log group named /rhi/issuer; `src/rhi/access/stack.py` holds it.
- [ ] The function's target group and its rule exist only when the public load balancer has a listener, which it has only while https_listener is true (issue E2-47); with the key false the function deploys with neither and nothing reaches it from the internet.
- [ ] Its role may read the roster table and read and write the keys table; it may not read the usage table, a pool's secret or the configuration.
- [ ] Its environment holds RHI_CLIENT_ID, RHI_COURSE_END, RHI_ISSUANCES_PER_DAY, RHI_ROSTER_TABLE and RHI_KEYS_TABLE, from the configuration and the stage; no template synthesized from the repository's example holds a real client ID.
- [ ] `src/rhi/config.py` gains two optional keys, issuer_reserved_concurrency and key_issuances_per_day, both 5 by default; `config.example.toml` gives each at its default, and `tests/unit/test_config.py` holds them.
- [ ] Every cdk-nag finding `src/rhi/access/issuer.py` or `src/rhi/access/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/access_issuer.toml` or `tests/policy/acknowledged/access_stack.toml`.
- [ ] `tests/synth/test_issuer.py` checks the function, its rule, its environment and its role.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_issuer.py` passes: a reserved concurrency of 5 under the example configuration, one rule at priority 20 for the paths under /auth/ and for no other path, the log group /rhi/issuer in the function's logging configuration, the five environment variables, and a role limited to the two tables.
- [ ] `uv run --locked pytest tests/unit/test_config.py` passes with the two keys at 5 when a source leaves them out, and `config.example.toml` giving each.
- [ ] `scripts/check.sh synth` exits 0 with the issuer's function in the stage.
- [ ] In the account, a roster member completes a login with a GitHub App that has no permissions and no installation, and the issuer reads their numeric user ID: this is spike S9, recorded as a measurement record (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 2, 6.4, 8.6, 10, 12, 17
- `docs/design/evidence.md`, E17
