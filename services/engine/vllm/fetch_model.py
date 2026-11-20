"""The fallback loader's copy: a model out of S3 to a local folder, before vLLM starts (system
design, section 4.4).

A pool whose loader is `download` is given, first among its engine flags, one argument of this
project, `--rhi-fetch-model=<s3://bucket/prefix/>,<folder>` (`rhi.pools.vllm_command`). The image's
entry point (`entrypoint.sh`) runs this program with that argument, then takes it off and starts
vLLM, which loads the model from the folder with its default loader.

The program copies every object under the prefix to the folder, at its path under the prefix, and
fails, so that vLLM does not start, when it finds none or when any copy fails. It runs inside the
upstream vLLM image with what that image ships: the standard library and boto3, which the image
installs with the Run:ai streamer's S3 extra. boto3 takes the region from `AWS_REGION`, which the
GPU drop-in gives the container, and the instance role's credentials from the instance metadata,
which the host's network lets it reach. The role may list the pool's prefix and read its objects.

This file is not part of a package: the image holds it beside the entry point, and the tests load
it by its path.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import Any

import boto3

OPTION = "--rhi-fetch-model"
SCHEME = "s3://"


class FetchError(Exception):
    """The copy cannot be made. The message names what is wrong, never a credential."""


def parse(argument: str) -> tuple[str, str, Path]:
    """The bucket, the prefix and the local folder that the copy's argument names."""
    option, equals, value = argument.partition("=")
    location, comma, folder = value.partition(",")
    if option != OPTION or not equals or not comma:
        raise FetchError(f"the argument is not {OPTION}=<s3 location>,<folder>")
    if not location.startswith(SCHEME):
        raise FetchError(f"the model's location does not start with {SCHEME}")
    bucket, _, prefix = location.removeprefix(SCHEME).partition("/")
    if not bucket or not folder.startswith("/"):
        raise FetchError("the location names no bucket, or the folder is not an absolute path")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix, Path(folder)


def _target(folder: Path, relative: str) -> Path:
    """Where an object goes under the folder. A key that would leave the folder is refused."""
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or any(part in (".", "..") for part in parts):
        raise FetchError("an object's key does not name a file under the model's location")
    return folder.joinpath(*parts)


def copy(client: Any, bucket: str, prefix: str, folder: Path) -> list[Path]:
    """Copy every object under `prefix` in `bucket` to `folder`, every page of the listing, and
    return the files written. `client` is boto3's S3 client."""
    written: list[Path] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for listed in page.get("Contents", []):
            key = listed["Key"]
            relative = key[len(prefix) :]
            # A key that ends in a slash is a folder's marker, which holds no file.
            if relative.endswith("/") or relative == "":
                continue
            target = _target(folder, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            written.append(target)
    if not written:
        raise FetchError("the model's location holds no file")
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(f"fetch_model: give one argument, {OPTION}=<s3 location>,<folder>", file=sys.stderr)
        return 2
    try:
        bucket, prefix, folder = parse(argv[0])
        folder.mkdir(parents=True, exist_ok=True)
        written = copy(boto3.client("s3"), bucket, prefix, folder)
    except FetchError as refused:
        print(f"fetch_model: {refused}", file=sys.stderr)
        return 1
    print(f"fetch_model: copied {len(written)} files to {folder}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
