"""`rhi revoke`: revoke this installation's key, or every key of the student (system design, section 6.4).

`rhi revoke` sends the stored key to the issuer's `POST /auth/revoke`, in `Authorization: Bearer`
and with nothing else: the key is what authenticates its own revocation. Once the issuer has
answered, the key is removed from the store. It goes to the stored endpoint only, the one that
issued it.

`rhi revoke --all` must work when a key is lost, so it uses none. It runs a fresh device flow, as
`rhi login` does (`rhi_cli.login.start_device_flow`): the student confirms a code with GitHub, and
the device code goes to the issuer's `POST /auth/revoke-all`, which revokes every key of that
student. Any key this installation still stores is then removed. With nothing stored at all, a new
machine say, the endpoint's address is given with `--endpoint`; it is not stored.

A revoked key stops working within 60 seconds, the gateway's lookup cache. The key is never
printed.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from typing import TextIO

from rhi_cli import login, store

REVOKE_PATH = "/auth/revoke"
REVOKE_ALL_PATH = "/auth/revoke-all"

NO_ENDPOINT = (
    "No endpoint is stored. Run `rhi revoke --all --endpoint <address>`, "
    "with the address from the course's private channel."
)


def revoke(*, transport: login.Transport = login.http_transport, out: TextIO | None = None) -> None:
    """Revoke the stored key and remove it from the store; `LoginError` when it fails."""
    out = out or sys.stdout
    address, key = login.signed_in()
    status, body = transport(login.Call("POST", address + REVOKE_PATH, headers={"Authorization": f"Bearer {key}"}))
    if status != 200:
        raise login.LoginError(f"the endpoint did not revoke the key: {login.refusal(status, body)}")
    store.delete_key()
    print("Your key is revoked and removed from this machine. It stops working within a minute.", file=out)


def revoke_all(
    endpoint: str | None = None,
    *,
    transport: login.Transport = login.http_transport,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
) -> None:
    """Revoke every key of the student who confirms a fresh device flow, and remove the stored one."""
    out = out or sys.stdout
    address = login.address_of(endpoint, store.load_config())
    if not address:
        raise login.LoginError(NO_ENDPOINT)
    flow = login.start_device_flow(address, transport, out)
    request = login.Call("POST", address + REVOKE_ALL_PATH, {"device_code": flow.device_code})
    answer = login.redeem(flow, request, "rhi revoke --all", transport=transport, sleep=sleep, clock=clock)
    store.delete_key()
    count = answer.get("revoked") if isinstance(answer, dict) else None
    revoked = f"{count} of your keys" if isinstance(count, int) else "Every key of yours"
    print(f"{revoked} revoked. They stop working within a minute. Run rhi login for a new one.", file=out)


def _run(arguments: argparse.Namespace) -> int:
    if arguments.endpoint is not None and not arguments.all:
        print("rhi revoke: --endpoint goes with --all; your own key is revoked where it was issued.", file=sys.stderr)
        return 2
    try:
        if arguments.all:
            revoke_all(arguments.endpoint, transport=login.http_transport, sleep=time.sleep)
        else:
            revoke(transport=login.http_transport)
    except (login.LoginError, store.StoreError) as error:
        print(f"rhi revoke: {error}", file=sys.stderr)
        return 1
    return 0


def register(commands: argparse._SubParsersAction) -> None:
    """Add `rhi revoke` to the CLI's commands."""
    parser = commands.add_parser("revoke", help="revoke your stored key, or every key of yours with --all")
    parser.add_argument(
        "--all",
        action="store_true",
        help="revoke every key of yours, after a fresh confirmation with GitHub; it needs no stored key",
    )
    parser.add_argument("--endpoint", help="with --all, the endpoint's address when none is stored")
    parser.set_defaults(run=_run)
