"""Copies each GPU pool's weights into the model bucket: the bucket's only writer (system design,
section 4.4).

For each GPU pool of the configuration, a pool whose engine is not the mock, the job copies the
files of the manifest of the pool's model (`weights.py`) from Hugging Face, at the pool's
revision, to the pool's prefix at that revision in the model bucket, `<pool>/<revision>/`.

**Nothing is copied unless every pool is in order.** Before its first request to Hugging Face or
to the bucket, the job loads the committed manifest of every GPU pool's model, which holds only
paths the allow-list admits, and compares its revision with the pool's. A pool whose model has no
manifest, or whose revision is not its manifest's, fails the job with nothing copied for any
pool. A configuration with no GPU pool copies nothing and passes, so the step passes before any
GPU pool is configured.

**A file is verified before it is written.** Each file is downloaded to the build's temporary
directory, anonymously and over HTTPS, and held to the manifest's size and SHA-256
(`weights.verify`). Only then is it uploaded. A file that fails is not written, and fails the job.

**The job never reads an object of the bucket**, whose reads only the VPC's endpoint may make
(`rhi.pools.model_store`). It learns what a revision's prefix holds by listing it. A key already
there at the manifest's size is skipped: only this job writes, an object appears once its upload
is complete, and every upload was verified first. A key there at another size is written again.
So a job that stopped part-way is run again, and copies what is missing.

**It removes from that prefix what the manifest does not list**, once the manifest's files are
there, and touches no other prefix: a new revision is written beside the one in service, and
older revisions stay in the bucket.

What it reads: the configuration, from SSM, with the loader of `rhi.config`; the manifests, from
the checkout; and the bucket's name from `RHI_MODEL_BUCKET`, which the pipeline stack builds with
`rhi.pools.model_store.bucket_name`. It prints the pools' names and the files' paths, and never a
message of AWS.

Run by the pipeline's step `CopyWeights` (`src/rhi/pipeline/weights.py`), before the stage's
deployment, with PYTHONPATH holding the import path of `pyproject.toml`:

    uv run --locked python scripts/pipeline/copy_weights.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

import weights
from rhi import config

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from rhi_common.pool_config import Pool

NAME = "copy_weights"
# The variable that names the model bucket.
BUCKET_VARIABLE = "RHI_MODEL_BUCKET"
# The engine of a pool that needs no GPU, and so no weights (`rhi.pools.image`, which imports the
# CDK and is not imported here).
MOCK_ENGINE = "mock"


class Failed(Exception):
    """A call to the model bucket that failed, said without AWS's own message."""


@dataclass(frozen=True)
class Copy:
    """What the job copies for one GPU pool: the files of `manifest`, under `prefix`."""

    pool: str
    manifest: weights.Manifest

    @property
    def prefix(self) -> str:
        """The pool's prefix at its revision, the layout of `rhi.pools.model_store`."""
        return f"{self.pool}/{self.manifest.revision}/"


def planned(pools: Iterable[Pool], manifests: Path) -> tuple[list[Copy], list[str]]:
    """What to copy for the GPU pools among `pools`, and what is wrong with those that are not in
    order: a pool whose model has no manifest under `manifests`, or another revision than its
    manifest's."""
    copies, refusals = [], []
    for pool in pools:
        if pool.engine == MOCK_ENGINE:
            continue
        try:
            if pool.model is None or pool.revision is None:
                raise weights.Refused("it names no model repository at a revision")
            manifest = weights.load(pool.model, manifests)
            if manifest.revision != pool.revision:
                raise weights.Refused(f"its revision is not the one in the manifest of {pool.model}")
        except weights.Refused as refusal:
            refusals.append(f"pool {pool.name}: {refusal}")
        else:
            copies.append(Copy(pool.name, manifest))
    return copies, refusals


def _https_only(request: httpx.Request) -> None:
    if request.url.scheme != "https":
        raise weights.Refused(f"a request to {request.url.host} would not go over HTTPS")


def _s3(client: Any) -> tuple[Any, tuple[type[Exception], ...]]:
    """The S3 client, the caller's or one made with the build's own credentials and region, and
    the errors a call to it raises. Their messages are AWS's: every caller replaces them."""
    # Imported here: a job with nothing to copy does not load the SDK's S3 client.
    import boto3
    import boto3.exceptions
    import botocore.exceptions

    errors = (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError, boto3.exceptions.Boto3Error)
    try:
        return (boto3.client("s3") if client is None else client), errors
    except errors as error:
        raise Failed(f"no S3 client can be made: the AWS SDK raised {type(error).__name__}") from None


def held(s3: Any, bucket: str, prefix: str) -> dict[str, int]:
    """The keys the bucket holds under a prefix, each with its size, from a listing."""
    found: dict[str, int] = {}
    request = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**request)
        found.update({entry["Key"]: entry["Size"] for entry in page.get("Contents", [])})
        if not page.get("IsTruncated"):
            return found
        request["ContinuationToken"] = page["NextContinuationToken"]


def download(client: httpx.Client, manifest: weights.Manifest, file: weights.File, to: Path) -> None:
    """Downloads a file of the manifest at the manifest's revision into `to`. Refuses one that has
    not the manifest's size and SHA-256."""
    url = f"{weights.HUB}/{manifest.repository}/resolve/{manifest.revision}/{quote(file.path)}"
    with client.stream("GET", url) as response, to.open("wb") as content:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            content.write(chunk)
    if to.stat().st_size != file.size:
        raise weights.Refused(f"{file.path}: its size is not the one in the manifest of {manifest.repository}")
    weights.verify(manifest, file.path, to)


def copy(each: Copy, *, client: httpx.Client, s3: Any, errors: tuple[type[Exception], ...], bucket: str) -> None:
    """Copies one pool's files, then removes from the pool's prefix at its revision what the
    manifest does not list."""
    doing = f"listing {each.prefix}"
    try:
        present = held(s3, bucket, each.prefix)
        with tempfile.TemporaryDirectory() as folder:
            for file in each.manifest.files:
                key = each.prefix + file.path
                if present.get(key) == file.size:
                    print(f"{NAME}: {each.pool}: already there: {file.path}")
                    continue
                downloaded = Path(folder) / "download"
                download(client, each.manifest, file, downloaded)
                doing = f"writing {key}"
                s3.upload_file(Filename=str(downloaded), Bucket=bucket, Key=key)
                print(f"{NAME}: {each.pool}: copied: {file.path}")
        listed = {each.prefix + file.path for file in each.manifest.files}
        for key in sorted(present.keys() - listed):
            doing = f"removing {key}"
            s3.delete_object(Bucket=bucket, Key=key)
            print(f"{NAME}: {each.pool}: removed, the manifest does not list it: {key.removeprefix(each.prefix)}")
    except errors as error:
        raise Failed(f"{doing} in the model bucket failed: the AWS SDK raised {type(error).__name__}") from None


def main(
    environ: Mapping[str, str] | None = None,
    *,
    ssm: Any = None,
    s3: Any = None,
    transport: httpx.BaseTransport | None = None,
    manifests: Path = weights.MANIFESTS,
) -> int:
    """Copies every GPU pool's weights; 0 once each pool's prefix at its revision holds its
    manifest's files and nothing else. `ssm` and `s3` are clients of the AWS SDK; without them,
    the build's own credentials and region make them. `transport` is how a test answers in Hugging
    Face's place."""
    env = os.environ if environ is None else environ
    bucket = env.get(BUCKET_VARIABLE)
    if not bucket:
        print(f"{NAME}: {BUCKET_VARIABLE} is not set: it names the model bucket", file=sys.stderr)
        return 1
    try:
        pools = config.read_ssm(ssm).pools
    except config.ConfigError as error:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 1
    copies, refusals = planned(pools, manifests)
    if refusals:
        for refusal in refusals:
            print(f"{NAME}: {refusal}", file=sys.stderr)
        print(f"{NAME}: nothing is copied, for any pool", file=sys.stderr)
        return 1
    if not copies:
        print(f"{NAME}: the configuration has no GPU pool: nothing to copy")
        return 0
    try:
        bucket_client, errors = _s3(s3)
        # No token and no credentials: the client sends none unless given one.
        with httpx.Client(
            transport=transport, follow_redirects=True, timeout=60, event_hooks={"request": [_https_only]}
        ) as client:
            for each in copies:
                copy(each, client=client, s3=bucket_client, errors=errors, bucket=bucket)
    except httpx.HTTPStatusError as error:
        print(f"{NAME}: Hugging Face answered {error.response.status_code}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"{NAME}: no answer from Hugging Face ({type(error).__name__})", file=sys.stderr)
        return 1
    except (weights.Refused, Failed) as failure:
        print(f"{NAME}: {failure}", file=sys.stderr)
        return 1
    for each in copies:
        size = sum(file.size for file in each.manifest.files)
        print(f"{NAME}: {each.pool}: {each.prefix} holds {len(each.manifest.files)} files, {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
