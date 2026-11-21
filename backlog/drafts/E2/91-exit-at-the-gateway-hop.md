+++
id = "E2-91"
title = "Phase 1's exit at the gateway's hop: drop the smoke tests that call the internal load balancer directly"
labels = ["size:S", "kind:test"]
epic = 4
+++

## Context

Phase 1 exits when a student key works end to end against the mock pool, the scaler's scenarios
pass, and the forbidden routes are refused (section 14). The forbidden routes are now checked at
the gateway's hop only: the pipeline's verification calls them through the gateway and expects 404,
and the internal load balancer's own rules (the four inference routes forwarded, a fixed 404 for
everything else, the health route included) are asserted on the synthesized template (sections 4.1
and 14; ADR 0003). Only the gateway can reach that load balancer (section 6.5), so a test run from
the verification step, outside the private network, could never call it.

The verification (issue #141) deselects the five smoke tests that call the internal load balancer
directly, `tests/smoke/deployed/test_internal_routes.py`, by their IDs, the tuple DESELECTED of
`scripts/pipeline/verify.py`. Those tests are dropped. The tests of the gateway's hop
(`tests/smoke/test_smoke.py::test_a_forbidden_route_is_not_found`) stay, and so do the template
assertions that already hold the load balancer's rules and its security group:
`tests/synth/test_internal_routing.py` and `tests/synth/test_security_groups.py`.

The verification still leaves out, in its run on the mock pool, the tests that belong to the run on
the coursework pool (GPU_PASS_ONLY). `tests/unit/test_deploy_settings.py` holds the owner's table
`[deploy]` of the engine's configuration equal to the files of the tests the verification leaves
out, and names the internal-routes file as its example: it takes the file of the GPU-only test
instead. The engine's configuration itself is not changed here: its commented example of
unverified_tests is the owner's.

The reporter's allow-list of smoke-test IDs is the list of `tests/smoke/ids.py`, read at
synthesis (`src/rhi/pipeline/reporter.py`), so the five IDs leave it with the tests and nothing
else changes there.

## Deliverables

- [ ] `tests/smoke/deployed/test_internal_routes.py` is deleted, and `tests/smoke/ids.py` no longer lists the IDs internal-invocations, internal-pause, internal-tokenize, internal-metrics and internal-health.
- [ ] `scripts/pipeline/verify.py` has no tuple DESELECTED: in the run on the mock pool it deselects the tests of GPU_PASS_ONLY and no other, and its docstring says that the forbidden routes are checked at the gateway's hop and the internal load balancer's rules on the synthesized template.
- [ ] `tests/unit/test_verify_script.py` checks that the run on the mock pool deselects exactly the tests of GPU_PASS_ONLY and that a run naming a pool deselects none.
- [ ] `tests/unit/test_deploy_settings.py` takes as its example of a file the verification leaves out the file that holds the GPU-only smoke test, and still checks the files the verification's own run leaves out.

## Acceptance criteria

- [ ] `test ! -e tests/smoke/deployed/test_internal_routes.py` exits 0.
- [ ] `uv run --locked pytest tests/unit/test_verify_script.py tests/unit/test_deploy_settings.py tests/smoke/test_smoke.py` passes.
- [ ] `uv run --locked pytest tests/synth/test_internal_routing.py tests/synth/test_security_groups.py` passes: four POST rules per pool, a default 404, no rule for the health route, and only the gateway's security group admitted by the internal load balancer's.
- [ ] The pipeline's verification of the first deployment after this merge runs every smoke test of the mock pool's run but the GPU-only one, the forbidden routes at the gateway's hop included, and passes, which is the forbidden-route half of phase 1's exit (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.1, 6.5 and 14
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 6
- `scripts/pipeline/verify.py`
