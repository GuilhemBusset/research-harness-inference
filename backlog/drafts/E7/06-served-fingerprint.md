+++
id = "E7-06"
title = "Expose a public fingerprint of the engine arguments actually launched"
labels = ["size:M", "kind:app"]
epic = "E7"
+++

## Context

Current lifecycle telemetry identifies the image and model revision. Config-only experiments also need evidence bound to the running launch. The deployment environment can place private locators in arguments.

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/`, `services/engine/units/`, `services/engine/boot/` and `services/common/rhi_common/` bind an allow-listed normalized settings fingerprint to the engine launch actually in service and expose it with lifecycle telemetry where available.
- [ ] The normalization contract covers evaluation-relevant engine arguments, omits private values and permits existing releases and metrics consumers to keep serving when launch evidence is unavailable.
- [ ] `tests/unit/lifecycle/test_serving.py` and `tests/unit/lifecycle/test_launch.py` cover the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tests/unit/lifecycle/test_serving.py` and launch-path tests show different step budgets yield distinct fingerprints at the same image and model revision, and changing only desired settings cannot relabel the running engine.
- [ ] `uv run --locked pytest tests/unit/lifecycle` verifies old or unknown launch evidence is unavailable and no account-specific argument value appears in samples.

## References

- `docs/design/agent-session-evaluation.md`, Reproducing and comparing runs
- `services/engine/units/rhi-engine.service`
- `services/engine/lifecycle/rhi_lifecycle/serving.py`
