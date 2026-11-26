"""The operator-run evaluation of agent sessions (docs/design/agent-session-evaluation.md).

A package, unlike the other tools: its modules import each other, and `booking` is also the name of
the pipeline's module (`scripts/pipeline/booking.py`), which this one books through. With `tools`
on the path, this one is `evaluate.booking`.

`evaluate.contracts` is the public format of a run: its manifest, its suite and its evidence
records, which other tools exchange and recompute a scorecard from. It imports nothing of the
package, and what `booking` keeps in its journal is private and is never written in it.

`evaluate.plan` expands a suite (`suites/`) into the ordered workload of a profile and the bookings
that hold it, offline. `evaluate.cli` is the command line, which `scripts/evaluate.sh` runs.

`evaluate.streams` reads one streamed attempt on each of the three routes: how it ended, when its
output came, the usage it reported and its speed. It reads with the gateway's and the load
generator's readers, so it needs `services/gateway` and `spikes` on the path as well.
"""
