"""The admin's export and erase of one student's data (`rhi_admin.privacy`), through the CLI.

The roster, keys and usage tables are moto's. CloudFormation, CloudWatch Logs and Managed
Prometheus are stubbed, and a stand-in answers the workspace's signed queries. Every key is
issued while the tests run: none is written in this file.
"""

from __future__ import annotations

import dataclasses
import datetime
import io
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from rhi_admin import keys as admin_keys
from rhi_admin import main, privacy
from rhi_admin import roster as admin_roster
from rhi_common.keys import key_hash
from rhi_common.students import UsageDay
from rhi_common.tables import KEYS, ROSTER, USAGE, Table
from rhi_gateway.auth import CACHE_SECONDS
from rhi_gateway.keystore import DynamoKeyStore
from rhi_issuer import rules

REPO = Path(__file__).resolve().parents[3]
NOW = 1_800_000_000
# When the two students were put on the roster: their telemetry is read from then on.
ADDED = NOW - 30 * 86_400
COURSE_END = datetime.date(2027, 6, 30)
OTTER, HERON = 4242, 5151
WORKSPACE = "arn:aws:aps:us-east-1:123456789012:workspace/ws-example"
ENDPOINT = "https://example.com/workspaces/ws-example/"
LOG_GROUP = "/rhi/gateway"
# The series the workspace holds: otter's samples an hour and ten days ago, and one of heron's.
SERIES = [
    ({"__name__": "rhi_gateway_requests_total", "alias": "otter", "pool": "mock"}, [(NOW - 3600, "4"), (NOW - 864_000, "1")]),
    ({"__name__": "rhi_gateway_requests_total", "alias": "heron", "pool": "mock"}, [(NOW - 60, "9")]),
]
RECORDS = [{"alias": "otter", "route": "/v1/messages", "status": 200}, {"alias": "heron", "route": "/v1/messages"}]


def _create(dynamodb: Any, table: Table) -> Any:
    def schema(*parts: Any) -> list[dict[str, str]]:
        return [{"AttributeName": p.name, "KeyType": k} for p, k in zip(parts, ("HASH", "RANGE")) if p is not None]

    keyed = {table.partition_key, table.sort_key} | {k for i in table.indexes for k in (i.partition_key, i.sort_key)}
    indexes = [
        {"IndexName": i.name, "KeySchema": schema(i.partition_key, i.sort_key), "Projection": {"ProjectionType": "ALL"}}
        for i in table.indexes
    ]
    return dynamodb.create_table(
        TableName=table.name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": k.name, "AttributeType": k.type.value} for k in keyed if k is not None],
        KeySchema=schema(table.partition_key, table.sort_key),
        **({"GlobalSecondaryIndexes": indexes} if indexes else {}),
    )


def workspace(request: Any, timeout: float) -> io.BytesIO:
    """The workspace's query address: the samples of the selected series in the window that ends at `time`."""
    authorization = request.get_header("Authorization")
    assert authorization.startswith("AWS4-HMAC-SHA256") and "/aps/aws4_request" in authorization
    form = parse_qs(request.data.decode())
    (alias,) = re.findall(r'alias="([a-z]+)"', form["query"][0])
    (window,) = re.findall(r"\[(\d+)s\]", form["query"][0])
    end = float(form["time"][0])
    result = []
    for labels, samples in SERIES:
        inside = [[at, value] for at, value in samples if end - int(window) < at <= end]
        if labels["alias"] == alias and inside:
            result.append({"metric": labels, "values": inside})
    return io.BytesIO(json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}}).encode())


class _Session:
    def __init__(self, real: Any, stubbed: dict[str, Any]) -> None:
        self.real, self.stubbed = real, stubbed

    def client(self, name: str) -> Any:
        return self.stubbed.get(name) or self.real.client(name)

    def resource(self, name: str) -> Any:
        return self.real.resource(name)

    def get_credentials(self) -> Any:
        return self.real.get_credentials()

    @property
    def region_name(self) -> str:
        return self.real.region_name


def _detail(logical_id: str, physical_id: str) -> dict[str, Any]:
    return {
        "StackResourceDetail": {
            "LogicalResourceId": logical_id,
            "PhysicalResourceId": physical_id,
            "ResourceType": "AWS::CloudFormation::CustomResource",
            "LastUpdatedTimestamp": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
            "ResourceStatus": "CREATE_COMPLETE",
        }
    }


@dataclasses.dataclass
class Deployed:
    admin: main.Admin
    usage: Any
    stubs: dict[str, Stubber]
    sleeps: list[float]
    during_sleep: list[Any]

    def run(self, *argv: str) -> tuple[int, str]:
        cloudformation, amp = self.stubs["cloudformation"], self.stubs["amp"]
        for logical_id, stack, physical in (
            (main.WORKSPACE_LOGICAL_ID, main.OBSERVABILITY_STACK_NAME, WORKSPACE),
            (main.USAGE_LOGICAL_ID, main.STACK_NAME, USAGE.name),
            (main.GATEWAY_LOG_GROUP_LOGICAL_ID, main.GATEWAY_STACK_NAME, LOG_GROUP),
        ):
            expected = {"StackName": stack, "LogicalResourceId": logical_id}
            cloudformation.add_response("describe_stack_resource", _detail(logical_id, physical), expected)
        found = {
            "workspaceId": "ws-example",
            "arn": WORKSPACE,
            "status": {"statusCode": "ACTIVE"},
            "createdAt": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
            "prometheusEndpoint": ENDPOINT,
        }
        amp.add_response("describe_workspace", {"workspace": found}, {"workspaceId": "ws-example"})
        out = io.StringIO()
        status = main.run(["privacy", *argv], self.admin, now=NOW, out=out, err=out)
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()
        return status, out.getvalue()

    def rows(self, github_id: int) -> list[dict[str, Any]]:
        tables = (self.admin.roster, self.admin.keys, self.usage)
        found = [item for table in tables for item in table.scan(ConsistentRead=True)["Items"]]
        lock = rules.lock_key(github_id)[KEYS.partition_key.name]
        return [item for item in found if item.get("github_id") == github_id or item.get("key_hash") == lock]


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Deployed]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        real = boto3.session.Session()
        dynamodb = real.resource("dynamodb")
        roster, keys, usage = (_create(dynamodb, table) for table in (ROSTER, KEYS, USAGE))
        clients = {name: real.client(name) for name in ("cloudformation", "logs", "amp")}
        stubs = {name: Stubber(client) for name, client in clients.items()}
        for stub in stubs.values():
            stub.activate()
        for logical_id, table in ((main.ROSTER_LOGICAL_ID, roster), (main.KEYS_LOGICAL_ID, keys)):
            expected = {"StackName": main.STACK_NAME, "LogicalResourceId": logical_id}
            stubs["cloudformation"].add_response("describe_stack_resource", _detail(logical_id, table.name), expected)
        admin = main.connect(_Session(real, clients), urlopen=workspace)
        sleeps: list[float] = []
        during_sleep: list[Any] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if during_sleep:
                during_sleep.pop(0)()

        made = admin.privacy
        admin = dataclasses.replace(admin, privacy=lambda: dataclasses.replace(made(), sleep=sleep))
        for github_id, alias in ((OTTER, "otter"), (HERON, "heron")):
            admin_roster.add(github_id, alias, roster=roster, course_end=COURSE_END, now=ADDED)
        yield Deployed(admin, usage, stubs, sleeps, during_sleep)
        for stub in stubs.values():
            stub.deactivate()


def issue(deployed: Deployed, github_id: int) -> str:
    return admin_keys.issue(
        github_id, roster=deployed.admin.roster, keys=deployed.admin.keys, course_end=COURSE_END, now=NOW - 3600
    )


def use(deployed: Deployed, github_id: int, alias: str, day: str, cookies: dict[str, str] | None = None) -> None:
    row = UsageDay(github_id, day, alias, tokens=1234, expires_at=NOW + 86_400, affinity_cookies=cookies or {})
    deployed.usage.put_item(Item=row.to_item())


def test_the_export_holds_the_student_s_data_and_no_key_and_no_hash(deployed: Deployed) -> None:
    keys = [issue(deployed, OTTER), issue(deployed, OTTER)]
    admin_keys.revoke(OTTER, keys=deployed.admin.keys, now=NOW - 60)
    keys.append(issue(deployed, OTTER))
    use(deployed, OTTER, "otter", "2027-01-14", {"mock": "AWSALB=cookie-of-otter"})
    use(deployed, OTTER, "otter", "2027-01-15")
    use(deployed, HERON, "heron", "2027-01-15")
    events = [{"timestamp": (NOW - 600 + n) * 1000, "message": json.dumps(r)} for n, r in enumerate(RECORDS)]
    expected = {
        "logGroupName": LOG_GROUP,
        "filterPattern": '{ $.alias = "otter" }',
        "startTime": ADDED * 1000,
        "endTime": (NOW + 1) * 1000,
    }
    deployed.stubs["logs"].add_response("filter_log_events", {"events": events[:1], "nextToken": "more"}, expected)
    deployed.stubs["logs"].add_response("filter_log_events", {"events": events[1:]}, {**expected, "nextToken": "more"})

    status, printed = deployed.run("export", str(OTTER))

    assert status == 0
    document = json.loads(printed)
    assert document["entry"]["github_id"] == OTTER and document["entry"]["alias"] == "otter"
    assert [set(key) for key in document["keys"]] == [{"issued_at", "expires_at", "revoked_at"}] * 3
    assert sorted(key["revoked_at"] is None for key in document["keys"]) == [False, False, True]
    assert [(row["github_id"], row["day"]) for row in document["usage"]] == [(OTTER, "2027-01-14"), (OTTER, "2027-01-15")]
    assert document["usage"][0]["affinity_cookies"] == {"mock": "AWSALB=cookie-of-otter"}
    assert [entry["record"] for entry in document["logs"]] == RECORDS[:1]
    ((labels, samples),) = [(s["labels"], s["values"]) for s in document["series"]]
    assert labels == SERIES[0][0] and samples == [[NOW - 864_000, "1"], [NOW - 3600, "4"]]
    for key in keys:
        assert key not in printed and key_hash(key) not in printed
    assert re.search(r"rhi_[0-9A-Za-z]{20,}|[0-9a-f]{64}", printed) is None


def test_after_an_erase_no_table_holds_a_row_of_the_student_and_no_key_verifies(deployed: Deployed) -> None:
    keys = [issue(deployed, OTTER), issue(deployed, OTTER)]
    heron = issue(deployed, HERON)
    use(deployed, OTTER, "otter", "2027-01-15", {"mock": "AWSALB=cookie-of-otter"})
    use(deployed, HERON, "heron", "2027-01-15")
    entry = admin_roster.get(OTTER, roster=deployed.admin.roster)
    assert entry is not None

    def login_in_flight() -> None:
        # A login that read the entry before the erase removed it stores its key afterwards.
        minted = rules.issue(entry, "installation-of-otter", course_end=COURSE_END, now=NOW, keys=deployed.admin.keys)
        keys.append(minted.key)

    def request_from_the_cache() -> None:
        # The gateway, which still had a key in its cache, reserves for a request of the student.
        use(deployed, OTTER, "otter", "2027-01-16")

    deployed.during_sleep.extend([login_in_flight, request_from_the_cache])

    status, printed = deployed.run("erase", str(OTTER))

    assert status == 0, printed
    assert deployed.rows(OTTER) == []
    store = DynamoKeyStore(deployed.admin.keys)
    assert [store.lookup(key_hash(key)) for key in keys] == [None, None, None]
    assert store.lookup(key_hash(heron)) is not None
    assert len(deployed.rows(HERON)) == 3
    # The logins in flight, then the gateway's cache, are waited out before anything is deleted
    # (tests/synth/test_issuer.py holds the first wait to the issuer's timeout).
    assert deployed.sleeps == [privacy.ISSUER_SECONDS, privacy.GATEWAY_SECONDS]
    assert privacy.GATEWAY_SECONDS > CACHE_SECONDS
    retention = printed.split("Removed by retention only")[1]
    for named in ("workspace's series", "gateway's log records", "load balancers' access logs", "WAF's logs"):
        assert named in retention
    assert "3 key(s)" in printed and "2 usage row(s)" in printed
    assert f"No new roster entry is given the alias otter for {privacy.ALIAS_HELD_DAYS} days" in printed


def test_an_erased_alias_is_held_back_from_any_new_entry_while_its_telemetry_is_kept(
    deployed: Deployed, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert deployed.run("erase", str(OTTER))[0] == 0
    roster = deployed.admin.roster

    # Its series and log records cannot tell two students apart: no one else is given them, even
    # while the alias index, which DynamoDB updates after the write, shows neither entry nor hold.
    with monkeypatch.context() as patch:
        query = roster.query
        patch.setattr(roster, "query", lambda **q: {"Items": []} if "IndexName" in q else query(**q))
        with pytest.raises(admin_roster.AdminError, match="held back"):
            admin_roster.add(6060, "otter", roster=roster, course_end=COURSE_END, now=NOW)
    (held,) = [item for item in roster.scan(ConsistentRead=True)["Items"] if item["alias"] == "otter"]
    # What holds it names no student, and the table's expiry removes it once the telemetry is gone.
    assert held == {"github_id": admin_roster.held_alias_id("otter"), "alias": "otter", "expires_at": held["expires_at"]}
    assert held["github_id"] < 0 and held["expires_at"] == NOW + privacy.ALIAS_HELD_DAYS * 86_400
    assert [entry.alias for entry in admin_roster.entries(roster=roster)] == ["heron"]

    roster.delete_item(Key={"github_id": held["github_id"]})
    assert admin_roster.add(6060, "otter", roster=roster, course_end=COURSE_END, now=NOW).alias == "otter"


def test_the_student_is_not_put_back_on_the_roster_while_their_erase_runs(deployed: Deployed) -> None:
    refused: list[str] = []

    def reenroll() -> None:
        try:
            admin_roster.add(OTTER, "kestrel", roster=deployed.admin.roster, course_end=COURSE_END, now=NOW)
        except admin_roster.AdminError as error:
            refused.append(str(error))

    deployed.during_sleep.append(reenroll)

    status, printed = deployed.run("erase", str(OTTER))

    assert status == 0, printed
    assert refused == ["that GitHub ID is being erased: wait for the erase, or run it again if it stopped"]
    assert deployed.rows(OTTER) == []
    # Once the erase is over, the student may be added again, under another alias.
    again = admin_roster.add(OTTER, "kestrel", roster=deployed.admin.roster, course_end=COURSE_END, now=NOW)
    assert again.alias == "kestrel"


def test_a_key_issue_that_read_the_entry_before_an_erase_leaves_no_key(deployed: Deployed) -> None:
    erased: list[int] = []

    def erase_first(**_: Any) -> None:
        # The erase runs to its end between the command's read of the entry and its write of the
        # key, and the student is put back on the roster under another alias. The erase's own
        # transactions pass here too: it runs once.
        if not erased:
            erased.append(-1)
            erased[0] = deployed.run("erase", str(OTTER))[0]
            admin_roster.add(OTTER, "kestrel", roster=deployed.admin.roster, course_end=COURSE_END, now=NOW)

    deployed.admin.keys.meta.client.meta.events.register("before-call.dynamodb.TransactWriteItems", erase_first)
    out = io.StringIO()
    admin = dataclasses.replace(deployed.admin, course_end=lambda: COURSE_END)

    status = main.run(["key", "issue", str(OTTER)], admin, now=NOW, out=out, err=out)

    assert (erased, status) == ([0], 1)
    assert rules.every_key(OTTER, deployed.admin.keys) == []


def test_an_erase_taken_over_by_another_writes_nothing_more(deployed: Deployed) -> None:
    issue(deployed, OTTER)
    use(deployed, OTTER, "otter", "2027-01-15")
    later: list[str] = []

    def erase_again_then_reenroll() -> None:
        # A second erase of the student runs to its end while the first one waits, and the
        # student is put back on the roster, with a key and a usage row of the new entry.
        assert deployed.run("erase", str(OTTER))[0] == 0
        admin_roster.add(OTTER, "kestrel", roster=deployed.admin.roster, course_end=COURSE_END, now=NOW)
        later.append(issue(deployed, OTTER))
        use(deployed, OTTER, "kestrel", "2027-01-16")

    deployed.during_sleep.append(erase_again_then_reenroll)

    status, printed = deployed.run("erase", str(OTTER))

    assert status == 1 and admin_roster.ERASE_TAKEN_OVER in printed
    record = DynamoKeyStore(deployed.admin.keys).lookup(key_hash(later[0]))
    assert record is not None and not record.revoked and record.alias == "kestrel"
    assert [row["day"] for row in deployed.usage.scan()["Items"] if row["github_id"] == OTTER] == ["2027-01-16"]


def test_an_erase_refuses_what_is_no_github_id_before_it_writes_anything(deployed: Deployed) -> None:
    before = deployed.admin.roster.scan(ConsistentRead=True)["Items"]

    # The one number whose erase would be held under heron's own entry.
    status, printed = deployed.run("erase", str(-(2**60) - HERON))

    assert status == 1 and "GitHub ID" in printed
    assert deployed.admin.roster.scan(ConsistentRead=True)["Items"] == before


def test_an_erase_run_again_removes_a_login_s_lock_left_alone(deployed: Deployed) -> None:
    deployed.admin.roster.delete_item(Key={"github_id": OTTER})
    deployed.admin.keys.put_item(Item={**rules.lock_key(OTTER), "locked_at": NOW, "holder": "a-login"})

    status, printed = deployed.run("erase", str(OTTER))

    assert status == 0, printed
    assert deployed.rows(OTTER) == []


def test_the_usage_table_and_the_workspace_are_found_by_their_pinned_logical_ids() -> None:
    pinned = json.loads((REPO / "tests/policy/stateful_logical_ids.json").read_text())
    assert main.USAGE_LOGICAL_ID in pinned[main.STACK_NAME]
    assert main.WORKSPACE_LOGICAL_ID in pinned[main.OBSERVABILITY_STACK_NAME]
    assert privacy.RETENTION_DAYS == admin_roster.RETENTION_DAYS
