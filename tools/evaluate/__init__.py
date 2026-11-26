"""The operator-run evaluation of agent sessions (docs/design/agent-session-evaluation.md).

A package, unlike the other tools: its modules import each other, and `booking` is also the name of
the pipeline's module (`scripts/pipeline/booking.py`), which this one books through. With `tools`
on the path, this one is `evaluate.booking`.

`evaluate.contracts` is the public format of a run: its manifest, its suite and its evidence
records, which other tools exchange and recompute a scorecard from. It imports nothing of the
package, and what `booking` keeps in its journal is private and is never written in it.

`evaluate.workers` starts and disposes of the containers a trial and its submitted code run in,
from the image `evaluate.image` prepares.

`evaluate.plan` expands a suite (`suites/`) into the ordered workload of a profile and the bookings
that hold it, offline. `evaluate.cli` is the command line, which `scripts/evaluate.sh` runs.

`evaluate.streams` reads one streamed attempt on each of the three routes: how it ended, when its
output came, the usage it reported and its speed. It reads with the gateway's and the load
generator's readers, so it needs `services/gateway` and `spikes` on the path as well.

`evaluate.forwarding` is a trial's own way to the endpoint, on the operator's side: a forwarder
that streams the three routes to one gateway and one pool with the student's key, the budget that
holds the whole agent tree to the plan's wall time, turns and generated tokens, and the supervisor
that stops a trial at a limit. It reads each attempt with `evaluate.streams`.

`evaluate.adapters` runs the five student harnesses in a trial's worker, each with its student's
profile and the trial's forwarder as its only destination: the command of each prompt, a follow-up
in the same conversation, the delegated workers Claude Code's output shows, and how the trial
ended. `evaluate.sdk_tool_loop` is the fifth harness, the script it copies into the trial's home.
`evaluate.scripted` is a scripted stand-in for the pool, which the tests put behind a forwarder to
drive a harness through tool exchanges without a model.

`evaluate.traces` freezes the conversations a replay sends, from a seed and the course fixture
under `fixtures/replay/`, before any of them is sent: the requests and pauses of each, in the form
the load generator's recorded-session reader reads. It needs `spikes` on the path too.
"""
