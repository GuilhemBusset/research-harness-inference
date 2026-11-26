+++
id = "E1-26"
title = "The Assets stage pulls its emulator, its builder and its base images from the private mirror"
labels = ["size:M", "kind:infra"]
epic = 3
blocked_by = ["E1-25"]
+++

## Context

E1-25 creates the private mirror (`src/rhi/pipeline/mirror.py`, one repository per image under
`rhi-mirror/`) and the step Mirror, which copies every pinned image the mirror lacks in the stage
Build, before the synthesis, on the commit being deployed. So when the stage Assets of an execution
starts, the mirror holds every digest that execution's source pins. This issue makes the stage pull
from the mirror only, authenticated, and from Docker Hub not at all. With E1-25 it supersedes #701.

**What changes in an image project** (`IMAGE_BUILDER` in `src/rhi/pipeline/stack.py`, issue #118):

- it logs in to the deploying account's registry with the project's own role, then runs the
  emulator image and creates its BuildKit builder from the mirror's copies, at the digests the
  stack pins now: the Docker daemon pulls those two, before `cdk-assets` runs;
- it gives the builder a BuildKit configuration (`docker buildx create` takes a buildkitd
  configuration file) whose one mirror for docker.io is that registry's `rhi-mirror` path.
  BuildKit's reference for `buildkitd.toml` documents a mirror with a path, a host followed by a
  project path, for registries that need one. BuildKit asks the mirror for an image at its Docker
  Hub path under that path, by digest, and turns to Docker Hub only when the mirror lacks it, which
  the step Mirror of the same execution prevents. The registry's host can be read on the build
  host, the caller's account and CodeBuild's region, so that the build specification stays a plain
  string naming no account, as `_builds_images_for_both_platforms` and `tests/policy/test_pipeline.py`
  read it.

**The Dockerfiles keep their FROM lines.** Dependabot reads them, and CI's container tests build
them with no AWS credentials: #706 changed a FROM line and broke CI's `container-tests` with the
pipeline. Nothing under `services/` changes.

**Whose credentials the builder uses.** `cdk-assets` logs in to the account's registry with the
bootstrap's image-publishing role (cdk-hnb659fds-image-publishing-role, followed by the account and
the region) before it builds, to push and to read build caches, and the builder's pulls carry that
login. That role's own policy, written by `cdk bootstrap`, reaches only the bootstrap's asset
repository. So each mirror repository's policy lets that role, by its name in the deploying
account, pull its images: within one account a repository's policy is enough. The image projects'
own role, which CDK Pipelines creates in this stack, gets the authorization token and a pull of the
emulator's and BuildKit's repositories, for the daemon's pulls. The bootstrap's roles and policies
stay as they are: nothing changes in `setup/bootstrap/`, and the account is not bootstrapped again.

**Protected paths.** `src/rhi/pipeline/` and `tests/policy/` are protected (ADR 0001, decision 10);
the builder may change the files named below because this issue names them. The change to
`tests/policy/test_pipeline.py` is expected: its allowances for what an image project installs
(`INSTALLS_THE_IMAGE_BUILDER`) swap the emulator and BuildKit images from Docker Hub for the login,
the same images from the mirror and the builder's configuration, each a pattern as exact as the
others, and allow nothing more.

**Left out**, as in #701: the images pulled at run time rather than by the pipeline, the gateway's
collector on Fargate and the containers the GPU instances' units start.

Suggested branch: `feat/assets-from-mirror`

## Deliverables

- [ ] `src/rhi/pipeline/stack.py` makes each image project log in to the deploying account's registry with its own role, run the emulator and create its builder from the mirror's copies of tonistiigi/binfmt and moby/buildkit at the digests it pins now, and give the builder a BuildKit configuration whose one mirror for docker.io is that registry's rhi-mirror path.
- [ ] `src/rhi/pipeline/mirror.py` gives each mirror repository a policy that lets the bootstrap's image-publishing role of the deploying account pull its images, and the image projects' role may get an authorization token and pull from the emulator's and BuildKit's repositories.
- [ ] `tests/policy/test_pipeline.py` allows in an image project the login to the deploying account's registry, the emulator and BuildKit images from the mirror by digest, and the builder's configuration with that one mirror, in place of the two images from Docker Hub, and nothing more; `tests/policy/acknowledged/pipeline_stack.toml` lists what the image projects' role adds.
- [ ] `tests/synth/test_pipeline_assets.py` checks that each image project logs in, runs the emulator and creates its builder from the mirror by digest with the mirror configured, and that no command of an image project names an image on Docker Hub.
- [ ] `docs/design/system-design.md`, section 8.2 step 4, says that the asset step pulls the emulator, the builder and the base images from the mirror, with whose credentials, and that the Dockerfiles keep their FROM lines.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pipeline_assets.py tests/synth/test_pipeline_mirror.py tests/policy/test_pipeline.py tests/policy/test_acknowledged.py tests/unit/test_ci_workflow.py` passes.
- [ ] `tests/policy/test_pipeline.py` lists, rather than leaves out, an image project that runs tonistiigi/binfmt from Docker Hub by digest, and one that creates its builder from moby/buildkit on Docker Hub by digest.
- [ ] `tests/synth/test_pipeline_mirror.py` shows that each mirror repository's policy lets the image-publishing role of the deploying account pull its images, and lets no principal push to it.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] The first execution after the merge that runs the stage Assets passes it, and no line of that stage's CodeBuild logs names docker.io or registry-1.docker.io (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, section 8.2, step 4
- `src/rhi/pipeline/stack.py`, `IMAGE_BUILDER` and `_builds_images_for_both_platforms`
- `src/rhi/pipeline/mirror.py`, delivered by E1-25
- `tests/policy/test_pipeline.py`, `INSTALLS_THE_IMAGE_BUILDER`
- `setup/bootstrap/README.md`
- Issue #701 and PRs #706 and #713
- https://github.com/moby/buildkit/blob/master/docs/buildkitd.toml.md, the registry mirrors
