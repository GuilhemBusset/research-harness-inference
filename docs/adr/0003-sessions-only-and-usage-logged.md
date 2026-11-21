# ADR 0003 — Sessions only, usage logged and not enforced

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** Guilhem Busset (repo owner)
- **Context:** [`docs/design/system-design.md`](../design/system-design.md), section 15, and the
  open questions of issue #236; amends [ADR 0002](0002-accept-the-system-design.md)

## Context

ADR 0002 accepted the system design with its recommended answers and left the other decisions of
section 15 to the owner. The design then planned for homework between sessions, a per-student daily
token budget with reserve-and-settle, GPU-hour ceilings and a budget latch that stop pools, a session
schedule in configuration with pre-warm and a go or no-go notice, and deployments held around each
scheduled window. Phase 1 was built with the budget. Issue #236 asked, among others, how the
pipeline's verification should reach the internal load balancer, whether token counting spends a
student's budget, and what to do with content blocks the engine refuses.

## Decision

1. **Sessions only.** The endpoint serves students only during class sessions, which the owner
   switches on and off with one record (section 5.6). For homework, and whenever the endpoint is
   down or not ready, students use their own subscriptions to hosted models; nothing is built to
   stand in (decisions 1, second half, 5, second half, and 12). The whole endpoint is switched on
   and off with `parked`.
2. **No enforcement of usage or cost.** No per-student token budget, no GPU-hour ceilings, no
   budget latch, no session schedule in configuration. The autoscaler stays, zero to a maximum, and
   serves students only while a session is on. The gateway logs each request's token counts and
   the other numbers of section 6.2; a token count (`count_tokens`) is answered and logged as a
   count, never charged. The fleet-size cap of `tests/policy/` stays. The cost net is AWS Budgets
   e-mail alerts at thresholds set at deployment; nothing stops automatically (decision 6, first
   half). Deployments wait while a session is on (decision 8, second half).
3. **The budget code already merged is removed after the first deployment.** Phases 0 and 1 are
   deployed as built; a later change removes the reservation and settlement, the two causes
   answered 402 and the mode `budget-latched`, keeping the concurrency lease and the affinity
   cookie that share their writes.
4. **Harnesses:** Claude Code, Codex, an SDK script, OpenCode and Pi (decision 4).
5. **Confirmed:** Qwen3.8-27B at 8 bits on one 96 GB GPU (decision 2), the custom gateway
   (decision 7), self-hosted Grafana behind the load balancer's sign-in (decision 11). The upgrade
   pool waits for phase 2's measurements and is built only if coursework instances cannot carry a
   session.
6. **Phase 1's exit at the gateway's hop.** The pipeline's verification calls the forbidden routes
   through the gateway; the internal load balancer's rules are asserted on the synthesized
   template. The smoke tests that call the internal load balancer directly are dropped (sections
   4.1 and 14).
7. **Blocks the engine refuses** (a search result, the blocks of the tools Anthropic's service
   runs, a document given by URL) are replaced by a line of text, as documents already are
   (section 6.2, step 4).
8. **Spikes that need AWS:** the job runs the scenario and records raw measurements with their
   times. For a spike whose job is not yet built, the pass or fail reading is written after the
   first real run, from its data (section 12).
9. **Trust boundary:** no owner review in the merge path (decision 8, first half; section 8.4).

## Consequences

- Section 15 lists what is settled and what stays open: the dates, the upgrade model's licence,
  the class size, the monthly amount and thresholds of the alerts, the privacy notice's signatory
  and the domain.
- New work is drafted under `backlog/drafts/` for the session switch, the deployment hold, the
  usage records, the cost alerts, the gateway's error-rate alarm, the blocks replaced by text, the
  exit at the gateway's hop, and OpenCode and Pi. Issues written for the earlier plan (schedules,
  ceilings, the latch, the class-day notice) are closed or rewritten on GitHub.
- A student who reaches the endpoint between sessions gets `no_session` at once and works with
  their own subscription.
