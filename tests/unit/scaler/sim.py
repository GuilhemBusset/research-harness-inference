"""A simulated pool for the scaler's scenarios (system design, section 5.1).

The scenarios of section 5.1 are sequences over time, not one call of the decision function. This
module plays them: a clock in whole seconds, one pool's Auto Scaling group, the gateway tasks and
the instances' lifecycle agents that write heartbeats, and the scaler, which is the real
`rhi_scaler.decide.decide` called on what the group and the heartbeats hold. The simulator decides
nothing itself: each run builds `Inputs` the way the scaler's edge reads them, calls the function,
and applies its `PoolDecision` to the group.

What the group does, as section 5 describes it:

- An instance taken from the warm pool comes into service `warm_start_seconds` after it was
  started, a cold launch after `cold_start_seconds`. Every instance in service is protected from
  scale-in.
- An instance that never becomes healthy is abandoned by the launch hook after
  `launch_hook_seconds` (15 minutes) and terminated; the group then launches a replacement, while
  launches are not suspended.
- When the desired capacity is below the instances counted (in service and starting), the group
  releases instances whose protection is lifted, and only those. A released instance is
  deregistered at once, so it takes no new request, and drains: once its engine reports nothing
  running, the termination hook completes and it returns to the warm pool.

What a gateway task does: it rewrites its heartbeat row every 5 seconds, until it stops. A request
it receives goes to the instance in service with the fewest requests; with none in service it is
held, the task rewrites its row and calls the scaler, and it repeats the call every 15 seconds while
it holds. A held request is forwarded once an instance is in service, and fails after the pool's
`hold_seconds`. An instance's lifecycle agent writes its row every 5 seconds while it serves.

The schedule calls the scaler at every whole minute.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rhi_common.pool_config import Pool
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode, is_fresh
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Supply

HEARTBEAT_SECONDS = 5
SCHEDULE_SECONDS = 60
WAKE_REPEAT_SECONDS = 15
LAUNCH_HOOK_SECONDS = 15 * 60
IMAGE_DIGEST = "sha256:" + "ab" * 32


@dataclass
class Request:
    """One request: received by a gateway task at `received_at`, it runs `seconds` on an instance."""

    gateway: str
    received_at: int
    seconds: int
    instance: str | None = None
    ends_at: int | None = None
    outcome: str | None = None  # "served" or "failed", once it is over


@dataclass
class Instance:
    name: str
    state: str  # "warm", "starting", "in-service" or "draining"
    started_at: int = 0
    ready_at: int = 0
    healthy: bool = True
    protected: bool = False


@dataclass
class Gateway:
    task: str
    reporting: bool = True
    held: list[Request] = field(default_factory=list)
    last_request_at: int | None = None
    last_wake_at: int | None = None
    row: GatewayHeartbeat | None = None


@dataclass(frozen=True)
class Run:
    """One run of the scaler: when, who called it, and what it decided."""

    now: int
    caller: str  # "schedule" or the gateway task that held a request
    decision: PoolDecision


@dataclass(frozen=True)
class Launch:
    now: int
    instance: str
    from_warm: bool


class Sim:
    """One pool, its group, its gateways and its scaler, from the time `start` on."""

    def __init__(
        self,
        pool: Pool,
        *,
        start: int,
        gateways: tuple[str, ...] = ("task-1",),
        warm: int = 1,
        in_service: int = 0,
        warm_start_seconds: int = 120,
        cold_start_seconds: int = 300,
        healthy: bool = True,
        mode: Mode = Mode.ENABLED,
    ) -> None:
        self.pool, self.mode, self.now = pool, mode, start
        self.warm_start_seconds, self.cold_start_seconds = warm_start_seconds, cold_start_seconds
        self.healthy = healthy  # whether a launch ever becomes healthy
        self.instances: dict[str, Instance] = {}
        self.desired = in_service
        self.launches_suspended = False
        self.memory = Memory()
        self.gateways = {task: Gateway(task) for task in gateways}
        self.instance_rows: dict[str, InstanceHeartbeat] = {}
        self.requests: list[Request] = []
        self.runs: list[Run] = []
        self.launches: list[Launch] = []
        self.released: list[tuple[int, str]] = []
        # The group's activity history: (time, "abandoned" or "in-service").
        self.history: list[tuple[int, str]] = []
        self._count = 0
        for _ in range(warm):
            self._new("warm")
        for _ in range(in_service):
            self._new("in-service", protected=True)
        self._heartbeats()

    # What the scenarios do.

    def request(self, gateway: str, seconds: int) -> Request:
        """A request received now by a gateway task."""
        task = self.gateways[gateway]
        request = Request(gateway, self.now, seconds)
        self.requests.append(request)
        task.last_request_at = self.now
        if not self._forward(request):
            task.held.append(request)
            self._wake(task)
        return request

    def stop_gateway(self, gateway: str) -> None:
        """The gateway task stops: it writes no heartbeat again."""
        self.gateways[gateway].reporting = False

    def advance(self, seconds: int) -> None:
        """Let `seconds` pass, one second at a time."""
        for _ in range(seconds):
            self.now += 1
            self._second()

    # What is observed.

    def in_service(self) -> list[str]:
        return self._named("in-service")

    def starting(self) -> list[str]:
        return self._named("starting")

    def warm(self) -> list[str]:
        return self._named("warm")

    # The scaler.

    def run_scaler(self, caller: str = "schedule") -> PoolDecision:
        """One run: the inputs as the edge reads them, the decision, and its effect on the group."""
        supply = Supply(
            desired=self.desired,
            in_service=tuple(self.in_service()),
            starting=tuple(self.starting()),
            draining=tuple(self._named("draining")),
            warm=tuple(self.warm()),
            running=tuple(name for name, i in sorted(self.instances.items()) if i.state != "warm"),
        )
        rows = [task.row for task in self.gateways.values() if task.row is not None]
        pool = PoolInputs(
            config=self.pool,
            mode=self.mode,
            supply=supply,
            gateway_heartbeats=tuple(row for row in rows if is_fresh(row, self.now)),
            instance_heartbeats=tuple(row for row in self.instance_rows.values() if is_fresh(row, self.now)),
            abandoned_launches=self._abandoned_in_a_row(),
            memory=self.memory,
        )
        (decision,) = decide(Inputs(now=self.now, pools=(pool,))).pools
        self.runs.append(Run(self.now, caller, decision))
        for name in decision.lift_protection:
            if name in self.instances:
                self.instances[name].protected = False
        self.desired = decision.desired
        self.launches_suspended = decision.suspend_launches
        self.memory = decision.memory
        self._reconcile()
        return decision

    # The rest is the simulation.

    def _named(self, state: str) -> list[str]:
        return sorted(name for name, instance in self.instances.items() if instance.state == state)

    def _new(self, state: str, *, protected: bool = False) -> Instance:
        self._count += 1
        instance = Instance(f"instance-{self._count}", state, protected=protected)
        self.instances[instance.name] = instance
        return instance

    def _on(self, instance: str) -> list[Request]:
        return [r for r in self.requests if r.instance == instance and r.outcome is None]

    def _abandoned_in_a_row(self) -> int:
        """Abandoned launches since the last launch that reached service, from where the memory counts."""
        count = 0
        for at, outcome in reversed(self.history):
            if outcome != "abandoned" or (
                self.memory.abandons_counted_from is not None and at < self.memory.abandons_counted_from
            ):
                break
            count += 1
        return count

    def _reconcile(self) -> None:
        """What Auto Scaling does to bring the instances counted to the desired capacity."""
        counted = self.in_service() + self.starting()
        if len(counted) > self.desired:
            # Only an instance whose protection is lifted is released.
            for name in [n for n in counted if not self.instances[n].protected][: len(counted) - self.desired]:
                self.instances[name].state = "draining"
                self.released.append((self.now, name))
        for _ in range(self.desired - len(counted)):
            if self.launches_suspended:
                break
            warm = self.warm()
            instance = self.instances[warm[0]] if warm else self._new("starting")
            delay = self.warm_start_seconds if warm else self.cold_start_seconds
            instance.state, instance.started_at, instance.ready_at = "starting", self.now, self.now + delay
            instance.healthy, instance.protected = self.healthy, True
            self.launches.append(Launch(self.now, instance.name, bool(warm)))

    def _forward(self, request: Request) -> bool:
        """Forward to the instance in service with the fewest requests, if there is one."""
        serving = self.in_service()
        if not serving:
            return False
        request.instance = min(serving, key=lambda name: (len(self._on(name)), name))
        request.ends_at = self.now + request.seconds
        return True

    def _wake(self, task: Gateway) -> None:
        """A gateway that holds a request rewrites its row, then calls the scaler."""
        task.last_wake_at = self.now
        if task.reporting:
            self._write(task)
        self.run_scaler(task.task)

    def _write(self, task: Gateway) -> None:
        in_flight = sum(1 for r in self.requests if r.gateway == task.task and r.instance and r.outcome is None)
        task.row = GatewayHeartbeat(
            task.task, self.pool.name, in_flight, len(task.held), task.last_request_at, self.now
        )

    def _heartbeats(self) -> None:
        for task in self.gateways.values():
            if task.reporting:
                self._write(task)
        for name in self.in_service():
            running = len(self._on(name))
            self.instance_rows[name] = InstanceHeartbeat(
                name, self.pool.name, running, 0, IMAGE_DIGEST, "rev-1", "serving", self.now
            )

    def _second(self) -> None:
        now = self.now
        for instance in list(self.instances.values()):
            if instance.state == "starting" and instance.healthy and now >= instance.ready_at:
                instance.state = "in-service"
                self.history.append((now, "in-service"))
            elif instance.state == "starting" and now >= instance.started_at + LAUNCH_HOOK_SECONDS:
                # The launch hook times out and abandons it; the instance is terminated.
                del self.instances[instance.name]
                self.history.append((now, "abandoned"))
            elif instance.state == "draining" and not self._on(instance.name):
                # The termination hook completes: back to the warm pool.
                instance.state, instance.protected = "warm", False
                self.instance_rows.pop(instance.name, None)
        self._reconcile()
        for request in self.requests:
            if request.outcome is None and request.ends_at is not None and now >= request.ends_at:
                request.outcome = "served"
        for task in self.gateways.values():
            if not task.reporting:
                continue
            for request in list(task.held):
                if self._forward(request):
                    task.held.remove(request)
                elif now - request.received_at >= self.pool.hold_seconds:
                    request.outcome = "failed"
                    task.held.remove(request)
            if task.held and task.last_wake_at is not None and now - task.last_wake_at >= WAKE_REPEAT_SECONDS:
                self._wake(task)
        if now % HEARTBEAT_SECONDS == 0:
            self._heartbeats()
        if now % SCHEDULE_SECONDS == 0:
            self.run_scaler()
