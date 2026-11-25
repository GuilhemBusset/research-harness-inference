+++
id = "E2-92"
title = "The asset step pulls no emulator, no builder and no Python base image from Docker Hub"
labels = ["size:M", "kind:infra"]
epic = 4
+++

## Context

Docker Hub limits anonymous pulls by address, and CodeBuild's build hosts share their addresses.
The deployed pipeline's Assets stage fails on that limit often, several times a day on a day of
several merges, and is retried by hand until it passes. No Docker Hub token is assumed: the owner
may provide one later, and nothing here depends on it.

What the stage pulls from Docker Hub today:

- **On every run, in every image project**, before `cdk-assets` knows whether any image needs
  building: the emulator image `tonistiigi/binfmt` and the BuildKit image `moby/buildkit`, both
  run by `IMAGE_BUILDER` in `src/rhi/pipeline/stack.py` (issue #118).
- **When an image's asset changed**, the base image its build pulls: `python` for the gateway and
  the mock engine, `grafana/grafana-oss` and `vllm/vllm-openai`. `cdk-assets` skips an image
  whose asset is already in ECR, so a base is pulled only when its image is rebuilt.

What was checked on 2026-11-25, anonymously, with no AWS account:

- Amazon ECR Public's copy of the Docker Official Images serves
  `public.ecr.aws/docker/library/python:3.13.14-slim-trixie` and the same multi-architecture
  index the two Dockerfiles pin, `sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6`
  (registry API, `Docker-Content-Digest`): the FROM line changes its registry and keeps its tag
  and digest.
- Neither Grafana's image nor vLLM's has an official repository on ECR Public or on GHCR. They
  stay on Docker Hub, in the test's allowed list with that reason. A pull-through cache with a
  Docker Hub token could later take them off anonymous pulls: an owner step, not this issue.
- Neither `tonistiigi/binfmt` nor `moby/buildkit` has an official copy outside Docker Hub. The
  binfmt project's own GitHub release `deploy/v10.2.3-68` (published 2026-06-08) carries the same
  tool and the same QEMU 10.2.3 as the pinned image, as two files for linux/amd64, at
  `https://github.com/tonistiigi/binfmt/releases/download/deploy/v10.2.3-68/<file>`:
  `binfmt_linux-amd64.tar.gz` (the tool, sha256 `9f955e980acb29986365766db7d36383a1dbbf2159f30b532a74474f6f5dd75d`)
  and `qemu_v10.2.3_linux-amd64.tar.gz` (the emulators, `qemu-aarch64` among them, sha256
  `8e7d8f4c0c7809fc3fea0085199fd6b16f671e7c73d9bf6bec711e1cb535920a`). The tool reads the
  emulators' folder from `QEMU_BINARY_PATH` (default `/usr/bin`, where the image keeps them).

**The emulation, and why this way.** The image projects keep their x86_64 hosts and #118's
output, an image for linux/arm64 and one for linux/amd64 in the same run. The emulator comes from
the binfmt release above, downloaded from the host the buildx plugin already comes from and
pinned the same way, by exact version and checksum, and registered by the same tool with the
same `--install arm64`. The BuildKit container goes rather than moves: `cdk-assets` builds each
image for one platform, which the Docker daemon's own builder (buildx's default builder, with its
`docker` driver) does under a registered emulator, and it loads nothing because the image is
already in the daemon. The pinned buildx download stays, for `DOCKER_BUILDKIT=1`. Two other ways
were weighed: arm64 CodeBuild hosts for the arm64 images would need neither emulator nor builder,
but they need a per-asset environment override of the projects CDK Pipelines creates, and arm64
build capacity in the account that nobody has checked; QEMU from the build image's package
manager cannot be pinned by checksum and is an older QEMU than #118 pinned.

**Docker Hub, explicit or implicit.** An image reference names Docker Hub when its host is
`docker.io`, `index.docker.io`, `registry-1.docker.io` or `registry.hub.docker.com`, or when it
names no host: its first path component has no `.` or `:` and is not `localhost` (`python:3.13`,
`grafana/grafana-oss`). A Dockerfile's `# syntax=` parser directive names an image the builder
pulls, so it counts too. `FROM scratch` pulls nothing, and neither does a FROM that names an
earlier stage of the same file. The test reads the Dockerfiles git lists, tracked or untracked
but not ignored (`git ls-files --cached --others --exclude-standard`): the pipeline's synth step
runs the tests in a work tree with no commit, where every file is untracked.

**Dependabot.** Its docker ecosystem reads the registry from each FROM line and asks
`public.ecr.aws` anonymously, so `.github/dependabot.yml` does not change, and
`tests/unit/test_ci_workflow.py::test_dependabot_watches_every_image_under_services` keeps
checking that its folders cover every Dockerfile. ECR Public limits anonymous tag listing on
repositories with many tags, as `docker/library/python` is
(https://github.com/dependabot/dependabot-core/issues/11921): a weekly run may then propose
nothing for the two Python lines, and that bump is made by hand, after the cooling-off of
section 17.

**Protected paths.** `src/rhi/pipeline/` and `tests/policy/` are protected (ADR 0001, decision
10); the builder may change the files named below because this issue names them. The change to
`tests/policy/test_pipeline.py` is expected: its allowances for what an asset-publishing project
installs (`INSTALLS_THE_IMAGE_BUILDER`) swap the emulator image and the BuildKit image for the
release's download, checksum check, extraction and registration, each a pattern as exact as the
others, and allow nothing more.

**Left out.** Images pulled at run time rather than by the pipeline: the gateway's collector on
Fargate and the containers the GPU instances' units start. The Grafana and vLLM FROM lines,
which stay as they are.

Suggested branch: `fix/assets-no-docker-hub`

## Deliverables

- [ ] `services/gateway/Dockerfile` and `services/engine/mock/Dockerfile` take their base from public.ecr.aws/docker/library/python at the tag and digest they pin now, and their comments say it is Amazon ECR Public's copy of the Docker Official Image.
- [ ] `src/rhi/pipeline/stack.py` registers the arm64 emulator from the binfmt release deploy/v10.2.3-68 in place of the emulator image: it downloads the tool and the emulators for linux/amd64 by that exact version, checks each file against its checksum, extracts them and runs the tool with --install arm64.
- [ ] `src/rhi/pipeline/stack.py` creates no builder from a BuildKit image and sets no BUILDX_BUILDER, so an image project builds with the Docker daemon's own builder, and its docstring says where the emulator and the builder come from.
- [ ] `tests/policy/test_pipeline.py` allows in an asset-publishing project the emulator release's download by exact version, its checksum check, its extraction and its registration, and no longer allows a docker run of an emulator image or a docker buildx create with a BuildKit image.
- [ ] `tests/synth/test_pipeline_assets.py` checks that each image project registers the emulator from the release and builds with the default builder, and that no command of an image project runs, pulls or names an image on Docker Hub.
- [ ] `tests/unit/test_base_images.py` fails when a FROM line or a syntax directive of a Dockerfile git lists names an image on Docker Hub outside an allowed list, grafana/grafana-oss and vllm/vllm-openai, each with its reason, or when a FROM line names an image without its sha256 digest.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_base_images.py tests/synth/test_pipeline_assets.py tests/policy/test_pipeline.py tests/unit/test_ci_workflow.py` passes.
- [ ] `tests/unit/test_base_images.py` refuses, each in a case of its own, a FROM of python by digest with no host, a FROM of docker.io/library/python by digest, a FROM with a tag and no digest, and a syntax directive naming docker/dockerfile.
- [ ] `tests/synth/test_pipeline_assets.py` refuses, each in a case of its own, the docker run of tonistiigi/binfmt and the docker buildx create with image=moby/buildkit, both pinned by digest as the stack had them.
- [ ] `tests/policy/test_pipeline.py` lists, rather than leaves out, an asset-publishing project that downloads the emulator release without its exact version, and one that runs the emulator image pinned by digest.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] The first pipeline execution after the merge passes its Assets stage, and no line of that stage's CodeBuild logs names docker.io other than a pull of grafana/grafana-oss or vllm/vllm-openai (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2 (step 4) and 9, and section 17 (the cooling-off)
- `src/rhi/pipeline/stack.py`, `IMAGE_BUILDER`, and issue #118, which added it
- `tests/policy/test_pipeline.py`, `INSTALLS_THE_IMAGE_BUILDER`
- `.github/dependabot.yml`, the docker entry
- `docs/adr/0001-software-factory.md`, decision 10
