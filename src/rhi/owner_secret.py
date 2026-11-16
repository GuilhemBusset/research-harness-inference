"""The Apps step of the owner's walkthrough (setup/owner-setup.sh): the status reporter's
credentials, written to the Secrets Manager secret the reporter reads.

    python -m rhi.owner_secret <secret> --app-id <n> --installation-id <n> --private-key-file <file>

The secret's value is one JSON object with the fields `app_id`, `installation_id` and
`private_key`. The two IDs are stored as strings of digits, the private key as the text of its
file. The secret is created when it is absent, and its value is replaced when it exists.

Nothing this module prints holds a value: it names the secret and its fields. What AWS answers is
never repeated, since an error message of AWS can name the account.

That the key file lies outside the working tree is the walkthrough's check, made before it starts
this module. The module reaches AWS with the caller's credentials and region.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

FIELDS = ("app_id", "installation_id", "private_key")


def _digits(text: str) -> str:
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError("it must be a number")
    return text


def store(name: str, value: str, client: Any = None) -> str:
    """Writes the value to the secret, and returns `created` or `updated`. `client` is a Secrets
    Manager client of the AWS SDK; without one, the caller's credentials and region make it.
    Raises `RuntimeError`, in this module's own words, when AWS refuses."""
    # Imported here: only the writing loads the SDK.
    import boto3
    import botocore.exceptions

    errors = (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError)
    try:
        client = boto3.client("secretsmanager") if client is None else client
    except botocore.exceptions.NoRegionError:
        raise RuntimeError("no Secrets Manager client can be made: the AWS configuration must give a region") from None
    except errors as error:
        # The class names the cause (a missing dependency, a profile that cannot be read); its
        # text is the SDK's and is not repeated.
        raise RuntimeError(f"no Secrets Manager client can be made: the AWS SDK raised {type(error).__name__}") from None
    try:
        try:
            client.create_secret(Name=name, SecretString=value)
        except botocore.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ResourceExistsException":
                raise
        else:
            return "created"
        client.put_secret_value(SecretId=name, SecretString=value)
    except errors:
        raise RuntimeError(f"the secret {name} cannot be written") from None
    return "updated"


def main(arguments: Sequence[str] | None = None, client: Any = None) -> int:
    """Stores the three fields in the secret. `client` is as for `store`. Returns the exit status:
    0, or 1 when the key file cannot be used or AWS refused."""
    parser = argparse.ArgumentParser(prog="python -m rhi.owner_secret", description=__doc__.partition("\n\n")[0])
    parser.add_argument("secret", help="the secret's name")
    parser.add_argument("--app-id", required=True, type=_digits, help="the App's ID, as GitHub gives it")
    parser.add_argument("--installation-id", required=True, type=_digits, help="the ID of the App's installation")
    parser.add_argument("--private-key-file", required=True, help="the App's private key, kept outside the tree")
    options = parser.parse_args(arguments)
    try:
        private_key = Path(options.private_key_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        print("refused: the private key file cannot be read", file=sys.stderr)
        return 1
    if not private_key.strip():
        print("refused: the private key file is empty", file=sys.stderr)
        return 1
    fields = dict(zip(FIELDS, (options.app_id, options.installation_id, private_key), strict=True))
    try:
        outcome = store(options.secret, json.dumps(fields), client)
    except RuntimeError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    print(f"the secret {options.secret} is {outcome}, with the fields {', '.join(FIELDS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
