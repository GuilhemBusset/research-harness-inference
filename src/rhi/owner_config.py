"""The configuration step of the owner's walkthrough (setup/owner-setup.sh): the owner's file,
checked by the configuration loader and written to SSM by the configuration writer. Its second
half writes the file's private values to the hygiene scanner's list of local values.

    python -m rhi.owner_config check <file>   # the loader reads the file; nothing is written
    python -m rhi.owner_config write <file>   # the same check, the zone check, then one parameter per key in SSM
    python -m rhi.owner_config values <file> --list <list>   # the private values, onto the list

The file goes through the loader's own `file` source and to SSM through `write_ssm`
(src/rhi/config.py), so the walkthrough and the pipeline read the same keys the same way. A file
the loader refuses is refused here, before anything is written, in the loader's words: they name
a key or a source and never a value. Nothing this module prints holds a value of the file.

**The zone check.** Whether a zone is one of the region's, and whether it offers an instance type,
only EC2 can say, and synthesis has no AWS access. So `write` asks EC2 before it writes any
parameter, with the caller's credentials and region:

- DescribeAvailabilityZones, filtered to the zone type availability-zone: the region's zones,
  sorted by name. Its first three are the zones the VPC spans when the configuration lists none
  (`rhi.network.stack`);
- DescribeInstanceTypeOfferings, with the location type availability-zone, filtered to the
  instance types the pools list, every page: which zone offers which type. A configuration whose
  pools list no instance type has nothing to ask, and the call is not made.

It refuses a configuration in which a zone of the key `zones` is not one of the region's, in which
that key does not begin with the region's first three zones by name, or in which a zone of a pool
does not offer one of the pool's instance types, its fallbacks included. A pool's zones are its own
list; without one, the first three of the key `zones`; without that key, the region's first three
by name. A refusal names the pool, and the zone and the instance type by their positions in the
configuration, never a zone's name, an instance type or a text of AWS. A call that fails is named,
whatever the SDK raised, and so is one whose answer cannot be read: the error's text, which can
hold EC2's answer, is not repeated. `check` asks nothing: it stays offline.

`values` adds the value of each key of `config.PRIVATE_KEYS`, and each line of its standard
input, to the list the scanner reports as `local-value` (tools/hygiene/README.md, "Values only
the owner knows"): one value per line, in a file only its owner can read.

That the file and the list lie outside the working tree is the walkthrough's check, made before
it starts this module. Only `write` reaches AWS, with the caller's credentials and region: it
needs `ec2:DescribeAvailabilityZones` and `ec2:DescribeInstanceTypeOfferings` for the zone check,
and `ssm:PutParameter` on the configuration's parameters.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# What the scanner (tools/hygiene/scan.py, `load_local_values`) asks of a line of the list: a
# shorter value stops every scan of the machine, and a line that starts with `#` is a comment.
VALUE_MIN = 4


class ListError(Exception):
    """The list of local values was refused. The message never holds a value."""


def _listed(text: str) -> list[str]:
    """The values a list holds, as the scanner reads them."""
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def write_values(loaded: config.Config, further: Iterable[str], path: Path) -> int:
    """Adds the private values of the configuration, then the further ones, to the list at
    `path`, each only if the list does not hold it yet, and returns how many values the list
    then holds. The list is created for its owner alone, and one that exists is made so."""
    values = {f"the value of {key!r}": str(getattr(loaded, key)).strip() for key in sorted(config.PRIVATE_KEYS)}
    entered = [line.strip() for line in further]
    values |= {f"entered value {number}": value for number, value in enumerate(filter(None, entered), start=1)}
    for name, value in values.items():
        if len(value) < VALUE_MIN or value.startswith("#") or len(value.splitlines()) != 1:
            raise ListError(f"{name} cannot go on the list: a value is one line of {VALUE_MIN} characters or more")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "rb+") as file:
            os.fchmod(descriptor, 0o600)
            text = file.read().decode("utf-8-sig")
            listed = _listed(text)
            # The scanner matches a value in any letter case, so that is how one is "on the list".
            held = {value.casefold() for value in listed}
            new = {value.casefold(): value for value in values.values() if value.casefold() not in held}
            lines = "".join(f"{value}\n" for value in new.values())
            if text and not text.endswith("\n"):
                lines = "\n" + lines
            file.write(lines.encode())
    except (OSError, UnicodeDecodeError) as error:
        raise ListError(f"the list cannot be read and written: {error.__class__.__name__}") from error
    return len(set(listed)) + len(new)


class ZoneError(Exception):
    """The zone check refused the configuration, or could not ask EC2. The message holds no zone's
    name, no instance type and no text of AWS."""


_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")


def _ordinal(index: int) -> str:
    """The position of the item at `index` of a list, in words as far as the tenth: `third`, `11th`."""
    if index < len(_ORDINALS):
        return _ORDINALS[index]
    position = index + 1
    ending = "th" if position % 100 in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(position % 10, "th")
    return f"{position}{ending}"


def _ec2(client: Any) -> Any:
    """The EC2 client: the caller's, or one made with the caller's own credentials and region, as
    `config._ssm` makes the SSM client. The text of an error of the SDK is AWS's and is not repeated."""
    if client is not None:
        return client
    # Imported here: the check, the list of values and the module itself load without the SDK.
    import boto3
    import botocore.exceptions

    try:
        return boto3.client("ec2")
    except botocore.exceptions.NoRegionError:
        raise ZoneError("no EC2 client can be made: the caller's AWS configuration must give a region") from None
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as error:
        # The class names the cause; its text is the SDK's and is not repeated.
        raise ZoneError(f"no EC2 client can be made: the AWS SDK raised {type(error).__name__}") from None


def _asked(call: str) -> ZoneError:
    return ZoneError(
        f"the zones cannot be checked: EC2's {call} failed, or its answer cannot be read, "
        "with the caller's credentials and region"
    )


def _region_zones(client: Any) -> list[str]:
    """The region's zones of the type availability-zone, sorted by name."""
    try:
        described = client.describe_availability_zones(
            Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}]
        )
        # The SDK leaves out of an answer a list that EC2 gave no item of.
        return sorted(str(zone["ZoneName"]) for zone in described.get("AvailabilityZones", []))
    except Exception:  # noqa: BLE001 - whatever the SDK raises, its text is AWS's: the call is named instead
        raise _asked("DescribeAvailabilityZones") from None


def _offered(client: Any, kinds: list[str]) -> set[tuple[str, str]]:
    """Which zone offers which of the instance types `kinds`, as (zone, type) pairs, from every
    page of the answer."""
    offered: set[tuple[str, str]] = set()
    request: dict[str, Any] = {
        "LocationType": "availability-zone",
        "Filters": [{"Name": "instance-type", "Values": kinds}],
    }
    try:
        while True:
            page = client.describe_instance_type_offerings(**request)
            # The SDK leaves the list out of an answer that holds no offer: no zone offers the types.
            offers = page.get("InstanceTypeOfferings", [])
            offered |= {(str(offer["Location"]), str(offer["InstanceType"])) for offer in offers}
            if not page.get("NextToken"):
                return offered
            request["NextToken"] = page["NextToken"]
    except Exception:  # noqa: BLE001 - whatever the SDK raises, its text is AWS's: the call is named instead
        raise _asked("DescribeInstanceTypeOfferings") from None


def check_zones(loaded: config.Config, client: Any = None) -> None:
    """Asks EC2 whether the configuration's zones are the region's and offer the pools' instance
    types (the module's docstring, "The zone check"), and raises `ZoneError` for the first that is
    not, or for a call that fails. `client` is an EC2 client of the AWS SDK; without one, the
    caller's credentials and region make it."""
    client = _ec2(client)
    region = _region_zones(client)
    first = tuple(region[: config.FIRST_ZONES])
    kinds = sorted({kind for pool in loaded.pools for kind in pool.instance_types})
    # EC2 refuses a filter with no value: pools that list no instance type have nothing to ask.
    offered = _offered(client, kinds) if kinds else set()

    if loaded.zones is not None:
        for index, zone in enumerate(loaded.zones):
            if zone not in region:
                raise ZoneError(f"the {_ordinal(index)} zone of the key 'zones' is not a zone of the region")
        for index, zone in enumerate(loaded.zones[: config.FIRST_ZONES]):
            if first[index : index + 1] != (zone,):
                raise ZoneError(
                    f"the {_ordinal(index)} zone of the key 'zones' is not the region's {_ordinal(index)} zone by "
                    "name: the key must begin with the region's first three zones by name, which are the zones "
                    "the deployed VPC spans"
                )
    for pool in loaded.pools:
        # The zones the pool's group spans, and what a refusal calls one of them.
        if pool.zones is not None:
            zones, which = pool.zones, "zone it lists"
        elif loaded.zones is not None:
            zones = loaded.zones[: config.FIRST_ZONES]
            which = "of the first three zones of the key 'zones', which it spans since it lists none,"
        else:
            zones = first
            which = "of the region's first three zones by name, which it spans since no zones are listed,"
        for zone_index, zone in enumerate(zones):
            for kind_index, kind in enumerate(pool.instance_types):
                if (zone, kind) not in offered:
                    raise ZoneError(
                        f"the pool {pool.name!r}: the {_ordinal(zone_index)} {which} "
                        f"does not offer its {_ordinal(kind_index)} instance type"
                    )


def main(arguments: Sequence[str] | None = None, client: Any = None, ec2: Any = None) -> int:
    """Checks the file, and writes it to SSM, or its private values to the list, when asked to.
    `client` is as for `config.write_ssm`, and `ec2` as the client of `check_zones`. Returns the
    exit status: 0, or 1 on a refusal."""
    parser = argparse.ArgumentParser(prog="python -m rhi.owner_config", description=__doc__.partition("\n\n")[0])
    parser.add_argument(
        "action",
        choices=("check", "write", "values"),
        help="check the file; check it and write it to SSM; or add its private values to the list",
    )
    parser.add_argument("file", help="the owner's configuration file, kept outside the working tree")
    parser.add_argument("--list", type=Path, help="values: the scanner's list of local values, outside the tree too")
    options = parser.parse_args(arguments)
    held = 0
    if (options.action == "values") != (options.list is not None):
        parser.error("--list goes with values, and only with it")
    try:
        loaded = config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": options.file})
        if options.action == "write":
            # Before any parameter: a configuration the zone check refuses writes nothing.
            check_zones(loaded, ec2)
            config.write_ssm(loaded, client)
        elif options.action == "values":
            # Further values, one per line: the walkthrough passes on what the owner entered.
            held = write_values(loaded, sys.stdin, options.list)
    except (config.ConfigError, ListError, ZoneError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    if options.action == "check":
        print("the configuration loader accepts the file")
    elif options.action == "write":
        print(f"the configuration is written to SSM, under {config.SSM_PREFIX}")
    else:
        print(f"the list of local values holds {held} values, readable by its owner alone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
