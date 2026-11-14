+++
id = "E3-07"
title = "The model bucket: written by one job, read by each pool under its own prefix"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E2-41", "E2-44", "E2-45", "E2-88"]
+++

## Context

Weights live in S3 (section 4.4): cheapest at rest, under $1 a month for the coursework model, and
streaming from S3 is within seconds of local disk (evidence E11). Only the copy job's role can write
to the bucket (issue E3-09). Instances read weights through the S3 gateway endpoint (issue E2-45)
and never talk to Hugging Face. The bucket is stateful.

**Reads go through the endpoint; writes cannot.** The copy job runs in CodeBuild, outside the VPC,
so the bucket's policy refuses reading an object by any request that does not come through the VPC's
S3 gateway endpoint, and says nothing of where a write comes from. The copy job never reads an
object: it learns what a prefix holds by listing it (issue E3-09).

**How the pipeline finds the bucket.** The copy job's role lives in the pipeline stack, which cannot
refer to a resource of the stage. So the bucket's name is fixed: a function of
`src/rhi/pools/model_store.py` builds it from a constant prefix of the module, the account and the
region, as deployment-time values, and both the stage and the pipeline stack call it. The repository
then holds the prefix only, never an account or a region.

**Layout.** A pool's weights are under the prefix `<pool>/<revision>/`, so that a new revision is
written beside the one in service and an instance never loads a half-replaced model (issues E3-04
and E3-09). Each GPU pool's instance role reads its own pool's prefix and nothing else.

**Stateful protections.** The stateful invariant of issue E2-88 asks of an S3 bucket a retain policy
on deletion and on replacement; point-in-time recovery and deletion protection are DynamoDB's.

This issue changes `tests/policy/stateful_logical_ids.json` and `tests/policy/acknowledged/`,
protected paths (ADR 0001, decision 10); the builder may change the files named below because this
issue names them.

## Deliverables

- [ ] `src/rhi/pools/model_store.py` creates one private, encrypted bucket with a retain policy, named by a function of the module from its constant prefix, the account and the region, and `src/rhi/pools/stack.py` holds it.
- [ ] The bucket's policy refuses reading an object by a request that does not come through the VPC's S3 gateway endpoint, and does not restrict writes by endpoint.
- [ ] `src/rhi/pools/role.py` lets each GPU pool's instance role read the objects under its own pool's prefix and nothing else in the bucket, and no role of the stage has a write action on the bucket.
- [ ] Every cdk-nag finding `src/rhi/pools/model_store.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_model_store.toml`.
- [ ] Every cdk-nag finding `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_stack.toml`.
- [ ] Every cdk-nag finding `src/rhi/pools/role.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_role.toml`.
- [ ] `tests/policy/stateful_logical_ids.json` records the bucket, and `tests/synth/test_model_store.py` checks the bucket, its name and who reads what.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_model_store.py` passes: public access is blocked, the bucket's name is built from the prefix with the account and region left to deployment, a read of an object from outside the endpoint is refused by the policy, each GPU pool of a configuration built in the test reads its own prefix only, and no stage role has a write action on the bucket.
- [ ] `uv run --locked pytest tests/policy/test_stateful_resources.py` passes with the bucket recorded.
- [ ] `scripts/check.sh synth` exits 0 with the bucket in the stage.

## References

- `docs/design/system-design.md`, sections 4.4, 8.5
- `docs/design/evidence.md`, E11
