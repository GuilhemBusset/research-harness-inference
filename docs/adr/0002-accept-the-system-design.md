# ADR 0002 — Accept the system design

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** Guilhem Busset (repo owner)
- **Context:** [`docs/design/system-design.md`](../design/system-design.md) and its evidence file;
  the backlog written from it (epics #2 to #7)

## Context

The system design describes the inference endpoint: the model and the pools that serve it, the
scaler, the gateway and student login, observability, delivery through CodePipeline, the trust
boundary, cost and the rollout in five phases. The backlog was written from it, one epic per
phase plus the factory's own. Section 15 lists twelve decisions for the owner. Some carry a
recommendation or a choice the design already makes; the others ask for a value or a choice the
design cannot make alone.

## Decision

The system design is accepted as the basis of the implementation.

- **Where section 15 recommends an answer, or the design already chooses one, that answer is
  accepted** until the owner decides otherwise: decision 2 (Qwen3.8-27B on one 96 GB GPU as the
  coursework model, which is also the "cheap model first"), decision 7 (the custom gateway),
  decision 8 (changes to the pipeline, the workflows, the gates and the policy tests wait for the
  owner's approval), decision 9 (US East unless students' data must stay in the EU) and
  decision 11 (Grafana OSS behind the load balancer's sign-in).
- **Where it asks for a value or a choice the design does not make, the owner gives it when the
  work needs it**: decision 1 (dates), 3 (scheduled sessions and the upgrade model's licence),
  4 (the harnesses that must work), 5 (class size and homework), 6 (money), 10 (domain) and
  12 (continuity). Each backlog issue that needs one carries it as an owner item.
- The questions the backlog review left are answered in issue #236. An answer that changes the
  design is recorded as a new ADR, and the design is updated with it.

## Consequences

- The implement loop may build every epic as written. No issue waits on this acceptance.
- The owner items that carry a decision stay the owner's obligations, listed by the loop when it
  closes the issue that carries them.
- The design keeps describing the system as intended; this record says why it became the plan.
