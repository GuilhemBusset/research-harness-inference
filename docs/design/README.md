# Design

The design of the inference endpoint for the coding-agent cursus.

| Document | What it is |
| --- | --- |
| [`system-design.md`](system-design.md) | The design: AWS services, how the system works, why it should work, what it should serve, cost, risks, rollout, and the decisions that need the owner. Start here. |
| [`evidence.md`](evidence.md) | The source behind every `[E n]` reference in the design, and how far each claim was verified. |

**Status:** proposed on 2026-11-13. The decisions that wait for the owner are in section 15 of the design.

Reading order for a decision: section 0 of the design (summary and decisions at a glance), then section 15 (decisions for the owner), then sections 4.2 and 4.7 (which model, and what it should serve), then section 12 (what is evidence and what is still an assumption).

Once a decision in the design is accepted or changed, record it as an ADR under [`../adr/`](../adr/) and update the design. The design describes the system as intended; the ADRs record why.
