# The gateway's image (system design, sections 2 and 6.2).
#
# Built from the context src/rhi/packaging.py's `image_context("gateway", ...)` gives: this file,
# requirements.txt (the gateway group of uv.lock, with hashes), rhi_common/ and rhi_gateway/.
#
# The base is pinned by the digest of its multi-architecture index, which is published for
# linux/arm64 and linux/amd64 among others. No platform is fixed here: the builder's `--platform`,
# or its own platform when it gives none, takes its image from the same index. An update is a
# change to this line.
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

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
COPY rhi_gateway/ rhi_gateway/

USER 65534:65534

# The application, for the public load balancer. The metrics are served on the loopback address,
# port 9091, to the collector of the same task: that port is not exposed.
EXPOSE 8080

# Exec form: Python is process 1 and receives SIGTERM itself, so the requests in flight finish and
# the heartbeat writes its last rows.
CMD ["python", "-m", "rhi_gateway.main"]
