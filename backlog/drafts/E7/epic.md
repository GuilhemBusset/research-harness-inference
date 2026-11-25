+++
id = "E7"
kind = "epic"
title = "Evaluate class capacity and agent task quality on operator-booked pools"
labels = ["kind:test"]
+++

Give the operator a single evaluation skill invocation to establish how many students a serving configuration supports and which coursework their agents finish. The same experiment compares host counts, model builds or vLLM settings at matched loads. An experiment issue retains the reference, candidate, and comparison scorecards.

The work covers seeded traffic through the student gateway, live course tasks with independent graders, verified host telemetry, bounded booking and cleanup, offline evidence and durable GitHub reporting. Launching an evaluation is an operator action. Model deployment stays in the existing deployment workflow.

The scope and numerical protocol are in `docs/design/agent-session-evaluation.md`. The evidence for existing one-host capacity is in `docs/measurements/s5.md`. The epic is complete when its sub-issues close, including the operator's live proof.
