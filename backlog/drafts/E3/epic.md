+++
id = "E3"
kind = "epic"
title = "Phase 2: the coursework pool, its request profile and student affinity, and the numbers the design still assumes"
labels = ["kind:infra"]
+++

Phase 2 of the rollout (`docs/design/system-design.md`, section 14): the coursework pool, which is
Qwen3.8-27B at 8 bits on one 96 GB GPU (`g7e.2xlarge`) under vLLM 0.33 or later, with its stopped
warm pool; the gateway's request profile, which makes the model work in Claude Code; student
affinity, which keeps a student's turns on the instance that holds their cached transcript; and
spikes S1 to S5, S7 and S8. There is no smaller GPU pool before it: the mock proves the control loop,
and the first GPU pool is the one the class uses (section 4.2). This is the first phase that needs a
GPU quota.

Exit criteria, from the design: measured cold start, hold behaviour, harness results and what one
instance serves recorded under `docs/measurements/`; the weekly canary green twice. Each is an
obligation on the issue that builds its job: S1 on E3-15, S2 on E3-16, S3 on E3-41 and E3-35, S5 on
E3-42, the canary on E3-13. S4, S7 and S8 (E3-18, E3-20, E3-33) are part of the phase too.

The pieces, in the order they build on one another:

- The engine: its image (E3-03), the contract settings carried in each release (E3-04), the units
  only GPU pools install (E3-38), the fallback loader (E3-44), the coursework release's settings
  (E3-22), GPU metrics (E3-05), and the vLLM metric names with its cache capacity (E3-06).
- The instance: its machine image (E3-01), the canary instance outside any group (E3-39) and the
  promotion job (E3-02).
- The weights: the bucket, the allow-list and manifests, the copy job (E3-07 to E3-09), and the
  manifest of the 4-bit build that S3 and S5 also run (E3-34).
- The pool: its configuration entry (E3-10), its group and the owner's steps that put it in the
  account (E3-40), its verification on each release (E3-12) and the weekly canary (E3-13).
- The request profile: the mechanism (E3-23), replaying a capture through a profile (E3-43), its
  three entries (E3-24 to E3-26), and the coursework pool's profile (E3-27). The upgrade pool of
  phase 4 shares it.
- Student affinity: the settings and records (E3-28), stickiness on each target group (E3-29), the
  gateway keeping each student's cookie (E3-30), the scaler's rebalance number (E3-31) and the
  gateway moving a few students for each new number (E3-32).
- The spikes: the code they share and their canary students (E3-14), the jobs the owner starts
  (E3-45), the load generator (E3-36), the captures of the real harnesses (E3-21), the mock pool's
  start delay that S2 holds clients with (E3-46), and S1 to S5, S7 and S8 (S3's task is E3-17; S5's
  runs are E3-19 and its verdict E3-42).

A spike is built here as a job and a record. What follows from a spike that fails (a baked image
after S1, another engine version or the fallback model after S3, more instances after S5, a pool
kept at one instance after S8) is not drafted: it depends on a number nobody has yet.

The coursework model is the owner's decision 2 (section 15), carried on E3-10. The harnesses that
must work are decision 4, carried on E3-41. The class size is decision 5, carried on E3-42.

The owner writes the coursework pool's first machine image by hand, then adds the pool and its
request profile to the configuration in SSM (E3-40, E3-27); a merge never needs either, since a pool
that is not in the deployed configuration is not built.
