# The trial image of the evaluation (docs/design/agent-session-evaluation.md, "Course quality and
# the grader"): the five student harnesses and the solver, each at the repository's pin.
#
# Built from this folder by `evaluate.image.prepare` (setup/README.md, "The evaluation's trial
# image"). The build reads four committed files and nothing else: this file, package.json with
# package-lock.json, and requirements.txt (.dockerignore keeps the rest out of its context).
#
# Both bases are pinned by the digest of their multi-architecture index, which is published for
# linux/amd64 and linux/arm64 among others. No platform is fixed here: a build takes the image of
# its own platform from each index, and both are Debian trixie, so the Node of the first runs on
# the second. An update of a base is a change to its line, and of Node's version to
# `NODE_VERSION` in spikes/harnesses.py with it.

# The four harnesses npm installs (Claude Code, Codex, OpenCode and Pi), on the pinned Node.
FROM node:24.22.3-trixie-slim@sha256:4d00acb4bcc42c454c4eb801526f7ad7f772ea3bd4fd168c5cc2e384abcbb9d4 AS harnesses

WORKDIR /opt/harnesses

# `npm ci` installs exactly what package-lock.json names, each package checked against its
# integrity hash, and resolves nothing. It runs the install scripts package.json allows and no
# other: the ones that put the native binary of Claude Code and of OpenCode in place.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# The image: Python, with the SDKs of the fifth harness (the SDK tool loop) and the solver.
FROM python:3.13.14-slim-trixie@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Every library at its locked version, each wheel checked against its hashes; nothing resolved,
# nothing compiled.
COPY requirements.txt /opt/python/requirements.txt
RUN pip install --require-hashes --no-deps --only-binary :all: --requirement /opt/python/requirements.txt

# Node itself, without npm: nothing is installed in a trial. Then the harnesses, at the path they
# were installed under.
COPY --from=harnesses /usr/local/bin/node /usr/local/bin/node
COPY --from=harnesses /opt/harnesses /opt/harnesses

# A home and a working folder the trial's user can write; everything installed above is root's.
RUN mkdir -p /trial/home /trial/work && chown 65534:65534 /trial/home /trial/work

ENV PATH=/opt/harnesses/node_modules/.bin:$PATH \
    HOME=/trial/home

WORKDIR /trial/work

USER 65534:65534

# No server and no entry point: whoever starts a trial gives its command.
CMD ["python", "--version"]
