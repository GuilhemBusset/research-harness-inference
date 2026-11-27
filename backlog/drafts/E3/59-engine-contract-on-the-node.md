+++
id = "E3-59"
title = "The release carries the engine contract suite, and the document rhi-engine-contract runs it on an instance against its own engine"
labels = ["size:M", "kind:infra"]
epic = 5
+++

## Context

Issue #149's last item, "On a GPU instance of the coursework pool, vLLM started with these
arguments passes the contract suite of `tests/contract/` (needs gpu:capacity)", has no way to be
run. The suite is an HTTP client of an engine, not of the gateway (`tests/contract/conftest.py`:
`RHI_ENGINE_URL`, `RHI_ENGINE_NAME`, `RHI_POOL_SECRET`, "such as `http://127.0.0.1:8000` on its
node"), and nothing deployed can reach a deployed engine but the gateway.

**Why not through the internal load balancer.** Two of the suite's four rows cannot pass there, by
design. The load balancer forwards the four inference routes and answers 404 to everything else,
the health route included (section 4.1; `rhi.pools.routing`), so `test_health` and `test_metrics`
fail through it; and the contract serves `/metrics` on localhost only (section 4.1). The load
balancer's group admits the gateway's tasks alone (`rhi.network.security`; section 6.5, "only the
gateway reaches the internal ALB; only the internal ALB reaches port 8000"), and section 4.1 says
that opening the network to a test would weaken the rule it tests. A job in the VPC that reached
an instance's port 8000 directly would break the second half of the same sentence, and would be a
new reader of the pool's secret. So the suite runs where its own configuration places it: on the
node, against `http://127.0.0.1:8000`.

**What the node needs, and has.** The engine runs with the host's network (`rhi-engine.service`),
so the loopback address reaches it. The instance role reads its own pool's secret, and the
engine's unit shows how (`aws secretsmanager get-secret-value` with `RHI_SECRET_ID` and
`RHI_REGION` of `/run/rhi/release.env`, the value handed to the container on its standard input
only). The release's settings name the engine's row of metric names as `RHI_ENGINE`. The host
holds the lifecycle agent's Python image (`rhi-lifecycle.service`, by digest). The boot program
unpacks the whole release archive into `/opt/rhi/release/` after checking its digest
(`services/engine/boot/rhi-boot.sh`), so a folder added to the archive reaches every instance with
nothing downloaded on the node. The suite's own libraries are pure Python (pytest, httpx,
prometheus-client, starlette, uvicorn), as the agent's are, and its two modules import only
`rhi_common`, `rhi_gateway.sse` and `rhi_mock_engine` of this project.

**How it is started.** Instances have no SSH; "Debugging is through Session Manager with a custom
minimal policy" (section 4.3), whose agent also runs Systems Manager's Run Command. One Command
document of the pools stack, `rhi-engine-contract`, with no parameter, runs the release's own
script and nothing else: whoever may send it can run the contract check on an instance, and no
other command. AWS's Systems Manager documentation ("Reference: ec2messages, ssmmessages, and
other API operations") says that the agent, from version 3.3.40.0, takes Run Command's requests
through `ssmmessages` when it can, the channel of Session Manager, whose five actions the instance
role already holds (`rhi.pools.role`). So this issue gives the role nothing: the builder reads that
page, and if it asks more of an instance's role, the pull request says what, and the role is
widened by an issue of its own, not here. The deployed runs below show whether it holds. Who may
send the document beyond the account's own administrator is issue E3-60's question; this issue
grants no role the right.

**What leaves the node.** Run Command keeps a command's output in the account. The script prints
only a fixed vocabulary, one line per test, `rhi-contract <test> <outcome>`, and a last line
`rhi-contract exit <status>`: nothing of pytest's own report, of the engine's answers, or of AWS's
text, so any reader of the output may copy it.

The four tests are `test_inference`, `test_health`, `test_metrics` and `test_bearer_secret` of
`tests/contract/test_engine_contract.py`: at an address, `test_shutdown` is not collected, and the
tests about the suite itself use the mock in process and say nothing of the node's engine.

Suggested branch: `feat/engine-contract-on-the-node`

## Deliverables

- [ ] `pyproject.toml` declares a dependency group `contract` holding the suite's libraries at the versions the other groups pin (pytest, httpx, prometheus-client, starlette, uvicorn), `uv.lock` is regenerated with it, and `src/rhi/packaging.py` bundles it as it bundles a service's group, with `tests/synth/test_packaging.py` holding each of its pins equal to the pin of every other group that holds the same library, `dev`'s pytest among them.
- [ ] `src/rhi/pools/release.py` puts in every pool's release archive a folder named contract, beside the agent's: the contract suite's folder as it is in the repository, the packages `rhi_common`, `rhi_gateway` and `rhi_mock_engine`, the libraries of the group `contract` at their locked versions, installed as the agent's are, and the files of `services/engine/contract/`; without libraries, as in the tests' synthesis, it holds everything but them, as the agent's folder does.
- [ ] `services/engine/contract/run.sh`, run as root on an instance, reads the release's settings from `/run/rhi/release.env`, reads the pool's secret as the start command of `rhi-engine.service` does, and runs the four tests of the Context from `/opt/rhi/release/contract/`, read-only, in the Python image `rhi-lifecycle.service` names, with the host's network, against `http://127.0.0.1:8000`, the engine's row being the release's `RHI_ENGINE`.
- [ ] `services/engine/contract/run.sh` hands the secret to the container on its standard input only, never in an argument, an `--env` option or a file, and prints on its standard output only the lines `rhi-contract <test> <outcome>`, one for each of the four tests, the outcome `passed`, `failed`, `error`, `skipped` or `missing`, then `rhi-contract exit <status>`, and exits with pytest's status.
- [ ] `src/rhi/pools/contract_check.py` defines one Systems Manager document of type Command named `rhi-engine-contract`, with no parameter, whose one step runs `/opt/rhi/release/contract/run.sh` with bash, since the archive gives no file the executable mode, under an execution timeout the module fixes, updated in place as a new version when its content changes, and `src/rhi/pools/stack.py` creates it once in the pools stack.
- [ ] `tests/unit/engine/test_contract_run.py` runs `services/engine/contract/run.sh` with bash against stand-ins for the host's `aws` and `docker` and a release folder made in the test, and asserts the secret on the container's standard input and in none of its arguments, the image, the network, the address and the engine's row the container is given, the five lines for a run where every test passed and for one where a test failed, and nothing else on standard output.
- [ ] `tests/synth/test_contract_check.py` asserts the document's name, type, absence of parameters and one command, and `tests/synth/test_pool_release.py` asserts that the release archive holds the folder contract with the suite, the three packages and the script.
- [ ] `docs/design/system-design.md` says in section 4.1 that the contract suite runs against a deployed engine on its own node, through the document `rhi-engine-contract`, and why not through the internal load balancer, and in section 4.3 that the release's archive carries the suite beside the agent.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_contract_run.py tests/synth/test_contract_check.py tests/synth/test_pool_release.py tests/synth/test_packaging.py tests/contract` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] `scripts/check.sh synth` exits 0 with every `AWS_*` variable unset.
- [ ] On an instance of a session of the mock pool, `aws ssm send-command --document-name rhi-engine-contract --targets Key=tag:rhi-pool,Values=mock` ends with the status `Success`, and the invocation's output is the line `rhi-contract <test> passed` for each of the four tests and `rhi-contract exit 0` (needs aws:deployed-environment).
- [ ] On an instance of a session of the coursework pool, the same command with `Values=coursework` ends with the status `Success` and the same five lines: vLLM started with the pool's arguments passes the contract suite of `tests/contract/`, issue #149's last item (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 4.3 and 6.5
- `tests/contract/conftest.py`
- `tests/contract/test_engine_contract.py`
- `services/engine/units/rhi-engine.service`, its start command
- `services/engine/units/rhi-lifecycle.service`
- `services/engine/boot/rhi-boot.sh`
- `src/rhi/pools/release.py`, `build_archive`
- `src/rhi/pools/role.py`, `SESSION_ACTIONS`
- `src/rhi/pools/routing.py`
- `src/rhi/network/security.py`
