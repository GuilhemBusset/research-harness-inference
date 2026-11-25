+++
id = "E6"
kind = "epic"
title = "Sessions the instructor books replace scaling by load"
labels = ["kind:app"]
+++

The endpoint stops scaling by load. The instructor books each session with one command: a number of
instances for a pool, a start and an end. The scaler sets each pool to what its session asks and to
zero outside it, an instance that is asked for stays asked for until AWS delivers it, and no
student's request starts a machine (`docs/design/system-design.md`, sections 5.1 to 5.6; the
capacity record behind it is `docs/design/evidence.md`, E32). When the epic is done:

- a session is one record per pool, written by `session start`, `session set` and `session stop`,
  and `session status` shows the instances requested, asked of AWS, waiting for capacity, starting
  and in service;
- the scaler reconciles and nothing else: no wake, no rule on load or idleness, no pool mode, no
  session switch, no nightly stop;
- the gateway serves a pool while its session is active and answers at once otherwise: no hold, no
  wake route;
- a pool has no warm pool and no instance refresh;
- the pipeline's verification, the weekly canary and the spikes get their capacity from a session,
  and "no capacity" is reported apart from a failed release;
- the fleet dashboard shows what a session requested against what is available, with each
  instance's telemetry, and one alarm says when a session is short.

The issues are ordered so that every merge leaves the endpoint deployable. One bridge carries the
order: from E6-02 the scaler treats an active session as a floor under its existing rules, so that
the verification, the canary, the spikes and the dashboards move to sessions before E6-15 removes
the rules on load.

**What the epic leaves out.**

- Removing the tolerance for the retired configuration keys. E6-06 and E6-21 retire keys: the
  loader accepts them and reads them into nothing, because the deployed configuration still holds
  them. The issue that refuses them is drafted only after the owner has removed them from the
  deployed configuration: built before, it would break the configuration's load.
- The decision record. The design asks for an ADR when a decision changes; the owner writes it.
- A capacity reservation for a session that must not start short. The design records it as an
  option (sections 13 and 16); it is not built.
- The readings of spikes S1 and S4. E6-10 removes their jobs; the commands that read the recorded
  launches are drafted once sessions have recorded some, as section 12 says of every reading.

**Open questions for the owner.**

1. May the pipeline book a GPU by itself, after a deployment that changes a GPU pool's release and
   once a week (decision 14 of section 15)? E6-08 carries it. The design assumes yes.
2. Is 12 hours the right longest session? It does not cover a session booked the evening before a
   morning class.
3. Two messages at most tell the instructor about capacity: short for 30 minutes, then filled. Is a
   message wanted every time a session is filled? And the configuration's phone number has no
   reader: wire it to the alerts, or retire it.
4. A session serves every key, whoever booked it: a student could use a canary's or a spike's
   instance. Accepted, or a flag on the session?
5. Students see when the session ends, in the status. Wanted?
6. The region: capacity for the coursework GPU type differs between regions (decision 9).
7. A capacity reservation for a critical session: build the option before the first class?
8. The ADR for this change of approach, and the status lines that cite it.
9. A pool's `max_instances` changes only by a deployment, and deployments wait during sessions:
   set it generously and let the quota be the cap, or accept the limit.
10. Fallback instance types: the design says the same GPU in the next sizes. A fallback with
    another GPU changes what section 4.7 estimates: keep one, or not?
11. The old items of the pool-state table (the modes, the switch): leave them, or delete them.
12. Spike S5's first result bears on how many instances a session books. It is not part of this
    epic.

Three more, which the drafts leave open rather than invent an answer:

- What a run of the scaler does with a session item it cannot read. E6-02 asks for no rule.
- How long a check waits for the mock pool's instance. The drafts use, for every pool a check
  books, the wait the design gives for a GPU instance: 45 minutes.
- How a GPU spike job learns how long its run lasts. In E6-11 the owner gives it.
