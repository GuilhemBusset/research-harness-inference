+++
id = "E1-25"
title = "The pipeline copies the images its builds pull into a private mirror before it synthesizes"
labels = ["size:L", "kind:infra"]
epic = 3
+++

## Context

This issue and E1-26 supersede #701. #701 moved the Python base image to Amazon ECR Public's copy
of the Docker Official Image and replaced the emulator and BuildKit images by downloads (PR #706).
It was reverted by #713: anonymous pulls from shared addresses, CodeBuild's and GitHub's runners,
hit ECR Public's anonymous pull rate, which BuildKit does not retry, and the pipeline's arm64 image
builds failed on every execution, and CI's container tests with them. Before and since, Docker Hub's anonymous limit, counted per address, fails
the Assets stage several times a day. The owner decided on 2026-11-27 that the base images are
mirrored into the account's private ECR and pulled from there, authenticated. This issue creates
the mirror and fills it; E1-26 makes the Assets stage pull from it. Until E1-26, nothing reads it.

**What the pipeline's builds pull**, all from Docker Hub today: `tonistiigi/binfmt` (the emulator)
and `moby/buildkit` (the builder), which `IMAGE_BUILDER` in `src/rhi/pipeline/stack.py` runs in
every image project on every run (issue #118); and the bases of the FROM lines, `python` for the
gateway and the mock engine, `grafana/grafana-oss` and `vllm/vllm-openai`, pulled when an image is
rebuilt. Each is pinned by its sha256 digest.

**The mirror.** One private ECR repository per image, named `rhi-mirror/` followed by the image's
path on Docker Hub, where an official image's path starts with `library/`. E1-26 gives BuildKit
that registry's `rhi-mirror` path as its mirror for docker.io, and BuildKit asks a mirror for an
image at its Docker Hub path under the mirror's own path: so the Dockerfiles keep their FROM lines,
which Dependabot reads and CI's container tests build with no AWS credentials. ECR's pull-through
cache was weighed: a rule for ECR Public needs no credential but would serve only `python` (the four
others have no official copy outside Docker Hub, checked for #701), and Docker Hub as an upstream
needs a Docker Hub credential, which is not assumed. A copy keeps one mechanism for all five, and
the repositories stay resources of the stack.

**The step.** A CodeBuild step named `Mirror` runs in the stage Build, before the action Synth. A
step that `Synth` depends on is placed there: a probe on 2026-11-27 with this repository's
aws-cdk-lib gave the stage Build the actions Mirror, then Synth, while a step added as a wave of its
own lands after the stage Assets, too late. The step runs `scripts/pipeline/mirror_images.py` on
the execution's source, in a locked environment, as the hold step runs its script: the stage Build
runs before the self-update, so the step's project is the one the previous deployment defined, but
it reads the pins from the commit being deployed. A merge that bumps a pin therefore has the new
digest copied before its own Assets stage.

**The copy** keeps the digest: the image's index and every manifest it lists, with their layers,
so the repository holds the pinned digest itself, the one BuildKit asks for. `crane copy` does that
(go-containerregistry). Pin it like the buildx plugin: version v0.22.1, published 2026-09-04, file
`go-containerregistry_Linux_x86_64.tar.gz` at
https://github.com/google/go-containerregistry/releases/download/v0.22.1/go-containerregistry_Linux_x86_64.tar.gz,
sha256 `0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0` (the release's
`checksums.txt`, and the file as downloaded on 2026-11-27); the archive holds `crane` at its top.
The step logs crane in to the account's registry with its own role. The registry's host can be
read on the build host, the caller's account and CodeBuild's region, so that the step's build
specification names no account.

**Docker Hub is read only for a digest the mirror lacks**: never on an ordinary run, once for each
new pin, and in this step, never in the stage Assets once E1-26 is merged. Docker Hub limits
anonymous pulls per address by the hour, and a first fill reads every platform's manifest of four
images, so the first runs of the step may be refused before the mirror holds them all. The step
copies image by image, keeps what it copied, and fails only after trying every image: a retried
execution copies what is left. Whether that residual read is acceptable, against "no Docker Hub at
build time", is the owner's decision (last acceptance item).

**A new image.** The stage Build runs before the self-update that creates a new image's
repository. The step leaves an image whose repository does not exist yet for a later execution,
rather than failing the one that adds it. Its role is therefore granted the repositories by their
prefix, `rhi-mirror/`, so that a repository a later change adds is in reach before the role is
redeployed, and a missing one reads as missing rather than as denied.

**IAM and the bootstrap.** The step's role may get an ECR authorization token, which AWS grants only
on every resource, and pull, push and describe images in the repositories whose names start with
`rhi-mirror/`, in the deploying account and region; the acknowledgements list the token's wildcard
and cdk-nag's finding on the prefix, each with its reason. It may create no repository. The bounded bootstrap denies only IAM users, access
keys, identity providers, roles without the boundary and changes to its own two policies
(`setup/bootstrap/README.md`), so a deployment may create these repositories and this role. No
service-linked role is involved: ECR repositories and pushes need none, and no pull-through cache
rule is created. Nothing changes in `setup/bootstrap/`, and the account is not bootstrapped again.

**Protected paths.** `src/rhi/pipeline/` and `tests/policy/` are protected (ADR 0001, decision 10);
the builder may change the files named below because this issue names them. The change to
`tests/policy/test_pipeline.py` is expected: its reviewed list gains the action Mirror. The role of
a CodeBuild action of the stage Build is held by that file's synth-role rule, which this role meets:
it reads no parameter or secret and assumes no role.

Suggested branch: `feat/base-image-mirror`

## Deliverables

- [ ] `src/rhi/pipeline/mirror.py` creates in the pipeline stack one private ECR repository for each image the pipeline's builds pull, named rhi-mirror/ followed by its path on Docker Hub: rhi-mirror/library/python, rhi-mirror/grafana/grafana-oss, rhi-mirror/vllm/vllm-openai, rhi-mirror/tonistiigi/binfmt and rhi-mirror/moby/buildkit, each retained when the stack is deleted.
- [ ] `src/rhi/pipeline/stack.py` runs the CodeBuild step named Mirror of `src/rhi/pipeline/mirror.py` in the stage Build before the action Synth, on the execution's source, with a role that may get an ECR authorization token and pull, push and describe images in the repositories whose names start with rhi-mirror/ in the deploying account and region, beside what CDK Pipelines gives every project, and may create no repository.
- [ ] `scripts/pipeline/mirror_images.py` lists the images to mirror from the source: the image of each FROM line of the files named Dockerfile under services, but scratch and a stage of the same file, and the emulator and BuildKit images that `src/rhi/pipeline/stack.py` pins; it fails on an image named without its sha256 digest.
- [ ] `scripts/pipeline/mirror_images.py` copies each listed image whose digest its repository lacks with crane v0.22.1, checked against its checksum, as its index and every manifest that index lists, reads nothing from a public registry for a digest already there, and leaves for a later execution an image whose repository does not exist yet.
- [ ] `scripts/pipeline/mirror_images.py` goes on to the next image when one cannot be copied, and exits non-zero once it has tried them all if any was not copied.
- [ ] `tests/policy/test_pipeline.py` lists the action Mirror before the action Synth in the stage Build of its reviewed list, and `tests/policy/acknowledged/pipeline_stack.toml` lists what the step's role adds, ecr:GetAuthorizationToken and the finding on the rhi-mirror/ prefix, each with its reason.
- [ ] `docs/design/system-design.md`, section 8.2, describes the mirror: its repositories, the step Mirror before the synthesis, and when the step reads Docker Hub.

## Acceptance criteria

- [ ] `tests/unit/test_mirror_images.py` shows, with stand-ins for crane and for ECR, that today's tree lists exactly the five images above, each with its digest, and that a run copies only the digests the mirror lacks.
- [ ] `tests/unit/test_mirror_images.py` shows that a run after a complete copy copies nothing and reads no public registry, that an image with no repository yet is skipped while the others are copied, and that a refused copy fails the run only after the other images were tried.
- [ ] `tests/synth/test_pipeline_mirror.py` shows the five repositories in the pipeline stack's template, the action Mirror before Synth in the stage Build, and the statements of its role limited to the repositories under rhi-mirror/ of the deploying account and region and to the authorization token.
- [ ] `uv run --locked pytest tests/unit/test_mirror_images.py tests/synth/test_pipeline_mirror.py tests/synth/test_pipeline.py tests/policy/test_pipeline.py tests/policy/test_acknowledged.py` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] The first execution that runs the step Mirror passes its stage Build, once retried if Docker Hub refused a copy, and the five repositories then hold the digests the source pins (needs aws:deployed-environment).
- [ ] The owner accepts that the step Mirror reads Docker Hub anonymously, once for each pin the mirror lacks, in the stage Build, since no Docker Hub credential is assumed (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 8.2 (steps 2 and 4), 9 and 17 (the cooling-off)
- `src/rhi/pipeline/stack.py`, `IMAGE_BUILDER`, and issue #118, which added it
- `src/rhi/pipeline/hold.py`, a step that runs a script of `scripts/pipeline/` on the source
- `tests/policy/test_pipeline.py`, `REVIEWED`
- `setup/bootstrap/README.md`, what the bounded bootstrap denies
- Issue #701 and PRs #706 and #713, the approach this replaces and why it was reverted
- https://github.com/google/go-containerregistry/releases/tag/v0.22.1
