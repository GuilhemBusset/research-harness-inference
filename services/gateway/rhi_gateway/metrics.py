"""The gateway's metrics per student, served to the collector beside it (system design, sections 6.6, 7.1 and 7.2).

The gateway serves its metrics on `127.0.0.1`, port 9091, route `/metrics`, in Prometheus' text
format, and on no other address: the collector of the same task scrapes them there
(`services/gateway/collector/config.yaml`) and pushes them to the Prometheus workspace, so nothing
outside the task reaches the endpoint. The design names no port: 9091 is the one that
configuration scrapes. Any other route answers 404.

| Series | Kind | Labels | Value |
| --- | --- | --- | --- |
| `rhi_gateway_student_requests_total` | counter | `alias`, `pool` | The requests the gateway answered |
| `rhi_gateway_student_input_tokens_total` | counter | `alias`, `pool` | The input tokens their settlements observed |
| `rhi_gateway_student_output_tokens_total` | counter | `alias`, `pool` | The output tokens their settlements observed |
| `rhi_gateway_student_errors_total` | counter | `alias`, `pool`, `cause` | The requests the gateway gave a cause |
| `rhi_gateway_student_budget_left_tokens` | gauge | `alias` | The tokens the student has left of the day |

**One request, one count.** `Metrics.count` is given the log record of each request when it ends
(`rhi_gateway.telemetry`), so the counters and the log agree: every request the gateway answers
is counted once, a local route, a refusal and a path outside the table included. The tokens are
those the request's settlement observed. A request counts as an error when its record names a
cause: one of `rhi_common.causes`, or `none` for the gateway's own fault, which is what the error
rate reads (section 7.3). An answer that states a cause is counted under it whatever its status,
so the error event that ends a stream answered 200 counts too.

**The student is the alias** (section 6.6). `alias` is the roster alias of the accepted key, and
no series carries another name of a student: no GitHub ID, no key, no hash of one. A request
counted before its key is accepted (a refused key, the hello, a path outside the table) has an
empty `alias`, and one that names no pool an empty `pool`; Prometheus reads an empty label as no
label, so these series are of no student. Every value of a label is the gateway's own: an alias
from the keys table, a configured pool's name, a cause of the fixed list. Nothing a client sends
becomes a label.

**The budget left** is the ledger's read (`rhi_gateway.budget.Ledger.tokens_left`), made when the
endpoint is scraped, for each student whose key this task has accepted since it started: the value
is then right after midnight, and after a request another task served, with no request here. A
read that fails gives no sample for that student at that scrape, never an old value, and the
counters are served still.

Nothing here reaches CloudWatch: its metrics cannot be deleted for 15 months, so the gateway
publishes none (section 6.6), and these series exist in Prometheus only.

The counters count from the task's start. The collector labels every series with the task's ID,
so the series of two tasks never merge, and a sum over them is right across the gateway's tasks.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import CollectorRegistry, Counter, make_wsgi_app
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.exposition import ThreadingWSGIServer

if TYPE_CHECKING:
    from prometheus_client import Metric

    from rhi_gateway.budget import Ledger, Student
    from rhi_gateway.telemetry import RequestRecord

HOST = "127.0.0.1"
PORT = 9091
ROUTE = "/metrics"

# The families' names; a counter's samples end in `_total`.
REQUESTS = "rhi_gateway_student_requests"
INPUT_TOKENS = "rhi_gateway_student_input_tokens"
OUTPUT_TOKENS = "rhi_gateway_student_output_tokens"
ERRORS = "rhi_gateway_student_errors"
BUDGET_LEFT = "rhi_gateway_student_budget_left_tokens"

_StartResponse = Callable[[str, list[tuple[str, str]]], object]
_Application = Callable[[dict[str, Any], _StartResponse], Iterable[bytes]]


class _BudgetLeft:
    """The budget each student has left of the day, read from the ledger at every scrape."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._lock = threading.Lock()
        # By alias, the label: the record of the key last accepted for it.
        self._students: dict[str, Student] = {}

    def seen(self, student: Student) -> None:
        with self._lock:
            self._students[student.alias] = student

    def describe(self) -> Iterable[Metric]:
        # The registry asks the family's name when the collector is registered: no read for that.
        return [self._family()]

    def collect(self) -> Iterable[Metric]:
        family = self._family()
        with self._lock:
            students = dict(self._students)
        for alias, student in students.items():
            try:
                left = self._ledger.tokens_left(student)
            except Exception:  # noqa: BLE001, S112 - a read that fails gives no sample; the scrape and the counters go on.
                continue
            family.add_metric([alias], left)
        return [family]

    @staticmethod
    def _family() -> GaugeMetricFamily:
        return GaugeMetricFamily(
            BUDGET_LEFT, "Tokens of today's budget neither charged nor reserved.", labels=["alias"]
        )


class Metrics:
    """The gateway's registry and what it counts of each request.

    `ledger` is the one the requests reserve in and settle with: without it there is no budget
    series. The registry is the gateway's own, not prometheus-client's default one, which also
    describes the process.
    """

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.registry = CollectorRegistry()
        self._requests = Counter(REQUESTS, "Requests answered.", ["alias", "pool"], registry=self.registry)
        self._input_tokens = Counter(
            INPUT_TOKENS, "Input tokens settled, cached or not.", ["alias", "pool"], registry=self.registry
        )
        self._output_tokens = Counter(
            OUTPUT_TOKENS, "Output tokens settled.", ["alias", "pool"], registry=self.registry
        )
        self._errors = Counter(
            ERRORS,
            "Requests given a cause; none is the gateway's own fault.",
            ["alias", "pool", "cause"],
            registry=self.registry,
        )
        self._budget_left = None if ledger is None else _BudgetLeft(ledger)
        if self._budget_left is not None:
            self.registry.register(self._budget_left)

    def count(self, record: RequestRecord) -> None:
        """Count one request that has ended, from its log record. Reads nothing and never waits."""
        alias, pool = record.alias or "", record.pool or ""
        self._requests.labels(alias, pool).inc()
        if record.input_tokens:
            self._input_tokens.labels(alias, pool).inc(record.input_tokens)
        if record.output_tokens:
            self._output_tokens.labels(alias, pool).inc(record.output_tokens)
        if record.cause is not None:
            self._errors.labels(alias, pool, record.cause).inc()
        if record.student is not None and self._budget_left is not None:
            self._budget_left.seen(record.student)

    def serve(self, *, host: str = HOST, port: int = PORT) -> WSGIServer:
        """Start serving the endpoint, in a thread that ends with the gateway, and return its server.

        `shutdown` then `server_close` stop it. `host` and `port` are arguments for the tests,
        which take a free port: the gateway gives neither.
        """
        server = make_server(host, port, application(self.registry), ThreadingWSGIServer, handler_class=_Quiet)
        threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
        return server


def application(registry: CollectorRegistry) -> _Application:
    """The endpoint as a WSGI application: `registry`'s metrics at `ROUTE`, and nothing elsewhere."""
    exposition = make_wsgi_app(registry)

    def answer(environ: dict[str, Any], start_response: _StartResponse) -> Iterable[bytes]:
        # prometheus-client's own application answers every path with the metrics.
        if environ.get("PATH_INFO") != ROUTE:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Not Found\n"]
        return exposition(environ, start_response)

    return answer


class _Quiet(WSGIRequestHandler):
    """A scrape every few seconds is not worth a log line each, and standard output is the request log's."""

    def log_message(self, format: str, *args: object) -> None:
        pass
