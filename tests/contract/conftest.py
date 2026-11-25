"""The engine the contract suite runs against (system design, section 4.1).

The suite of `test_engine_contract.py` is the same for every engine adapter. The environment says
which engine it is run against:

| Variable | Holds | When unset |
| --- | --- | --- |
| `RHI_ENGINE_URL` | The address of a running engine, such as `http://127.0.0.1:8000` on its node | The suite runs against the mock engine, in this process |
| `RHI_ENGINE_NAME` | The engine's row of the per-engine table of metric names (`rhi_common.engine_metrics`) | With an address: the run is refused, by the variable's name |
| `RHI_POOL_SECRET` | The pool's bearer secret, under the name the engine reads it from | With an address: the run is refused, by the variable's name |

**In process**, the engine is the mock (`rhi_mock_engine.app`), called as an ASGI application with
a secret drawn when the tests run and a sleep that does not wait. Nothing listens and nothing
leaves the process. The suite controls this engine, so two things are checked here only:

- a request can come from an address that is not a loopback one, which the metrics row needs;
- the shutdown row needs a process to signal. The fixture `engine_process` starts the mock as
  `main` serves it, through `serve`, on a Unix socket in a temporary folder, at a rate slow
  enough for a stream to be in flight when the signal comes.

**At an address**, the suite is one more HTTP client of an engine it did not start. It cannot
choose the address its requests come from, and it cannot signal the engine: every test that takes
`engine_process` is left out of the collection, so such a run skips nothing. An address with no
engine name, with a name the table has no row for, or with no secret is a usage error. The suite
never falls back to the mock when an engine was to be checked.

The fixtures `mock_engine`, `engine_at_an_address` and `read_engine_target` are for the tests
about the suite itself, which always use the mock, whatever the environment names.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn

from rhi_common.engine_metrics import ENGINES, MOCK
from rhi_mock_engine.app import SECRET_VARIABLE, Settings, create_app

CONTRACT = Path(__file__).resolve().parent
REPO = CONTRACT.parents[1]

URL_VARIABLE = "RHI_ENGINE_URL"
NAME_VARIABLE = "RHI_ENGINE_NAME"

LOOPBACK = "127.0.0.1"
# How long the client waits for the next bytes of an answer: a model may think before it writes.
READ_SECONDS = 120.0
# The rate of the process `engine_process` starts: about a second of stream for the canned turn.
PROCESS_TOKENS_PER_SECOND = 20

# The child serves the mock as `main` does, through `serve`, on the Unix socket it is given.
_CHILD = "import os, sys; from rhi_mock_engine.app import Settings, serve; serve(Settings.from_env(os.environ), uds=sys.argv[1])"


@dataclass(frozen=True)
class Engine:
    """An engine under test, and how a test reaches it."""

    # The engine's row of the per-engine table of metric names.
    name: str
    secret: str
    url: str
    # A transport whose requests come from the address it is given, where the suite can choose it.
    connect: Callable[[str], httpx.AsyncBaseTransport]
    # Whether the suite chooses the address its requests come from.
    in_process: bool

    @property
    def authorized(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"}

    def client(self, peer: str = LOOPBACK) -> httpx.AsyncClient:
        """A client of the engine. It sends no secret by itself."""
        return httpx.AsyncClient(transport=self.connect(peer), base_url=self.url, timeout=READ_SECONDS)


@dataclass(frozen=True)
class EngineProcess:
    """An engine the suite started as a process of its own, to signal it."""

    engine: Engine
    child: subprocess.Popen[bytes]


class TargetError(ValueError):
    """The environment does not say which engine to test. The message names a variable, never a value."""


@dataclass(frozen=True)
class Target:
    """The engine at an address that the environment names."""

    url: str
    name: str
    secret: str


def read_target(environ: Mapping[str, str]) -> Target | None:
    """The engine at an address the environment names, or `None` for the mock in process."""
    url = environ.get(URL_VARIABLE)
    if not url:
        return None
    name = environ.get(NAME_VARIABLE)
    if not name:
        raise TargetError(f"{NAME_VARIABLE} is unset or empty: {URL_VARIABLE} names an engine, which needs its row")
    if name not in ENGINES:
        raise TargetError(f"{NAME_VARIABLE} names no row of the per-engine table: expected one of {sorted(ENGINES)}")
    secret = environ.get(SECRET_VARIABLE)
    if not secret:
        raise TargetError(
            f"{SECRET_VARIABLE} is unset or empty: {URL_VARIABLE} names an engine, which needs its secret"
        )
    return Target(url=url.rstrip("/"), name=name, secret=secret)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Leave out the tests that signal the engine when the engine is one the suite did not start."""
    try:
        in_process = read_target(os.environ) is None
    except TargetError as error:
        raise pytest.UsageError(str(error)) from None
    if in_process:
        return
    left_out = [
        item
        for item in items
        if CONTRACT in item.path.parents and "engine_process" in getattr(item, "fixturenames", ())
    ]
    if left_out:
        config.hook.pytest_deselected(items=left_out)
        items[:] = [item for item in items if item not in left_out]


async def _no_sleep(seconds: float) -> None:
    del seconds


def _in_process_mock() -> Engine:
    secret = secrets.token_urlsafe(32)
    app = create_app(Settings(secret=secret), sleep=_no_sleep)
    return Engine(
        name=MOCK,
        secret=secret,
        url="http://engine",
        connect=lambda peer: httpx.ASGITransport(app=app, client=(peer, 40000)),
        in_process=True,
    )


@pytest.fixture
def mock_engine() -> Engine:
    """The mock engine, in this process."""
    return _in_process_mock()


@pytest.fixture
def engine() -> Engine:
    """The engine under test: the one the environment names, or the mock in this process."""
    target = read_target(os.environ)
    if target is None:
        return _in_process_mock()
    return Engine(
        name=target.name,
        secret=target.secret,
        url=target.url,
        connect=lambda peer: httpx.AsyncHTTPTransport(),
        in_process=False,
    )


@pytest.fixture
def engine_process() -> Iterator[EngineProcess]:
    """The mock engine as a process, ready, serving slowly enough to be signalled in mid-stream."""
    secret = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="rhi-engine-") as folder:
        socket_path = str(Path(folder) / "engine.sock")
        log_path = Path(folder) / "engine.log"
        environ = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(REPO / "services/engine/mock"), str(REPO / "services/common")]),
            SECRET_VARIABLE: secret,
            "RHI_MOCK_FIRST_BYTE_SECONDS": "0",
            "RHI_MOCK_TOKENS_PER_SECOND": str(PROCESS_TOKENS_PER_SECOND),
            "RHI_MOCK_HANG": "0",
        }
        with log_path.open("wb") as log:
            command = [sys.executable, "-c", _CHILD, socket_path]
            child = subprocess.Popen(command, env=environ, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(transport=httpx.HTTPTransport(uds=socket_path), base_url="http://engine") as client:
                deadline = time.monotonic() + 30
                while not (Path(socket_path).exists() and client.get("/health").status_code == 200):
                    assert child.poll() is None, log_path.read_text(encoding="utf-8", errors="replace")
                    assert time.monotonic() < deadline, "the mock engine did not start"
                    time.sleep(0.05)
            yield EngineProcess(
                engine=Engine(
                    name=MOCK,
                    secret=secret,
                    url="http://engine",
                    connect=lambda peer: httpx.AsyncHTTPTransport(uds=socket_path),
                    in_process=False,
                ),
                child=child,
            )
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


@pytest.fixture
def engine_at_an_address() -> Iterator[dict[str, str]]:
    """The mock engine served on a loopback port the system picks: the environment that names it."""
    secret = secrets.token_urlsafe(32)
    app = create_app(Settings(secret=secret), sleep=_no_sleep)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
    with socket.socket() as listening:
        listening.bind((LOOPBACK, 0))
        url = f"http://{LOOPBACK}:{listening.getsockname()[1]}"
        serving = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, name="contract-engine")
        serving.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                assert serving.is_alive() and time.monotonic() < deadline, "the mock engine did not start"
                time.sleep(0.01)
            yield {URL_VARIABLE: url, NAME_VARIABLE: MOCK, SECRET_VARIABLE: secret}
        finally:
            server.should_exit = True
            serving.join(timeout=30)


@pytest.fixture
def read_engine_target() -> Callable[[Mapping[str, str]], Target | None]:
    """`read_target`, for the test of what the environment asks for."""
    return read_target
