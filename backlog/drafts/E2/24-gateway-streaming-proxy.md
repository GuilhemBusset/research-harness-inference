+++
id = "E2-24"
title = "Gateway: forward a validated request and stream the answer, with every timeout set"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-14", "E2-15", "E2-17", "E2-18", "E2-19", "E2-21", "E2-22", "E2-23"]
+++

## Context

This is where the pieces of a warm request meet (section 3, warm path): authenticate, reserve,
validate and rewrite, forward to the pool, stream the answer back without buffering while reading
its usage, and settle. Section 6.2, step 7 bounds time: timeouts are set explicitly on every hop,
because the HTTP client's defaults would cut a quiet stream after 5 seconds. The HTTP client is
httpx (section 6.2), pinned by issue E1-16.

The upstream URL comes from the route table, never from the client's path (section 6.2, step 1).
Each pool is reached through the internal load balancer, which routes to the pool's target group
(issue E2-44). One listener serves every pool, and it chooses the pool by a header named
X-Rhi-Pool whose value is the pool's name, which the proxy sets on every request it forwards and
never takes from the client. The proxy is given the load balancer's address and each pool's bearer
secret; issue E2-76 reads them from the gateway's environment, and the deployment of issue E2-30
fills that environment in.

**The timeouts.** The design names none but the 30-minute limit. These are chosen here: 10 seconds
to connect, 60 seconds to write a request, 10 seconds to obtain a pooled connection, and 1,200
seconds to read, which is the idle timeout both load balancers run (section 5.3), so that only the
request's time limit (issue E2-77) ends a quiet stream. An idle upstream connection is closed after 5
seconds: the gateway closes idle upstream connections promptly, which is one of the guards that let
a pool's instance leave service quickly (section 5.1).

This issue is the path of a request that ends well. Every other end, the settlement of each, and the
causes `engine_error` and `time_limit` are issue E2-77.

## Deliverables

- [ ] `services/gateway/rhi_gateway/proxy.py` forwards the rewritten body to the upstream address it is given, never one taken from the request, with the pool's bearer secret and never the student's key, with the header X-Rhi-Pool set to the pool's name and any such header the client sent dropped, and streams the answer to the client chunk by chunk as it arrives.
- [ ] The upstream client's timeouts are set explicitly: 10 seconds to connect, 60 seconds to write, 1,200 seconds to read, 10 seconds to obtain a pooled connection, and an idle upstream connection is closed after 5 seconds.
- [ ] While it streams, it feeds the usage parser of the request's wire format (issues E2-21, E2-22 and E2-23) and gives its caller the tokens observed and whether the stream ended cleanly.
- [ ] `services/gateway/rhi_gateway/app.py` fills the four forwarded routes with this path, authentication, reservation, body filter, proxy, and settles a clean end with the reported usage.
- [ ] `tests/unit/gateway/test_proxy.py` covers the path against an upstream built in the test.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_proxy.py` passes: a stream reaches the client byte for byte in each wire format, its usage is settled, and the upstream never sees the student's key.
- [ ] `tests/unit/gateway/test_proxy.py::test_the_timeouts_are_set` finds the upstream client's connect, write, read and pool timeouts at 10, 60, 1,200 and 10 seconds and its idle-connection expiry at 5 seconds, none left to the library's default.
- [ ] `tests/unit/gateway/test_proxy.py::test_the_upstream_is_the_one_given` sends requests whose paths, headers and bodies name another host, and finds every upstream call addressed to the given upstream, with X-Rhi-Pool naming the pool the body filter chose even when the client sent that header with another value.

## References

- `docs/design/system-design.md`, sections 3, 5.1, 5.3, 6.2, 17
