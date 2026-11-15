"""The configuration in SSM (src/rhi/config.py): the ssm source, the writer to SSM and the writer
to a TOML file, against a stand-in for the SSM client. No test here reaches AWS."""

from __future__ import annotations

import dataclasses
import datetime
import subprocess
import sys
import tomllib
import traceback
from typing import TYPE_CHECKING, Any

import boto3
import botocore.exceptions
import pytest

from rhi import config

if TYPE_CHECKING:
    from pathlib import Path

EXAMPLE = config.load({"RHI_CONFIG": "example"})

# Part of every bad value below, so a test can tell that an error does not repeat the value.
MARK = "7701"


class StandIn:
    """An SSM client, as far as the module uses one: parameters by name, each with its type and
    its value, a read by path that pages as SSM does, a put, and a record of every call."""

    def __init__(self, parameters: dict[str, str] | None = None) -> None:
        self.parameters = {name: ("String", value) for name, value in (parameters or {}).items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_parameters_by_path(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("get_parameters_by_path", request))
        path = request["Path"].rstrip("/") + "/"
        below = sorted(name for name in self.parameters if name.startswith(path))
        if not request.get("Recursive"):
            below = [name for name in below if "/" not in name.removeprefix(path)]
        start = int(request.get("NextToken", "0"))
        found = [dict(zip(("Type", "Value"), self.parameters[name], strict=True), Name=name) for name in below]
        page: dict[str, Any] = {"Parameters": found[start : start + 10]}  # SSM gives ten at most
        if start + 10 < len(found):
            page["NextToken"] = str(start + 10)
        return page

    def put_parameter(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("put_parameter", request))
        if request["Name"] in self.parameters and not request.get("Overwrite"):
            raise sdk_error("ParameterAlreadyExists", "PutParameter")
        if len(request["Value"]) > 4096:
            raise sdk_error(f"ValidationException: {request['Value']}", "PutParameter")
        self.parameters[request["Name"]] = (request["Type"], request["Value"])
        return {"Version": 1}


def sdk_error(text: str, operation: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError({"Error": {"Code": text, "Message": text}}, operation)


def example_parameters() -> dict[str, str]:
    """The example's values as the owner types them into SSM, one parameter per key."""
    values = tomllib.loads((config.REPO_ROOT / config.EXAMPLE_FILE).read_text(encoding="utf-8"))
    assert values.pop("pools") == []  # an empty table: no parameter
    assert values.pop("session_windows") == []
    typed = {f"/rhi/config/{key}": str(value) for key, value in values.items()}
    return typed | {"/rhi/config/session_windows": "[]"}


def as_logged(error: BaseException) -> str:
    """The error as a build log shows it: its message, and those of the errors it is chained to."""
    return "".join(traceback.format_exception(error))


@pytest.fixture(autouse=True)
def _no_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_arguments: object, **_options: object) -> None:
        raise AssertionError("a test asked the SDK for a real client")

    monkeypatch.setattr(boto3, "client", refuse)


def test_the_ssm_source_gives_the_same_typed_object_as_the_example(monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in = StandIn(example_parameters())
    made: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(boto3, "client", lambda *arguments, **options: made.append((arguments, options)) or stand_in)

    from_ssm = config.load({"RHI_CONFIG": "ssm"})

    assert from_ssm == EXAMPLE
    for field in dataclasses.fields(config.Config):
        assert type(getattr(from_ssm, field.name)) is type(getattr(EXAMPLE, field.name)), field.name
    # The client is the caller's own: the module names no region, no account and no credential.
    assert made == [(("ssm",), {})]
    assert config.read_ssm(stand_in) == EXAMPLE
    assert made == [(("ssm",), {})]  # a client the caller gives is the one used


def test_the_layout() -> None:
    assert config.SSM_PREFIX == "/rhi/config/"

    written = StandIn()
    config.write_ssm(EXAMPLE, written)
    assert set(written.parameters) == {f"/rhi/config/{key}" for key in config.KEYS if key != "pools"}
    assert {kind for kind, _ in written.parameters.values()} == {"String"}
    assert not [name for name in written.parameters if name.startswith("/rhi/config/pools/")]
    assert all(request["Name"].startswith("/rhi/config/") for _, request in written.calls)
    assert {name: value for name, (_, value) in written.parameters.items()} == example_parameters()

    # A parameter beside the prefix is never met: only the prefix is read, by path, to its last page.
    beside = StandIn(example_parameters() | {"/rhi/pools/mock/release": f"release{MARK}"})
    assert config.read_ssm(beside) == EXAMPLE
    assert len(beside.calls) > 1
    assert {operation for operation, _ in beside.calls} == {"get_parameters_by_path"}
    assert {request["Path"] for _, request in beside.calls} == {"/rhi/config/"}


def test_a_written_configuration_reads_back_as_the_same_object(tmp_path: Path) -> None:
    other = dataclasses.replace(
        EXAMPLE,
        github_owner='an "owner" \\ of\ttwo\nlines, \x7f and é',
        monthly_budget_usd=17,
        time_zone="Europe/Paris",
        course_end=datetime.date(2031, 2, 3),
    )
    stand_in = StandIn()
    for written in (EXAMPLE, other):  # the second writing goes over the first
        config.write_ssm(written, stand_in)
        assert config.read_ssm(stand_in) == written

        config.write_file(written, tmp_path / "written.toml")
        from_file = config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(tmp_path / "written.toml")})
        assert from_file == written


@pytest.mark.parametrize(
    ("named", "changes"),
    [
        # A missing parameter.
        ("'domain'", {"/rhi/config/domain": None}),
        ("'session_windows'", {"/rhi/config/session_windows": None}),
        # A parameter under the prefix that names no key.
        ("'/rhi/config/surprise'", {"/rhi/config/surprise": f"value{MARK}"}),
        ("'/rhi/config/domain/more'", {"/rhi/config/domain/more": f"value{MARK}"}),
        ("'/rhi/config/pools'", {"/rhi/config/pools": "[]"}),
        ("'/rhi/config/pools/mock/more'", {"/rhi/config/pools/mock/more": f"value{MARK}"}),
        # A value of the wrong type.
        ("'monthly_budget_usd'", {"/rhi/config/monthly_budget_usd": f"{MARK}.5"}),
        ("'monthly_budget_usd'", {"/rhi/config/monthly_budget_usd": f"-{MARK}"}),
        ("'monthly_budget_usd'", {"/rhi/config/monthly_budget_usd": f"{MARK} "}),
        ("'monthly_budget_usd'", {"/rhi/config/monthly_budget_usd": MARK * 2000}),
        ("'course_end'", {"/rhi/config/course_end": f"{MARK}-02-30"}),
        ("'course_end'", {"/rhi/config/course_end": f"{MARK}0101"}),
        ("'time_zone'", {"/rhi/config/time_zone": f"Nowhere/City{MARK}"}),
        ("'session_windows'", {"/rhi/config/session_windows": f'["{MARK}"]'}),
        # A pool entry is its own parameter, and the loader refuses one until entries have a shape.
        ("'pools'", {"/rhi/config/pools/mock": f"entry{MARK}"}),
        # A parameter that is not a String.
        ("'/rhi/config/domain'", {"/rhi/config/domain": ("SecureString", f"cipher{MARK}")}),
    ],
)
def test_a_missing_an_unknown_or_a_wrongly_typed_parameter_raises(
    named: str, changes: dict[str, str | tuple[str, str] | None]
) -> None:
    stand_in = StandIn(example_parameters())
    for name, change in changes.items():
        if change is None:
            del stand_in.parameters[name]
        else:
            stand_in.parameters[name] = ("String", change) if isinstance(change, str) else change

    with pytest.raises(config.ConfigError) as raised:
        config.read_ssm(stand_in)
    assert named in str(raised.value)
    assert "'ssm'" in str(raised.value)
    assert MARK not in as_logged(raised.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("domain", f"{MARK}\ud800"),  # not text: neither a file nor a parameter can hold it
        ("monthly_budget_usd", f"{MARK}"),
        ("monthly_budget_usd", -int(MARK)),
        ("monthly_budget_usd", True),
        ("course_end", datetime.datetime(int(MARK), 1, 1, tzinfo=datetime.UTC)),
        ("time_zone", f"Nowhere/City{MARK}"),
        ("pools", (f"entry{MARK}",)),
        ("session_windows", None),
    ],
)
def test_a_writer_refuses_what_its_source_would_not_load(tmp_path: Path, key: str, value: object) -> None:
    refused = dataclasses.replace(EXAMPLE, **{key: value})
    stand_in = StandIn()
    for write in (lambda: config.write_ssm(refused, stand_in), lambda: config.write_file(refused, tmp_path / "written")):
        with pytest.raises(config.ConfigError) as raised:
            write()
        assert repr(key) in str(raised.value)
        assert MARK not in as_logged(raised.value)
    # Nothing was written, not even the keys before the refused one.
    assert stand_in.calls == []
    assert not (tmp_path / "written").exists()


def test_an_error_of_the_sdk_or_of_the_system_names_no_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def logged(call: Any) -> str:
        with pytest.raises(config.ConfigError) as raised:
            call()
        assert MARK not in as_logged(raised.value)
        return str(raised.value)

    # A read SSM refuses: its message can hold the role and the account.
    stand_in = StandIn(example_parameters())
    monkeypatch.setattr(stand_in, "get_parameters_by_path", lambda **_: (_ for _ in ()).throw(sdk_error(MARK, "Read")))
    assert "'ssm' cannot be read: the parameters under /rhi/config/" in logged(lambda: config.read_ssm(stand_in))

    # A put SSM refuses: the error names the parameter, and the writing stops there.
    long = dataclasses.replace(EXAMPLE, github_owner=MARK * 2000)
    stand_in = StandIn()
    assert "'/rhi/config/github_owner'" in logged(lambda: config.write_ssm(long, stand_in))
    assert "/rhi/config/github_owner" not in stand_in.parameters

    # No client: the SDK finds no region.
    monkeypatch.setattr(boto3, "client", lambda *_: (_ for _ in ()).throw(botocore.exceptions.NoRegionError()))
    for call in (lambda: config.load({"RHI_CONFIG": "ssm"}), lambda: config.write_ssm(EXAMPLE)):
        assert "SSM client" in logged(call)

    # A file that cannot be written: the error does not repeat its path.
    logged(lambda: config.write_file(EXAMPLE, tmp_path / f"folder{MARK}" / "written.toml"))


def test_the_sdk_is_pinned_and_only_the_ssm_source_and_writer_load_it() -> None:
    project = tomllib.loads((config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((config.REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    pins = [requirement for requirement in project["dependencies"] if requirement.startswith("boto3")]
    locked = [package["version"] for package in lock["package"] if package["name"] == "boto3"]
    assert len(pins) == 1
    assert len(locked) == 1  # one lock holds one version
    assert pins[0] == f"boto3=={locked[0]}"

    # The loader and the file writer run without the SDK: only the ssm source and its writer import it.
    program = (
        "import pathlib, sys; from rhi import config;"
        "config.write_file(config.load({'RHI_CONFIG': 'example'}), pathlib.Path(sys.argv[1]));"
        "sys.exit('boto3' in sys.modules or 'botocore' in sys.modules)"
    )
    environment = {"PYTHONPATH": str(config.REPO_ROOT / "src")}
    assert subprocess.run([sys.executable, "-c", program, "/dev/null"], env=environment, check=False).returncode == 0
