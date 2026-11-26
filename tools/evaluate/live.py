"""The instruments of the operator's driver: what `controller.LiveRuntime` measures the selected,
already-deployed pool with, resolved for one invocation from the driver's own AWS session and from
this host (docs/design/agent-session-evaluation.md, "How the operator runs it").

`resolve` reads, for the scope's pool and nothing else:

- the pool's deployed settings, from the scaler's function (`rhi_admin.pools.deployed`): its
  engine and the instance type it runs first;
- the pool's current release, from its release parameter (`/rhi/pools/<pool>/release`, as
  `rhi.pools.group.release_parameter` names it):
  the engine image's digest, the model revision and the engine's flags, whose launch fingerprint
  (`rhi_common.launch_settings.fingerprint`) is the treatment's;
- the gateway's address and the metrics workspace's query address, the outputs of the gateway and
  the observability stacks;
- the canary students' keys, from the spikes' secret (`rhi_common.spike_keys`), each under its
  roster alias (`rhi_admin.spikes.spike_alias`);
- the trial image, prepared and verified on this host (`evaluate.image.prepare`), the harness
  workers it runs (`evaluate.workers.Workers`, disposed of with the invocation), the course's task
  files and graders.

The release every selected host must serve is the release parameter's image digest and model
revision; `releases` gives what each host's lifecycle agent reports of the same two, from the
workspace. The gateway is ready when its status says the pool is `ready`, as a canary student
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
from rhi_admin import main as admin_main
from rhi_admin import pools as admin_pools
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


def release_of(image_digest: str, model_revision: str | None) -> str:
    """The release a host serves, as the driver compares it: the engine image and the model."""
    return f"{image_digest}/{model_revision or 'none'}"


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

    admin = admin_main.connect(session)
    try:
        pool = admin_pools.deployed(admin.pools()).get(scope.pool)
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
        keys = spike_keys.read(admin.secrets).students
    except _UNREAD as error:
        raise Unresolved(Check.CANARIES) from error
    reader = common.Workspace(workspace_url, session.region_name, session.get_credentials())
    clock = replay.RunClock()
    kind = pool.instance_types[0]
    fingerprint = launch_settings.fingerprint(flags)
    try:
        requested = Settings.from_json(
            {one.name: None for one in dataclasses.fields(Settings)}
            | {
                "host_count": scope.instances,
                "instance_type": kind if kind in set(InstanceType) else None,
                "model_revision": revision,
                "engine_image_digest": image_digest,
            },
            "requested",
        )
    except ValueError as error:  # a `ContractError`: the release does not read as public settings
        raise Unresolved(Check.BINDING) from error
    source = phases.Telemetry(
        reader,
        scope.pool,
        pool.engine,
        telemetry.Requested(requested, None if fingerprint == launch_settings.UNAVAILABLE else fingerprint),
    )
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

    def releases() -> list[str]:
        span = telemetry.range_of(telemetry.ALLOWANCE_SECONDS)
        found: dict[str, str] = {}
        for series in reader.read_samples(f"{selector}[{span}]", at=time.time()):
            labels = series.labels
            if labels.get("instance"):
                found[labels["instance"]] = release_of(labels.get("image_digest", ""), labels.get("model_revision"))
        return list(found.values())

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
