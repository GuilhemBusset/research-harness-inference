+++
id = "E4-20"
title = "A one-page privacy notice for students"
labels = ["size:S", "kind:docs"]
epic = "E4"
blocked_by = ["E1-02"]
+++

## Context

A one-page notice for students names the controller, what is logged, where (AWS, GitHub), for how
long, and whether usage counts toward grading (section 6.6). The facts it states are the design's:
prompts and responses are not stored; usage metadata is kept per student, and source addresses in
load balancer, WAF and gateway logs; per-student series are labelled with a roster alias, not the
GitHub ID; nothing is kept past course end plus 90 days; login goes through GitHub, which keeps
only the numeric user ID on the roster (section 6.4). An erasure removes the rows at once and cuts
the alias's link to the student, while logs and series expire with their retention.

The notice states what the design decides; it does not wait for the issues that build it (retention,
issues E4-18, E4-35 and E4-37; export and erasure, issue E4-19), which it neither reads nor tests.

The region is US East by default; if students are in the EU, the owner picks an EU region before
the first login. Who signs the notice, and the region, are the owner's decision 9 (section 15).
Whether usage counts toward grading is the owner's to say.

## Deliverables

- [ ] `docs/privacy-notice.md` is one page with these headings: who is responsible, what is recorded, what is not recorded, where it is kept, for how long, who can see it, whether it counts toward grading, how to ask for a copy or for deletion.
- [ ] It states the facts of the context above, including what an erasure removes at once and what only expires; the controller's name, the region and the grading rule are marked as the owner's to fill in.
- [ ] `tests/unit/test_docs.py` checks the notice's headings and that it states 90 days.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_docs.py` passes: the eight headings are present and the notice says 90 days.
- [ ] The owner names the controller and the region, says whether usage counts toward grading, and signs the notice: decision 9 of the design (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 6.4, 6.6, 15
