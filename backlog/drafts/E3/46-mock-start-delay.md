+++
id = "E3-46"
title = "The mock pool's start delay: an optional pool field the release passes to the mock as a flag"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E2-42", "E2-82", "E3-38"]
+++

## Context

Spike S2 asks whether each client survives a 7-minute held stream and a 4-minute held call (section
12; issue E3-16). The mock pool is enough for it, if the deployed mock takes that long to become
ready: the gateway holds a request until the pool has a healthy target (section 5.3), and the mock's
health route answers 200 only once its start delay has passed (issue E2-31). Nothing yet carries a
start delay from the configuration to the deployed mock.

**The field.** A pool may give start_delay_seconds, a whole number of seconds that only a pool whose
engine is mock may give. The design gives no such setting; its default is 0, the mock's behaviour
today, so that a configuration in SSM written before this issue loads unchanged and the deployed
mock pool stays as it is (rule of issue E1-03 for a field added later).

**How it reaches the mock.** A pool's release carries its engine flags, a list of arguments (issue
E2-33), and the engine's unit gives them to the engine's container as its arguments, in order (issue
E3-38). So `src/rhi/pools/release.py` writes, as the mock pool's engine flags, the two arguments
--start-delay-seconds and the field's value when the field is above 0, and no flag at 0: the mock
pool's release, and with it the trigger of its instance refresh (issue E2-42), is unchanged under a
configuration that leaves the field out. The mock's image runs the mock with the container's
arguments added to its command, not in place of it, and the mock's entry point reads its start delay
from that argument; the mock's other settings are set as issue E2-31 sets them.

**For the spike's run.** The owner sets the field on the mock pool in the configuration in SSM; the
next deployment changes the mock pool's release and refreshes its instances; after the run, the owner
removes the field. That is an obligation of issue E3-16, which runs the spike.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives a pool the optional field start_delay_seconds, a whole number of seconds, 0 by default, and refuses it, naming the pool and the field, when it is negative or given by a pool whose engine is not mock; `config.example.toml` documents the field and its default in a comment beside the mock pool and leaves it unset.
- [ ] `src/rhi/pools/release.py` writes, as the mock pool's engine flags, the arguments --start-delay-seconds and the field's value when the field is above 0, and no flag when it is 0.
- [ ] The mock's entry point, in `services/engine/mock/rhi_mock_engine/`, takes its start delay from the argument --start-delay-seconds when it is started with one, and `services/engine/mock/Dockerfile` adds the container's arguments to the mock's command, so that a container started with no argument runs as before.
- [ ] Every cdk-nag finding `src/rhi/pools/release.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_release.toml`.
- [ ] `tests/unit/common/test_pool_config.py` covers the field, `tests/synth/test_pool_release.py` the flags in the release, and `tests/unit/engine/test_mock.py` the argument.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a mock pool without the field reads 0, one that gives 400 reads 400, and a negative value, or the field on a vllm pool, is refused with an error that names the pool and the field.
- [ ] `tests/synth/test_pool_release.py::test_the_mock_start_delay_is_a_flag` finds the flags --start-delay-seconds and 400 in the release of a mock pool that gives 400, and the example configuration's mock pool's release unchanged, with no flag.
- [ ] `tests/unit/engine/test_mock.py::test_the_start_delay_argument` starts the mock's entry point with the arguments --start-delay-seconds and 2 and finds the health route answering 503 before 2 seconds have passed and 200 after.
- [ ] `tests/unit/common/test_pool_config.py::test_the_first_deployed_pool_still_parses` still passes.

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 5.3, 12
