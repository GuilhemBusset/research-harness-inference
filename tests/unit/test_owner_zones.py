"""The zone check of the owner's configuration tool (src/rhi/owner_config.py): before `write`
puts any parameter, it asks EC2 whether the configuration's zones are the region's and offer the
pools' instance types. EC2 and SSM are stand-ins, so no test here reaches AWS."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import boto3
import botocore.exceptions
import botocore.parsers
import botocore.session
import pytest

from rhi import config, owner_config

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

EXAMPLE = (config.REPO_ROOT / config.EXAMPLE_FILE).read_text(encoding="utf-8")
# The two lists as the example gives them: the VPC's zones, then the coursework pool's.
KEY_LINE = 'zones = ["example-zone-a", "example-zone-b", "example-zone-c", "example-zone-d"]\n'
POOL_LINE = 'zones = ["example-zone-a", "example-zone-b", "example-zone-d"]\n'
# The zones of the stand-in's region, by name: one more than the example lists.
REGION = tuple(f"example-zone-{letter}" for letter in "abcde")
# A zone of another type, which sorts before every zone of the region: a check that asked for all
# zone types would count it among the region's first three.
LOCAL_ZONE = "example-local-zone-y"
# The instance types of the example's two pools, and a fallback type a test adds to the coursework pool.
GPU, SMALL, FALLBACK = "g7e.2xlarge", "t4g.small", "g7e.4xlarge"
# Part of the text of every error the stand-in raises, which no refusal may repeat.
MARK = "7701"


def changed(text: str, *changes: tuple[str, str]) -> str:
    """The text with each change made: a piece of it, found once, replaced by another."""
    for old, new in changes:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


# The example as a configuration written before the lists: neither the key nor a pool's field.
NEITHER = changed(EXAMPLE, (KEY_LINE, ""), (POOL_LINE, ""))
# The example with a fallback type on its coursework pool, priced under the limit.
WITH_A_FALLBACK = changed(
    EXAMPLE,
    (f'instance_types = ["{GPU}"]', f'instance_types = ["{GPU}", "{FALLBACK}"]'),
    (f'prices = {{ "{GPU}" = 3.36 }}', f'prices = {{ "{GPU}" = 3.36, "{FALLBACK}" = 4.5 }}'),
    (f'allowed_instance_types = ["{SMALL}", "{GPU}"]', f'allowed_instance_types = ["{SMALL}", "{GPU}", "{FALLBACK}"]'),
)


class Zones:
    """An EC2 client, as far as the zone check uses one: the zones of a region, given in no order
    and filtered by type as EC2 filters them, and the offers of the instance types asked for, two
    a page. Every zone offers every type, but for the (zone, type) pairs of `without`. `failing`
    is an operation that raises, as EC2 refuses a call."""

    def __init__(
        self, region: Iterable[str] = REGION, without: Iterable[tuple[str, str]] = (), failing: str | None = None
    ) -> None:
        self.region = tuple(region)
        self.without = set(without)
        self.failing = failing
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _called(self, operation: str, request: dict[str, Any]) -> None:
        self.calls.append((operation, request))
        if operation == self.failing:
            text = f"refused {MARK}: {self.region[0]} {GPU}"
            raise botocore.exceptions.ClientError({"Error": {"Code": text, "Message": text}}, operation)

    def describe_availability_zones(self, **request: Any) -> dict[str, Any]:
        self._called("describe_availability_zones", request)
        zones = [(LOCAL_ZONE, "local-zone"), *((name, "availability-zone") for name in reversed(self.region))]
        for asked in request.get("Filters", []):
            assert asked["Name"] == "zone-type"
            zones = [(name, kind) for name, kind in zones if kind in asked["Values"]]
        return {"AvailabilityZones": [{"ZoneName": name, "ZoneType": kind} for name, kind in zones]}

    def describe_instance_type_offerings(self, **request: Any) -> dict[str, Any]:
        self._called("describe_instance_type_offerings", request)
        (asked,) = request["Filters"]
        assert asked["Name"] == "instance-type"
        assert asked["Values"], "EC2 refuses a filter with no value"
        # Asked for no location type, EC2 answers for the region, not for its zones.
        by_zone = request.get("LocationType") == "availability-zone"
        offers = [
            {"InstanceType": kind, "LocationType": "availability-zone", "Location": zone if by_zone else "example"}
            for zone in self.region
            for kind in asked["Values"]
            if (zone, kind) not in self.without
        ]
        start = int(request.get("NextToken", "0"))
        page: dict[str, Any] = {"InstanceTypeOfferings": offers[start : start + 2]}
        if start + 2 < len(offers):
            page["NextToken"] = str(start + 2)
        return page


class Parameters:
    """An SSM client, as far as the writer uses one: it records what is put."""

    def __init__(self) -> None:
        self.put: list[dict[str, Any]] = []

    def put_parameter(self, **request: Any) -> dict[str, Any]:
        self.put.append(request)
        return {"Version": 1}


@pytest.fixture(autouse=True)
def _no_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_arguments: object, **_options: object) -> None:
        raise AssertionError("a test asked the SDK for a real client")

    monkeypatch.setattr(boto3, "client", refuse)


class Written:
    """One run of `write` on a file of the given text: its exit status, what it put, and what it printed."""

    def __init__(self, folder: Path, capsys: pytest.CaptureFixture[str], text: str, ec2: Zones) -> None:
        file = folder / "owner.toml"
        file.write_text(text, encoding="utf-8")
        ssm = Parameters()
        self.status = owner_config.main(["write", str(file)], ssm, ec2)
        self.put = ssm.put
        printed = capsys.readouterr()
        self.printed = printed.out + printed.err
        self.loaded = config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(file)})

    def accepted(self) -> bool:
        """Whether everything the configuration writer writes was put. Anything else is a refusal:
        exit status 1, no parameter put, and a text that holds no zone's name and no instance type."""
        if self.status == 0:
            expected = Parameters()
            config.write_ssm(self.loaded, expected)
            assert self.put == expected.put != []
            return True
        assert self.status == 1
        assert self.put == []
        assert self.printed.startswith("refused: ")
        named = {LOCAL_ZONE, *REGION, *(self.loaded.zones or ()), *self.loaded.allowed_instance_types}
        assert not [value for value in named if value in self.printed]
        assert not re.search(r"example-|zone-|\.[0-9]*x?(large|small)", self.printed)
        assert MARK not in self.printed
        return False


@pytest.fixture
def write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Any:
    return lambda text, ec2: Written(tmp_path, capsys, text, ec2)


def test_the_example_is_written_when_every_zone_offers_every_type(write: Any) -> None:
    ec2 = Zones()
    assert write(EXAMPLE, ec2).accepted()

    # The two calls, each as the check needs it: the region's zones of the type availability-zone,
    # then the offers by zone of the instance types the pools list, to the last page.
    zones, *offerings = ec2.calls
    assert zones == (
        "describe_availability_zones",
        {"Filters": [{"Name": "zone-type", "Values": ["availability-zone"]}]},
    )
    assert len(offerings) > 1
    assert {operation for operation, _ in offerings} == {"describe_instance_type_offerings"}
    for _, request in offerings:
        assert request["LocationType"] == "availability-zone"
        assert request["Filters"] == [{"Name": "instance-type", "Values": sorted([GPU, SMALL])}]
    assert [request.get("NextToken") for _, request in offerings] == [None, *map(str, range(2, 2 * len(offerings), 2))]


@pytest.mark.parametrize(
    ("text", "without", "named"),
    [
        # The coursework pool lists the first, the second and the fourth zone of the VPC.
        pytest.param(EXAMPLE, ("example-zone-d", GPU), ("'coursework'", "third zone", "first instance type"), id="own"),
        pytest.param(EXAMPLE, ("example-zone-a", GPU), ("'coursework'", "first zone", "first instance type"), id="1st"),
        # A fallback type is a type the group may launch: every zone of the pool offers it too.
        pytest.param(
            WITH_A_FALLBACK,
            ("example-zone-b", FALLBACK),
            ("'coursework'", "second zone", "second instance type"),
            id="fallback",
        ),
    ],
)
def test_a_pool_zone_that_does_not_offer_one_of_the_pools_types_is_refused(
    write: Any, text: str, without: tuple[str, str], named: tuple[str, ...]
) -> None:
    refused = write(text, Zones(without=[without]))
    assert not refused.accepted()
    assert all(name in refused.printed for name in named), refused.printed
    # A zone the pool does not list may lack its type: that is why the pool lists its zones.
    assert write(text, Zones(without=[("example-zone-c", GPU), ("example-zone-e", GPU)])).accepted()
    assert write(WITH_A_FALLBACK, Zones(without=[("example-zone-c", FALLBACK)])).accepted()


def test_a_zone_that_is_not_the_regions_is_refused(write: Any) -> None:
    # The region has no fourth zone of that name: its zones end in a, b, c and e.
    refused = write(EXAMPLE, Zones(region=(*REGION[:3], REGION[4])))
    assert not refused.accepted()
    assert "'zones'" in refused.printed
    assert "fourth zone" in refused.printed
    # Nor is a zone of another type, which EC2 lists only when asked for every type.
    refused = write(changed(EXAMPLE, (KEY_LINE, KEY_LINE.replace('"]', f'", "{LOCAL_ZONE}"]'))), Zones())
    assert not refused.accepted()
    assert "fifth zone" in refused.printed


@pytest.mark.parametrize(
    ("key", "pool", "named"),
    [
        # Two of the first three the other way round: a subnet would move to another zone.
        pytest.param(("a", "c", "b", "d"), ("a", "b", "d"), "second zone", id="another-order"),
        # The region's first zone by name left out: the deployed VPC spans it.
        pytest.param(("b", "c", "d", "e"), ("b", "d"), "first zone", id="not-the-first"),
        pytest.param(("a", "b", "d", "c"), ("a", "b", "d"), "third zone", id="the-fourth-as-third"),
    ],
)
def test_a_list_that_does_not_begin_with_the_regions_first_three_is_refused(
    write: Any, key: tuple[str, ...], pool: tuple[str, ...], named: str
) -> None:
    def line(letters: tuple[str, ...]) -> str:
        return "zones = [" + ", ".join(f'"example-zone-{letter}"' for letter in letters) + "]\n"

    # Every zone is the region's and offers every type: only the order of the list is refused.
    refused = write(changed(EXAMPLE, (KEY_LINE, line(key)), (POOL_LINE, line(pool))), Zones())
    assert not refused.accepted()
    assert "'zones'" in refused.printed
    assert named in refused.printed


def test_a_pool_without_its_own_list_is_held_to_the_first_three_of_the_key(write: Any) -> None:
    # The mock pool lists no zones. The fourth zone of the key is not one of its own.
    assert write(EXAMPLE, Zones(without=[("example-zone-d", SMALL), ("example-zone-e", SMALL)])).accepted()
    for position, zone in (("first", "example-zone-a"), ("third", "example-zone-c")):
        refused = write(EXAMPLE, Zones(without=[(zone, SMALL)]))
        assert not refused.accepted()
        assert "'mock'" in refused.printed
        assert "'zones'" in refused.printed
        assert f"the {position} of the first three" in refused.printed
        assert "first instance type" in refused.printed

    # So is the coursework pool, as its entry was before it listed its zones: refused for the
    # third zone, which its list now leaves out.
    key_alone = changed(EXAMPLE, (POOL_LINE, ""))
    refused = write(key_alone, Zones(without=[("example-zone-c", GPU)]))
    assert not refused.accepted()
    assert "'coursework'" in refused.printed
    assert "third" in refused.printed
    assert write(key_alone, Zones(without=[("example-zone-d", GPU)])).accepted()


def test_without_any_list_a_pool_is_held_to_the_regions_first_three_by_name(write: Any) -> None:
    # The stand-in gives the region's zones last first: by name, the first three end in a, b and c.
    later = [(zone, kind) for zone in REGION[3:] for kind in (GPU, SMALL)]
    assert write(NEITHER, Zones(without=later)).accepted()
    for position, zone, kind, pool in (
        ("third", "example-zone-c", GPU, "coursework"),
        ("second", "example-zone-b", SMALL, "mock"),
    ):
        refused = write(NEITHER, Zones(without=[*later, (zone, kind)]))
        assert not refused.accepted()
        assert repr(pool) in refused.printed
        assert f"the {position} of the region's first three" in refused.printed
        assert "first instance type" in refused.printed


def test_a_call_ec2_refuses_is_named_and_its_text_is_not_repeated(write: Any) -> None:
    for operation, call in (
        ("describe_availability_zones", "DescribeAvailabilityZones"),
        ("describe_instance_type_offerings", "DescribeInstanceTypeOfferings"),
    ):
        refused = write(EXAMPLE, Zones(failing=operation))
        assert not refused.accepted()  # which holds that nothing of the error's text is printed
        assert call in refused.printed


def answering(operation: str, body: bytes) -> Any:
    """What the SDK makes of an answer of EC2 to the operation that holds `body`: the answer as the
    SDK's own parser reads it, or the error that parser raises."""
    model = botocore.session.get_session().get_service_model("ec2")
    shape = model.operation_model(operation).output_shape
    parser = botocore.parsers.create_parser(model.protocol)
    return lambda **_request: parser.parse({"body": body, "status_code": 200, "headers": {}}, shape)


def test_an_answer_without_offers_refuses_the_first_pool_zone(write: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # EC2 offers none of the types asked for, and the SDK leaves the empty list out of its answer.
    ec2 = Zones()
    body = b"<DescribeInstanceTypeOfferingsResponse><requestId>example</requestId></DescribeInstanceTypeOfferingsResponse>"
    monkeypatch.setattr(ec2, "describe_instance_type_offerings", answering("DescribeInstanceTypeOfferings", body))
    assert "InstanceTypeOfferings" not in ec2.describe_instance_type_offerings()
    refused = write(EXAMPLE, ec2)
    assert not refused.accepted()
    assert all(name in refused.printed for name in ("'coursework'", "first zone", "first instance type"))

    # A region that answers with no zone at all: the first zone of the key is not one of its zones.
    body = b"<DescribeAvailabilityZonesResponse><requestId>example</requestId></DescribeAvailabilityZonesResponse>"
    monkeypatch.setattr(ec2, "describe_availability_zones", answering("DescribeAvailabilityZones", body))
    refused = write(EXAMPLE, ec2)
    assert not refused.accepted()
    assert "first zone of the key 'zones'" in refused.printed


def test_an_answer_the_sdk_cannot_read_is_named_by_its_call_and_not_repeated(
    write: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK's parser raises an error of its own, outside the SDK's two families, and its text
    # holds the answer. An answer that is read and lacks a name is not repeated either.
    for operation, call, unreadable in (
        ("describe_availability_zones", "DescribeAvailabilityZones", {"AvailabilityZones": [{"ZoneId": MARK}]}),
        (
            "describe_instance_type_offerings",
            "DescribeInstanceTypeOfferings",
            {"InstanceTypeOfferings": [{"InstanceType": GPU, "LocationType": MARK}]},
        ),
    ):
        unparsed = answering(call, f"<example-answer-{MARK}>".encode())
        with pytest.raises(botocore.parsers.ResponseParserError, match=MARK):
            unparsed()
        for answer in (unparsed, lambda unreadable=unreadable, **_request: unreadable):
            ec2 = Zones()
            monkeypatch.setattr(ec2, operation, answer)
            refused = write(EXAMPLE, ec2)
            assert not refused.accepted()  # which holds that nothing of the answer is printed
            assert call in refused.printed


def test_the_ec2_client_is_the_callers_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "owner.toml"
    file.write_text(EXAMPLE, encoding="utf-8")
    ec2, ssm = Zones(), Parameters()
    made: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(boto3, "client", lambda *arguments, **options: made.append((arguments, options)) or ec2)

    # The module names no region, no account and no credential: the SDK finds the caller's.
    assert owner_config.main(["write", str(file)], ssm) == 0
    assert made == [(("ec2",), {})]
    assert ec2.calls
    assert ssm.put

    # No client: the SDK finds no region, or cannot make one. Nothing is written.
    for error, cause in (
        (botocore.exceptions.NoRegionError(), "must give a region"),
        (botocore.exceptions.MissingDependencyException(msg=f"no awscrt {MARK}"), "MissingDependencyException"),
    ):
        monkeypatch.setattr(boto3, "client", lambda *_, error=error: (_ for _ in ()).throw(error))
        capsys.readouterr()
        untouched = Parameters()
        assert owner_config.main(["write", str(file)], untouched) == 1
        printed = capsys.readouterr().err
        assert "EC2 client" in printed
        assert cause in printed
        assert MARK not in printed
        assert untouched.put == []


def test_a_configuration_without_pools_asks_for_no_offerings(write: Any) -> None:
    # The owner's first configuration: `pools = {}`. EC2 refuses a filter with no instance type,
    # so the second call is not made; the key is still checked against the region's zones.
    no_pools = changed(EXAMPLE[: EXAMPLE.index("[pools.coursework]")], ('default_pool = "coursework"\n', "pools = {}\n"))
    ec2 = Zones()
    assert write(no_pools, ec2).accepted()
    assert [operation for operation, _ in ec2.calls] == ["describe_availability_zones"]
    assert not write(no_pools, Zones(region=REGION[1:])).accepted()


def test_check_asks_nothing_and_loads_without_the_sdk(tmp_path: Path) -> None:
    # The walkthrough's dry run runs `check`: it stays offline. The SDK is imported only when the
    # zone check runs, so `check` and `values` load without it.
    file = tmp_path / "owner.toml"
    file.write_text(EXAMPLE, encoding="utf-8")
    assert owner_config.main(["check", str(file)]) == 0  # a client asked for would fail the test

    program = (
        "import sys; from rhi import owner_config;"
        "status = owner_config.main(['check', sys.argv[1]]);"
        "sys.exit(status or 'boto3' in sys.modules or 'botocore' in sys.modules)"
    )
    environment = {"PYTHONPATH": f"{config.REPO_ROOT / 'src'}:{config.REPO_ROOT / 'services/common'}"}
    done = subprocess.run([sys.executable, "-c", program, str(file)], env=environment, capture_output=True, check=False)
    assert done.returncode == 0, done.stderr
