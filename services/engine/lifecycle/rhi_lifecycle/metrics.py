"""The lifecycle agent's metrics: one registry, served on one endpoint of the instance itself.

The agent serves its metrics on `127.0.0.1`, port 9110, route `/metrics`, in Prometheus' text
format. The node's collector scrapes them there (system design, section 4.3), so nothing outside
the instance reaches the endpoint. The design names no port: 9110 is free of the engine's 8000 and
of the usual exporters' 9100 and 9400.

Every module of the agent that has a metric registers it in `REGISTRY`, and `serve` serves what the
registry holds at each scrape. Any other route answers 404.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import CollectorRegistry, make_wsgi_app
from prometheus_client.exposition import ThreadingWSGIServer

HOST = "127.0.0.1"
PORT = 9110
ROUTE = "/metrics"

# The agent's registry. Not prometheus-client's default one, which also describes the process.
REGISTRY = CollectorRegistry()

_StartResponse = Callable[[str, list[tuple[str, str]]], object]
_Application = Callable[[dict[str, Any], _StartResponse], Iterable[bytes]]


def application(registry: CollectorRegistry = REGISTRY) -> _Application:
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
    """A scrape every few seconds is not worth a log line each."""

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve(registry: CollectorRegistry = REGISTRY, *, host: str = HOST, port: int = PORT) -> WSGIServer:
    """Start serving the endpoint, in a thread that ends with the agent, and return its server.
    `shutdown` then `server_close` stop it. `host` and `port` are arguments for the tests, which
    take a free port: the agent gives neither."""
    server = make_server(host, port, application(registry), ThreadingWSGIServer, handler_class=_Quiet)
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server
