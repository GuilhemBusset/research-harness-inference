"""The ID of every smoke test (system design, section 8.4).

A smoke test is known by its ID, not by its function's name: the reporter's failure report names a
failed test by it, and only an ID of this list passes the report's filter. The reporter's
allow-list is this list: the reporter's construct reads this file at synthesis and gives the list
to the function (`rhi.pipeline.reporter`).

A test carries its ID as the marker `smoke_id`, and `test_ids_are_unique_and_listed` holds the two
together: every smoke test has one ID, no two share one, and the IDs are exactly these. An ID is
lower-case letters, digits and hyphens, and it does not change when its test is renamed or moved.

This file is data: it imports nothing, so that whatever reads the list needs neither pytest nor
the tests.
"""

SMOKE_TEST_IDS: tuple[str, ...] = (
    # tests/smoke/test_smoke.py: what both stacks answer.
    "hello",
    "no-key",
    "forbidden-invocations",
    "forbidden-pause",
    "forbidden-tokenize",
    "forbidden-metrics",
    "forbidden-health",
    "first-stream-wake",
    "stream-messages",
    "stream-chat-completions",
    "stream-responses",
    # tests/smoke/deployed/: what only a deployed stack answers.
    "internal-invocations",
    "internal-pause",
    "internal-tokenize",
    "internal-metrics",
    "internal-health",
    "metrics-gateway",
    "metrics-pool",
    "release-served",
    "gpu-release-served",
    "release-heartbeat",
    # tests/smoke/test_smoke.py: the tests of the smoke tests themselves, which reach no stack.
    "ids-listed",
    "deployed-need-endpoint",
    "environment",
)
