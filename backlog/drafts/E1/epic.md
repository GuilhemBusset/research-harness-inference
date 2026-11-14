+++
id = "E1"
kind = "epic"
title = "Phase 0: scaffold, bounded bootstrap, the pipeline and the first invariants"
labels = ["kind:infra"]
+++

Phase 0 of the rollout (`docs/design/system-design.md`, section 14): the owner's prerequisites of
section 8.6, the repository scaffold of section 8.1 with the configuration loader of sections 8.1
and 10, the bounded bootstrap and the first policy tests of section 8.4, the pipeline of section 8.2
with an empty stage, and spike S11. Nothing here needs a GPU quota, and the whole phase costs almost
nothing to run. The owner should still request the G-family GPU quota the day phase 0 starts,
because nobody documents how long a request takes (section 14).

Exit criteria, from the design: the pipeline deploys an empty stage, and a commit status appears on
GitHub. The first is an owner obligation on the synth-step issue of this epic, the first after which
the pipeline can be deployed; the second is delivered by the status reporter of epic E0 and is an
obligation on its construct issue.

The owner's one-time steps of section 8.6 are not agent work. They appear as obligations on the
issues that prepare them, and the walkthrough issues list them in order. The configuration loader
introduces every value only the owner can supply, so that the owner writes the configuration to SSM
once; every key or pool field a later issue adds has a default or is optional, so that no later
merge needs a value the deployed configuration lacks.

The epic also settles three conventions the rest of the backlog follows: accepted cdk-nag findings
and IAM actions granted on every resource are listed per construct module under
`tests/policy/acknowledged/`; the pipeline's reviewed list holds its stages and steps, while stacks
and assets added to the stage are held by rules; and the services' libraries are pinned in
dependency groups of the root project, from which functions and images are packaged without Docker.
