+++
id = "E6-13"
title = "Spike S7: lower a session and watch its instances leave"
labels = ["size:L", "kind:test"]
epic = "E6"
blocked_by = ["E6-12"]
+++

## Context

Spike S7 runs on the mock pool, for cents, what the design leaves to measurement about an instance
that leaves (system design, sections 5.1 and 12): how long an idle instance takes to leave when its
session is lowered or ends; whether a request in flight finishes within the drain when its instance
is released, and across a gateway deployment; and what the group does with an instance still
starting when the number is lowered. AWS documents none of them (`docs/design/evidence.md`, E13 and
E32). The job records what it observed, with times, and judges nothing, as today.

Today the job makes the pool grow by load, lets it fall idle, tells whether each instance was
terminated or went back to the warm pool, and then tests wakes after a long idle. None of that
exists under sessions. The job books the mock pool itself, as spike S8 does (E6-12, whose booking
module and role this issue uses), under the name `spike/s7_drain`, for the minutes of
`RHI_SPIKE_RUN_MINUTES`, and moves the pool by changing its own session:

1. **The stream across a gateway deployment**, unchanged.
2. **The departures.** It books two instances and waits until two are in service. It lowers its
   session to one, and records the departure of the instance that leaves; then it stops its
   session, and records the departure of the last one. A departure is told as today, by the note
   that asks for fewer, the in-service reading that drops and the instance whose collector falls
   silent, in the lines `scale_in_asked`, `out_of_service`, `gone` and `scale_in`. No line tells a
   path any more: there is no warm pool to return to.
3. **The stream across a release**, as today's stream across a scale-in: one slow stream whose body
   is held open at the gateway and whose last byte is sent when the job reads the note that asks
   for fewer, after its own lowering.
4. **The starting instance**, in a cycle of its own. From a pool at zero it books two instances,
   and as soon as the note reads one in service and one starting it lowers its session to one. It
   records, in a line `starting_instance`, which of the two left, the starting one or the one in
   service, and when.

The wakes go: no request starts a machine. With them go the job's reads of the pool's hold and idle
settings, which E6-21 retires.

The job is to be run once E6-15 is deployed: before it, a lowered session releases nothing by
itself. Its tests use doubles and do not wait for that. An instance is still named by its order of
appearance, never by its AWS identifier, and no line holds text from AWS.

Why the size: the job is 1,300 lines with 750 lines of tests, and three of its four steps change.

## Deliverables

- [ ] `spikes/s7_drain.py` books a session of two instances on the mock pool with the booking module spike S8 uses, before its cycles, refuses to start when the pool has a session booked or active or allows fewer than two instances, and stops its session when it ends, whatever happened.
- [ ] For the departures it lowers its session to one instance, then stops it, and writes for each instance that leaves the lines of kind scale_in_asked, out_of_service, gone and scale_in, none of which tells a path or reads a warm count.
- [ ] The stream across a release sends its last byte when the job reads the scaler's note that asks for fewer after the job's own lowering, and its line records whether it finished, ended with an error event or was cut.
- [ ] In a cycle that starts from a pool at zero, the job lowers a session of two to one as soon as the note reads one instance in service and one starting, and writes one line of kind starting_instance that says which of the two left, and when.
- [ ] The job sends no wake and reads neither the pool's hold nor its idle time: the step of the wakes and the variable RHI_S7_WAKES are gone.
- [ ] `docs/measurements/s7.md` says what the job records now, what it is given, and that it is run once the scaler releases the instances of a lowered session.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s7.py` passes: on recorded readings, a session lowered from two to one and then stopped gives two departures in order, each with its three times, and the session is stopped after a cycle that raised.
- [ ] `tests/unit/spikes/test_s7.py::test_the_starting_instance` shows the line for a run in which the starting instance left and for one in which the instance in service left.
- [ ] `git grep -n -i -e hold_seconds -e idle_minutes -e "warm pool" -e warm_ -e RHI_S7_WAKES -- spikes/s7_drain.py docs/measurements/s7.md` prints nothing.
- [ ] In the account, with E6-15 deployed, a build of the job on the mock pool writes every line of its four steps and leaves the pool with no active session (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.1 and 12
- `docs/design/evidence.md`, E13 and E32
- `spikes/s7_drain.py`
- `docs/measurements/s7.md`
- `scripts/pipeline/booking.py`, delivered by E6-07
