+++
id = "E5"
kind = "epic"
title = "Phase 4: the upgrade pool, Qwen3.8-Flash-Next on four GPUs, for scheduled sessions"
labels = ["kind:infra"]
+++

Phase 4 of the rollout (`docs/design/system-design.md`, section 14): the upgrade pool,
Qwen3.8-Flash-Next at 8 bits on `g7e.24xlarge` (four 96 GB GPUs) under vLLM, at $16.57 an hour,
and spike S6. It serves scheduled sessions only: a schedule or an admin command starts it, never a
student's request, and it keeps no warm instance between sessions (section 4.2). It shares the
coursework model's chat template and parsers, and so its request profile (section 6.1).

Exit criterion, from the design: a scheduled session served by the upgrade pool. It is an
obligation on issue E5-03.

The pieces: starting a scheduled-only pool by the admin's command (E5-01), one engine across the
instance's four GPUs (E5-06), heartbeats on the launch hook while it starts (E5-02), the model's
manifest, from the repository vLLM's recipe names (E5-08), the pool itself (E5-03), its expected
values (E5-07) and spike S6 (E5-05). The owner writes the pool's first machine image by hand, then
adds the pool to the configuration in SSM (E5-03); a merge never needs either.

Whether scheduled sessions use this pool or several coursework instances, and whether the upgrade
model's licence allows this use, is the owner's decision 3 (section 15). Spike S6 costs real money
and needs the budget of decision 6. If S6 fails, scheduled sessions run on more coursework
instances (section 12), which is configuration and needs no issue.

The xl pool (DeepSeek-V4.1-Flash on `p5en.48xlarge`) is built only if the owner asks for it
(sections 4.2 and 14). It is not drafted.
