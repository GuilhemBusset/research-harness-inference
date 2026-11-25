"""A simulated pool for the scaler's scenarios (system design, section 5.1).

The scenarios of section 5.1 are sequences over time, not one call of the decision function. This
module plays them: a clock in whole seconds, one pool's Auto Scaling group, the pool's session, the
instances' lifecycle agents that write heartbeats, and the scaler, which is the real
`rhi_scaler.decide.decide` called on what the group and the heartbeats hold. The simulator decides
nothing itself: each run builds `Inputs` the way the scaler's edge reads them, calls the function,
and applies its `PoolDecision` to the group as the edge does.

What the group does, as section 5 describes it:

- Every start is the launch of a new instance, which comes into service `start_seconds` after it
  was launched. An instance is protected from scale-in from the moment it enters service, and not
  before.
- AWS may have no capacity: with `capacity` set, the group holds at most that many instances. A
  launch that finds none is not abandoned: the group asks again every second until one comes.
- An instance that never becomes healthy is abandoned by the launch hook after
  `LAUNCH_HOOK_SECONDS` (15 minutes) and terminated, which takes `TERMINATE_SECONDS`. The group
  launches its replacement once it is gone, while launches are not suspended.
- When the desired capacity is below the instances counted (in service and starting), the group
  releases instances in service whose protection is lifted, and only those. What it does with an
  instance still starting is not documented: with `takes_starting` it terminates it first, and
  without it leaves it to enter service.
- A released instance is deregistered at once, so it takes no new request, and drains: it is
  terminated once its engine runs nothing, and at the latest `drain_seconds` after its release,
  which cuts the requests it still runs.

A request goes to the instance in service it names, or to the one with the fewest requests, and
runs there for its seconds. An instance's lifecycle agent writes its row every 5 seconds while it
serves. The gateway is not simulated: no rule reads what it reports.

The schedule calls the scaler at every whole minute, and `write_session` calls it once more, as
the admin's command does when it has written a session.
"""

from __future__ import annotations

from dataclasses import dataclass

from rhi_common.pool_config import Pool
from rhi_common.records import InstanceHeartbeat, Mode, Session, is_fresh
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Supply

HEARTBEAT_SECONDS = 5
SCHEDULE_SECONDS = 60
LAUNCH_HOOK_SECONDS = 15 * 60
TERMINATE_SECONDS = 60
IMAGE_DIGEST = "sha256:" + "ab" * 32


@dataclass
class Request:
    """One request: received at `received_at`, it runs `seconds` on an instance."""

    received_at: int
    seconds: int
    instance: str | None = None
    ends_at: int | None = None
    outcome: str | None = None  # "served", "cut" or "failed", once it is over


@dataclass
class Instance:
    name: str
    state: str  # "starting", "in-service", "draining" or "terminating"
    started_at: int = 0
    ready_at: int = 0
    gone_at: int = 0  # when a terminating instance is gone, and a draining one at the latest
    healthy: bool = True
    protected: bool = False


@dataclass(frozen=True)
class Run:
    """One run of the scaler: when, who called it, the supply it read and what it decided."""

    now: int
    caller: str  # "schedule" or "admin"
    supply: Supply
    decision: PoolDecision


@dataclass(frozen=True)
class Launch:
    now: int
    instance: str


class Sim:
    """One pool, its group, its session and its scaler, from the time `start` on."""

    def __init__(
        self,
        pool: Pool,
        *,
        start: int,
        in_service: int = 0,
        session: Session | None = None,
        start_seconds: int = 300,
        healthy: bool = True,
        capacity: int | None = None,
        takes_starting: bool = True,
    ) -> None:
        self.pool, self.now = pool, start
        self.session = session
        self.start_seconds = start_seconds
        self.healthy = healthy  # whether a launch ever becomes healthy
        self.capacity = capacity  # how many instances AWS has room for; `None` for any number
        self.takes_starting = takes_starting
        self.instances: dict[str, Instance] = {}
        self.desired = in_service
        self.launches_suspended = False
        self.memory = Memory()
        self.instance_rows: dict[str, InstanceHeartbeat] = {}
        self.requests: list[Request] = []
        self.runs: list[Run] = []
        self.launches: list[Launch] = []
        # The instances in service the group released, and the starting ones it took: (time, name).
        self.released: list[tuple[int, str]] = []
        self.taken: list[tuple[int, str]] = []
        # The group's activity history: (time, "abandoned" or "in-service").
        self.history: list[tuple[int, str]] = []
        self._count = 0
        for _ in range(in_service):
            self._new("in-service", protected=True)
        self._heartbeats()

    # What the scenarios do.

    def request(self, seconds: int, instance: str | None = None) -> Request:
        """A request received now, for the instance in service named or the one with the fewest."""
        request = Request(self.now, seconds)
        self.requests.append(request)
        serving = self.in_service()
        if instance is None and serving:
            instance = min(serving, key=lambda name: (len(self._on(name)), name))
        if instance in serving:
            request.instance, request.ends_at = instance, self.now + seconds
        else:
            request.outcome = "failed"
        return request

    def write_session(self, session: Session) -> PoolDecision:
        """The pool's session record is written, and the scaler runs once more."""
        self.session = session
        return self.run_scaler("admin")

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

    # The scaler.

    def run_scaler(self, caller: str = "schedule") -> PoolDecision:
        """One run: the inputs as the edge reads them, the decision, and its effect on the group."""
        supply = Supply(
            desired=self.desired,
            in_service=tuple(self.in_service()),
            starting=tuple(self.starting()),
            draining=tuple(self._named("draining")),
            running=tuple(sorted(self.instances)),
        )
        pool = PoolInputs(
            config=self.pool,
            mode=Mode.ENABLED,
            supply=supply,
            instance_heartbeats=tuple(row for row in self.instance_rows.values() if is_fresh(row, self.now)),
            abandoned_launches=self._abandoned_in_a_row(),
            memory=self.memory,
            session=self.session,
        )
        (decision,) = decide(Inputs(now=self.now, pools=(pool,))).pools
        self.runs.append(Run(self.now, caller, supply, decision))
        # The edge lifts the protection of the instances named, and protects the others in service.
        for name in supply.in_service:
            self.instances[name].protected = name not in decision.lift_protection
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
        excess = len(self.in_service()) + len(self.starting()) - self.desired
        if excess > 0 and self.takes_starting:
            for name in self.starting()[:excess]:
                del self.instances[name]
                self.taken.append((self.now, name))
                excess -= 1
        if excess > 0:
            # Only an instance whose protection is lifted is released.
            for name in [n for n in self.in_service() if not self.instances[n].protected][:excess]:
                self.instances[name].state = "draining"
                self.instances[name].gone_at = self.now + self.pool.drain_seconds
                self.released.append((self.now, name))
        # An abandoned instance still terminating is replaced only once it is gone.
        counted = len(self.in_service()) + len(self.starting()) + len(self._named("terminating"))
        for _ in range(self.desired - counted):
            if self.launches_suspended or (self.capacity is not None and len(self.instances) >= self.capacity):
                break
            instance = self._new("starting")
            instance.started_at, instance.ready_at = self.now, self.now + self.start_seconds
            instance.healthy = self.healthy
            self.launches.append(Launch(self.now, instance.name))

    def _heartbeats(self) -> None:
        for name in self.in_service():
            running = len(self._on(name))
            self.instance_rows[name] = InstanceHeartbeat(
                name, self.pool.name, running, 0, IMAGE_DIGEST, "rev-1", "serving", self.now
            )

    def _second(self) -> None:
        now = self.now
        for request in self.requests:
            if request.outcome is None and request.ends_at is not None and now >= request.ends_at:
                request.outcome = "served"
        for instance in list(self.instances.values()):
            if instance.state == "starting" and instance.healthy and now >= instance.ready_at:
                instance.state, instance.protected = "in-service", True
                self.history.append((now, "in-service"))
            elif instance.state == "starting" and now >= instance.started_at + LAUNCH_HOOK_SECONDS:
                # The launch hook times out and abandons it; the instance is terminated.
                instance.state, instance.gone_at = "terminating", now + TERMINATE_SECONDS
                self.history.append((now, "abandoned"))
            elif instance.state == "terminating" and now >= instance.gone_at:
                del self.instances[instance.name]
            elif instance.state == "draining" and (not self._on(instance.name) or now >= instance.gone_at):
                # The termination hook completes, or the drain is over: the instance is terminated.
                for request in self._on(instance.name):
                    request.outcome = "cut"
                del self.instances[instance.name]
                self.instance_rows.pop(instance.name, None)
        self._reconcile()
        if now % HEARTBEAT_SECONDS == 0:
            self._heartbeats()
        if now % SCHEDULE_SECONDS == 0:
            self.run_scaler()
