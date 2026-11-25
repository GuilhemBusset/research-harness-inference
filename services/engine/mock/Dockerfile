# The mock engine's image (system design, sections 4.1 and 4.2).
#
# Built from the context src/rhi/packaging.py's `image_context("mock-engine", ...)` gives: this
# file, requirements.txt (the mock-engine group of uv.lock, with hashes), rhi_common/ and
# rhi_mock_engine/.
#
# The base is pinned by the digest of its multi-architecture index, which is published for
# linux/arm64 and linux/amd64 among others. No platform is fixed here: `--platform linux/arm64`
# (the pipeline, for the mock pool) and a build with none (CI, on its runner's own platform) each
# take their own image from the same index. An update is a change to this line.
FROM python:3.13.14-slim-trixie@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Every library at its locked version, each wheel checked against its hashes; nothing resolved,
# nothing compiled.
COPY requirements.txt .
RUN pip install --require-hashes --no-deps --only-binary :all: --requirement requirements.txt

COPY rhi_common/ rhi_common/
COPY rhi_mock_engine/ rhi_mock_engine/

USER 65534:65534

EXPOSE 8000

# Exec form: Python is process 1 and receives SIGTERM itself, so requests in flight finish. An
# entry point with no command: the mock takes no argument, and a pool's release gives it none.
ENTRYPOINT ["python", "-m", "rhi_mock_engine.app"]
