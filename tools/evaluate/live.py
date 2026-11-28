"""The instruments of the operator's driver: what `controller.LiveRuntime` measures the selected,
already-deployed pool with, resolved for one invocation from the driver's own AWS session and from
this host (docs/design/agent-session-evaluation.md, "How the operator runs it").

`resolve` reads, for the scope's pool and nothing else:

- the pool's deployed settings, from the scaler's function of the pools stack
  (`rhi_admin.main.pool_access`, `rhi_admin.pools.deployed`): its engine, the instance type it
  runs first, and its gateway profile, the part of its entry that changes the body the gateway
  forwards (`request_profile`, `reasoning_effort`, `cache_salt_scope`), requested as the digest
  of its canonical JSON (`gateway_profile_digest`);
- the pool's current release, from its release parameter (`/rhi/pools/<pool>/release`, as
  `rhi.pools.group.release_parameter` names it):
  the engine image's digest, the model revision and the engine's flags, whose launch fingerprint
  (`rhi_common.launch_settings.fingerprint`) is the treatment's. The flags are read as that
  contract reads them (`flag_settings`): the step budget, the context limit, the tensor
  parallelism, the cache's precision and prefix caching are requested where the flags give them
  and never filled with vLLM's defaults; any other flag is declared by the fingerprint alone, and
  flags the contract does not know request no fingerprint and none of these settings;
- the gateway's address and the metrics workspace's query address, the outputs of the gateway and
  the observability stacks;
- the canary students' keys, from the spikes' secret (`rhi_common.spike_keys`), each under its
  roster alias (`rhi_admin.spikes.spike_alias`);
- the trial image, prepared and verified on this host (`evaluate.image.prepare`), the harness
  workers it runs (`evaluate.workers.Workers`, disposed of with the invocation), the course's task
  files and graders.

The booking reaches the pools through `pool_sessions`: the pools stack and `session_max_hours`
alone. Neither it nor `resolve` looks up the students' tables or reads the whole configuration.

The release every selected host must serve is the release parameter's image digest and model
revision; `releases` gives what each host's lifecycle agent reports of the same two in its latest
scrape, from the workspace (`current_releases`): a host whose latest scrape says two releases
serves none the driver admits. The gateway is ready when its status says the pool is `ready`, as a canary student
reads it. A lookup that cannot be made is `Unresolved`, naming the preflight check it fails: the
controller reports it and creates, books and sends nothing. Nothing resolved here is printed or
written: the keys, the addresses and the identifiers stay in the driver's process.

What the load driver records of itself (`contracts.Run`): the checkout's commit, the lock's digest,
the trial image's ID, this host's processors and memory, and two settings of the driver's
environment: `RHI_EVALUATE_DRIVER_IMAGE_DIGEST`, the image a driver that runs in one is in (this
checkout's driver runs beside the trial image, whose ID it records otherwise), and
`RHI_EVALUATE_PATH_CATEGORY`, where it stands (`public_internet` unless given).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

import common
from evaluate import image, phases, replay, telemetry, trials, workers
from evaluate.contracts import Family, InstanceType, PathCategory, Run, Settings
from rhi import config
from rhi_admin import main as admin_main
from rhi_admin import pools as admin_pools
from rhi_admin import session as admin_session
from rhi_admin.roster import AdminError
from rhi_admin.spikes import spike_alias
from rhi_common import launch_settings, spike_keys

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from evaluate.contracts import TrialIdentity
    from evaluate.controller import Instruments, Scope
    from evaluate.plan import Plan

GATEWAY_STACK, GATEWAY_OUTPUT = admin_main.GATEWAY_STACK_NAME, "Endpoint"
OBSERVABILITY_STACK, WORKSPACE_OUTPUT = admin_main.OBSERVABILITY_STACK_NAME, "WorkspaceUrl"
# What the gateway stack's output says while the endpoint has no HTTPS listener.
NO_ENDPOINT = "none"
READY = "ready"
# What `current_releases` gives for a host whose latest scrape is ambiguous: never a release.
AMBIGUOUS = "ambiguous"
DRIVER_IMAGE_VARIABLE = "RHI_EVALUATE_DRIVER_IMAGE_DIGEST"
PATH_CATEGORY_VARIABLE = "RHI_EVALUATE_PATH_CATEGORY"
LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
MIB = 1024 * 1024
# A trial worker's processors and processes: the suite sets its wall time and its folders.
WORKER_CPU_MILLIS, WORKER_PIDS = 2000, 512
_UNREAD = (AdminError, ClientError, BotoCoreError, KeyError, ValueError, TypeError)


class Unresolved(Exception):
    """A lookup of the scope's deployment, or of this host, that could not be made: `check` is
    the preflight check it fails (`controller.Check`)."""

    def __init__(self, check: str) -> None:
        super().__init__(check)
        self.check = check


def _output(session: Any, stack: str, key: str) -> str | None:
    answer = session.client("cloudformation").describe_stacks(StackName=stack)
    outputs = answer["Stacks"][0].get("Outputs", [])
    return next((one["OutputValue"] for one in outputs if one.get("OutputKey") == key), None)


def pool_sessions(session: Any) -> admin_session.Sessions:
    """The pools' sessions as the booking reads and writes them, from the session: the pools
    (`rhi_admin.main.pool_access`) and the one configuration key the booking needs,
    `session_max_hours`, read by its own name (its default when the stage does not set it). No
    student table is looked up and no other configuration is read. The time zone and the default
    pool serve the admin's own commands, never an evaluation, which names its pool."""
    name = config.SSM_PREFIX + "session_max_hours"
    try:
        text = session.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise
        hours = config.DEFAULTS["session_max_hours"]
    else:
        key = config.KEYS["session_max_hours"]
        hours = key.parse(key.from_text(text))
    if type(hours) is not int:
        raise TypeError("session_max_hours")
    return admin_session.Sessions(
        pools=admin_main.pool_access(session), time_zone="UTC", session_max_hours=hours, default_pool=None
    )


# The settings of `contracts.Settings` an engine's flags give, by the option's name as
# `launch_settings.normalize` reads it; the cache's precision as telemetry reads the engine's own.
FLAG_SETTINGS = {
    "max-num-batched-tokens": "step_budget_tokens",
    "max-model-len": "context_limit_tokens",
    "tensor-parallel-size": "tensor_parallelism",
    "kv-cache-dtype": "kv_dtype",
    "enable-prefix-caching": "prefix_caching",
}
KV_DTYPES = telemetry.EFFECTIVE["kv_dtype"][1]


def flag_settings(flags: Sequence[str]) -> dict[str, object]:
    """The requested settings a release's engine flags give: its launch fingerprint and the
    settings of `FLAG_SETTINGS`, each None where the flags leave its option out, and all of them
    None for flags the launch-settings contract does not know."""
    normalized = launch_settings.normalize(flags)
    found: dict[str, object] = dict.fromkeys(["launch_fingerprint", *FLAG_SETTINGS.values()])
    if normalized is None:
        return found
    found |= {name: normalized.get(option) for option, name in FLAG_SETTINGS.items()}
    if found["kv_dtype"] is not None:
        found["kv_dtype"] = KV_DTYPES[str(found["kv_dtype"])]
    return found | {"launch_fingerprint": launch_settings.fingerprint(flags)}


def gateway_profile_digest(pool: Any) -> str:
    """The digest of the deployed pool entry's gateway profile: the SHA-256 of the canonical JSON
    of its `request_profile`, `reasoning_effort` and `cache_salt_scope`."""
    profile = {
        "request_profile": list(pool.request_profile),
        "reasoning_effort": pool.reasoning_effort,
        "cache_salt_scope": pool.cache_salt_scope,
    }
    text = json.dumps(profile, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def release_of(image_digest: str, model_revision: str | None) -> str:
    """The release a host serves, as the driver compares it: the engine image and the model."""
    return f"{image_digest}/{model_revision or 'none'}"


def current_releases(answer: Sequence[common.Series]) -> list[str]:
    """The release each host reports now, one entry a host: its series sampled last. A host whose
    latest scrape says two releases at once says none the driver can use (`AMBIGUOUS`), whatever
    order the workspace answered in."""
    latest: dict[str, tuple[float, set[str]]] = {}
    for series in answer:
        host = series.labels.get("instance")
        if not host or not series.samples:
            continue
        at = max(sample[0] for sample in series.samples)
        said = release_of(series.labels.get("image_digest", ""), series.labels.get("model_revision"))
        before = latest.get(host)
        if before is None or at > before[0]:
            latest[host] = (at, {said})
        elif at == before[0]:
            before[1].add(said)
    return [next(iter(said)) if len(said) == 1 else AMBIGUOUS for _, said in latest.values()]


def worker_limits(plan: Plan) -> workers.Limits:
    """A trial worker's limits: the suite's wall time and folders, its memory four times what
    the folders may take, so that they never take more than half of it."""
    estimates = plan.spec.estimates
    scratch, output = estimates.trial_workspace_mib * MIB, estimates.trial_evidence_mib * MIB
    return workers.Limits(
        wall_seconds=math.ceil(plan.workload.limits.trial_wall_time_ms / 1000),
        memory_bytes=4 * (3 * scratch + output),
        cpu_millis=WORKER_CPU_MILLIS,
        pids=WORKER_PIDS,
        scratch_bytes=scratch,
        output_bytes=output,
    )


def course_task(root: Path) -> Any:
    """What a trial is given: its instance's files, materialized under `root` by its course
    module, the task's prompts, and its local tools."""

    def task(trial: TrialIdentity) -> trials.Task:
        module: Any = phases.COURSE_MODULES[Family(trial.instance_id.split(".")[0])]
        inputs = root / f"{trial.instance_id}-{trial.trial}"
        module.materialize(trial.instance_id, inputs)
        if hasattr(module, "prompts"):
            prompts = tuple(module.prompts())
        else:
            prompts = tuple(one.read_text(encoding="utf-8") for one in sorted((module.HERE / "task").glob("*.md")))
        return trials.Task(inputs, prompts, dict(getattr(module, "TOOLS", {})))

    return task


def _sealed() -> tuple[Path, ...]:
    found: list[Path] = []
    for module in phases.COURSE_MODULES.values():
        sealed = getattr(module, "SEALED", ())
        found += [sealed] if isinstance(sealed, Path) else list(sealed)
    return tuple(found)


def _run_record(image_id: str, environ: Mapping[str, str]) -> Run:
    from evaluate.controller import source_commit

    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // MIB
        return Run(
            ordinal=0,
            source_commit=source_commit(),
            lock_digest="sha256:" + hashlib.sha256(LOCK.read_bytes()).hexdigest(),
            harness_image_digest=image_id,
            driver_image_digest=environ.get(DRIVER_IMAGE_VARIABLE) or image_id,
            driver_cpus=os.cpu_count() or 1,
            driver_memory_mib=memory,
            path_category=PathCategory(environ.get(PATH_CATEGORY_VARIABLE) or PathCategory.PUBLIC_INTERNET),
        )
    except (OSError, ValueError) as error:
        raise Unresolved("measurement-unavailable") from error


def resolve(
    scope: Scope, plan: Plan, session: Any, environ: Mapping[str, str], stack: contextlib.ExitStack
) -> Instruments:
    """The instruments of the scope's pool (the module's text). The harness workers are disposed
    of when `stack` closes, with the invocation."""
    from evaluate.controller import Check, Instruments

    try:
        pools = admin_main.pool_access(session)
    except _UNREAD as error:
        # No client of the driver's session can be made, or the pools stack cannot be read.
        raise Unresolved(Check.DEPLOYMENT) from error
    try:
        pool = admin_pools.deployed(pools).get(scope.pool)
    except _UNREAD as error:
        raise Unresolved(Check.POOL_LIMITS) from error
    if pool is None:
        raise Unresolved(Check.BINDING)
    try:
        endpoint = _output(session, GATEWAY_STACK, GATEWAY_OUTPUT)
        workspace_url = _output(session, OBSERVABILITY_STACK, WORKSPACE_OUTPUT)
        parameter = session.client("ssm").get_parameter(Name=f"/rhi/pools/{scope.pool}/release")
        current = json.loads(parameter["Parameter"]["Value"])
        image_digest = "sha256:" + str(current["engine_image"]).rsplit("@sha256:", 1)[1]
        revision, flags = current.get("model_revision"), [str(one) for one in current["engine_flags"]]
    except (*_UNREAD, IndexError) as error:
        raise Unresolved(Check.BINDING) from error
    if not endpoint or endpoint == NO_ENDPOINT or not workspace_url:
        raise Unresolved(Check.BINDING)
    try:
        keys = spike_keys.read(session.client("secretsmanager")).students
    except _UNREAD as error:
        raise Unresolved(Check.CANARIES) from error
    reader = common.Workspace(workspace_url, session.region_name, session.get_credentials())
    clock = replay.RunClock()
    kind = pool.instance_types[0]
    try:
        requested = Settings.from_json(
            {one.name: None for one in dataclasses.fields(Settings)}
            | flag_settings(flags)
            | {
                "host_count": scope.instances,
                "instance_type": kind if kind in set(InstanceType) else None,
                "model_revision": revision,
                "engine_image_digest": image_digest,
                "gateway_profile_digest": gateway_profile_digest(pool),
            },
            "requested",
        )
    except ValueError as error:  # a `ContractError`: the release does not read as public settings
        raise Unresolved(Check.BINDING) from error
    source = phases.Telemetry(reader, scope.pool, pool.engine, telemetry.Requested(requested, requested.launch_fingerprint))
    try:
        prepared = image.prepare()
    except (OSError, RuntimeError, ValueError) as error:
        raise Unresolved(Check.HARNESS_VERSIONS) from error

    def trial_image(command: Sequence[str]) -> str:
        return image.run_docker(["run", "--rm", "--network=none", prepared.image_id, *command])

    run = _run_record(prepared.image_id, environ)
    root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rhi-evaluate-")))
    held = stack.enter_context(workers.Workers(root / "workers", sealed=_sealed()))
    selector = telemetry.selectors(scope.pool, pool.engine)[telemetry.Source.RELEASE]

    span = telemetry.range_of(telemetry.ALLOWANCE_SECONDS)

    def releases() -> list[str]:
        return current_releases(reader.read_samples(f"{selector}[{span}]", at=time.time()))

    status = common.Gateway(endpoint, scope.pool, keys[0])
    return Instruments(
        gateway=replay.Gateway(endpoint, scope.pool, clock),
        canaries=phases.Canaries(keys, tuple(spike_alias(number) for number in range(1, len(keys) + 1))),
        telemetry=source,
        artifacts=phases.Artifacts(run, trial_image),
        harnesses=trials.WorkerHarnesses(
            plan,
            upstream=common.gateway_address(endpoint),
            pool=scope.pool,
            image=prepared.image_id,
            limits=worker_limits(plan),
            start=held.start,
            tasks=course_task(root / "inputs"),
            clock=clock.now_ms,
        ),
        graders=trials.course_graders(),
        gateway_ready=lambda: status.state() == READY,
        releases=releases,
        release=release_of(image_digest, revision),
        source_commit=run.source_commit,
    )
