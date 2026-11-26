"""The operator-run evaluation of agent sessions (docs/design/agent-session-evaluation.md).

A package, unlike the other tools: its modules import each other, and `booking` is also the name of
the pipeline's module (`scripts/pipeline/booking.py`), which this one books through. With `tools`
on the path, this one is `evaluate.booking`.

`evaluate.contracts` is the public format of a run: its manifest, its suite and its evidence
records, which other tools exchange and recompute a scorecard from. It imports nothing of the
package, and what `booking` keeps in its journal is private and is never written in it.

`evaluate.workers` starts and disposes of the containers a trial and its submitted code run in,
from the image `evaluate.image` prepares.

`evaluate.course` holds the course tasks, one package for each family. `evaluate.course.optimization`
is the production-planning task: its three instances, the files a trial's workspace is given, and,
kept outside every workspace, the reference fix and the grader that reads the plan and the table
a run wrote. The grader needs the standard library only.

`evaluate.plan` expands a suite (`suites/`) into the ordered workload of a profile and the bookings
that hold it, offline. `evaluate.cli` is the command line, which `scripts/evaluate.sh` runs.

`evaluate.streams` reads one streamed attempt on each of the three routes: how it ended, when its
output came, the usage it reported and its speed. It reads with the gateway's and the load
generator's readers, so it needs `services/gateway` and `spikes` on the path as well.

`evaluate.forwarding` is a trial's own way to the endpoint, on the operator's side: a forwarder
that streams the three routes to one gateway and one pool with the student's key, the budget that
holds the whole agent tree to the plan's wall time, turns and generated tokens, and the supervisor
that stops a trial at a limit. It reads each attempt with `evaluate.streams`.

`evaluate.traces` freezes the conversations a replay sends, from a seed and the course fixture
under `fixtures/replay/`, before any of them is sent: the requests and pauses of each, in the form
the load generator's recorded-session reader reads. It needs `spikes` on the path too.

`evaluate.telemetry` reads from the Prometheus workspace what served a measured window: the hosts
and what each ran, their engines' counters through the drain, and the gateway's traffic. It says
which treatments that evidence leaves unverified and why a window cannot qualify. It reads with
spike S5's readers, by the names the services export, so it needs `spikes`, the services and
`dashboards` on the path.

`evaluate.replay` drives one cell of a plan on the student gateway: each slot's conversations as
their seat's canary student, independently of the other seats, for the cell's admissions and a
bounded drain, with a record of every request and of what the driver planned and observed. It
sends the traces of `evaluate.traces` and reads their streams with `evaluate.streams`, so it needs
the same paths.

`evaluate.journal` is the driver's private journal of one run, under the ignored `.cache/eval/`:
the run's ID, its issue number, what it was about to book or deliver, and each attempt of a cell
with its observations, held by one owner at a time. A restart reads from it what to reconcile and
which whole cells are left to run. It needs the standard library only.
"""
