"""What the evaluation's stream reader says of an attempt on each of the three routes
(docs/design/agent-session-evaluation.md, "Reading a result"): how it ended, when its output came,
the usage it reported and its speed.

Every stream here is made up, event by event, in the shape of its wire format: no capture, no host,
no key.
"""

from __future__ import annotations

import json

import pytest

from evaluate.contracts import RequestOutcome, RequestRecord, Route, Traffic, Usage
from evaluate.streams import Attempt, Attempts
from rhi_common.causes import Cause
from rhi_gateway.errors import WireFormat, render_stream_event

MESSAGES, RESPONSES, CHAT = Route.MESSAGES, Route.RESPONSES, Route.CHAT_COMPLETIONS
ROUTES = (MESSAGES, RESPONSES, CHAT)
routes = pytest.mark.parametrize("route", ROUTES)

ANSWER_TEXT = "made-up text of an answer"
ERROR_TEXT = "made-up text of an error"
MESSAGES_DELTA = "content_block_delta"


def event(data: object, name: str | None = None) -> bytes:
    """One event of a stream: its data, as JSON unless it is text already, under its name if it has one."""
    text = data if isinstance(data, str) else json.dumps(data)
    return (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()


def chat(delta: dict[str, object]) -> bytes:
    return event({"choices": [{"index": 0, "delta": delta}]})


def opening(route: Route, *, usage: bool = True) -> bytes:
    """The first event of a stream. On Messages it reports the input, unless `usage` is false."""
    if route is MESSAGES:
        reported = {"input_tokens": 15, "cache_read_input_tokens": 5, "output_tokens": 1}
        return event({"type": "message_start", "message": {"usage": reported} if usage else {}}, "message_start")
    if route is RESPONSES:
        return event({"type": "response.created", "response": {"usage": None}}, "response.created")
    return chat({"role": "assistant", "content": ""})


def text(route: Route, piece: str = ANSWER_TEXT) -> bytes:
    if route is MESSAGES:
        return event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": piece}}, MESSAGES_DELTA)
    if route is RESPONSES:
        return event({"type": "response.output_text.delta", "delta": piece}, "response.output_text.delta")
    return chat({"content": piece})


def reasoning(route: Route) -> bytes:
    if route is MESSAGES:
        delta = {"type": "thinking_delta", "thinking": "hm"}
        return event({"type": "content_block_delta", "delta": delta}, MESSAGES_DELTA)
    if route is RESPONSES:
        return event({"type": "response.reasoning_text.delta", "delta": "hm"}, "response.reasoning_text.delta")
    return chat({"reasoning_content": "hm"})


def tool_name(route: Route) -> bytes:
    """The event that opens a tool call: its name, and none of its arguments yet."""
    if route is MESSAGES:
        block = {"type": "tool_use", "name": "lookup", "input": {}}
        return event({"type": "content_block_start", "content_block": block}, "content_block_start")
    if route is RESPONSES:
        item = {"type": "function_call", "name": "lookup", "arguments": ""}
        return event({"type": "response.output_item.added", "item": item}, "response.output_item.added")
    return chat({"tool_calls": [{"index": 0, "function": {"name": "lookup", "arguments": ""}}]})


def tool_arguments(route: Route) -> bytes:
    if route is MESSAGES:
        delta = {"type": "input_json_delta", "partial_json": '{"q"'}
        return event({"type": "content_block_delta", "delta": delta}, MESSAGES_DELTA)
    if route is RESPONSES:
        name = "response.function_call_arguments.delta"
        return event({"type": name, "delta": '{"q"'}, name)
    return chat({"tool_calls": [{"index": 0, "function": {"arguments": '{"q"'}}]})


def usage_event(route: Route, output_tokens: int | None) -> bytes:
    """The event before the last that reports the usage. Responses has none: its last event does."""
    if route is MESSAGES:
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        reported = {} if output_tokens is None else {"usage": {"output_tokens": output_tokens}}
        return event({**delta, **reported}, "message_delta")
    if route is RESPONSES or output_tokens is None:
        return b""
    counts = {"prompt_tokens": 20, "prompt_tokens_details": {"cached_tokens": 5}, "completion_tokens": output_tokens}
    return event({"choices": [], "usage": counts})


def last_event(route: Route, output_tokens: int | None, kind: str = "response.completed") -> bytes:
    if route is MESSAGES:
        return event({"type": "message_stop"}, "message_stop")
    if route is CHAT:
        return event("[DONE]")
    counts = {"input_tokens": 20, "input_tokens_details": {"cached_tokens": 5}, "output_tokens": output_tokens}
    return event({"type": kind, "response": {"usage": None if output_tokens is None else counts}}, kind)


def ending(route: Route, output_tokens: int | None = 7) -> bytes:
    """What closes a stream that ended well: its usage, unless `output_tokens` is `None`, and its last event."""
    return usage_event(route, output_tokens) + last_event(route, output_tokens)


def error(route: Route) -> bytes:
    """The event that reports a failure inside a stream."""
    if route is MESSAGES:
        return event({"type": "error", "error": {"type": "overloaded_error", "message": ERROR_TEXT}}, "error")
    if route is RESPONSES:
        failed = {"status": "failed", "error": {"code": "server_error", "message": ERROR_TEXT}}
        return event({"type": "response.failed", "response": failed}, "response.failed")
    return event({"error": {"code": "server_error", "message": ERROR_TEXT}})


def replay(
    route: Route,
    *chunks: tuple[int, bytes],
    status: int | None = 200,
    sent: int = 0,
    retry: int = 0,
    broken: bool = False,
    into: Attempts | None = None,
) -> Attempt:
    """Reads an answer as a driver would: its status, unless none arrived, then each chunk at its
    time. The attempt ends 10 ms after its last chunk: the body is over, or, when `broken`, the
    transport failed."""
    reader = (into or Attempts()).send(route, sent, retry=retry)
    if status is not None:
        reader.answered(status)
    for at, chunk in chunks:
        reader.feed(chunk, at)
    return reader.end((chunks[-1][0] if chunks else sent) + 10, transport_error=broken)


def output_times(attempt: Attempt) -> tuple[int | None, int | None, int | None]:
    return attempt.first_output_ms, attempt.first_answer_ms, attempt.last_output_ms


# How an attempt ended.


@routes
def test_a_stream_read_to_its_last_event_is_completed_and_one_closed_before_it_is_truncated(route: Route) -> None:
    body = [(100, opening(route)), (200, text(route)), (300, text(route))]

    whole = replay(route, *body, (400, ending(route, 7)))

    assert (whole.outcome, whole.status, whole.failed) == (RequestOutcome.COMPLETED, 200, False)
    assert whole.usage == Usage(input_tokens=20, cached_input_tokens=5, output_tokens=7)
    assert (whole.sent_ms, *output_times(whole), whole.ended_ms) == (0, 200, 200, 300, 410)

    # Closed before its last event, and closed in the middle of it.
    for cut in (replay(route, *body), replay(route, *body, (400, ending(route, 7)[:-1]))):
        assert (cut.outcome, cut.status, cut.failed) == (RequestOutcome.TRUNCATED_STREAM, 200, True)
        assert (*output_times(cut), cut.output_tokens_per_second) == (200, 200, 300, None)


@routes
def test_an_error_event_inside_a_200_is_a_failure_whatever_follows_and_no_text_is_kept(route: Route) -> None:
    attempts = Attempts()

    attempt = replay(
        route, (100, opening(route)), (200, text(route)), (300, error(route)), (400, ending(route)), into=attempts
    )

    assert (attempt.outcome, attempt.status, attempt.failed) == (RequestOutcome.ERROR_EVENT, 200, True)
    assert output_times(attempt) == (200, 200, 200)
    kept = repr((attempt, attempt.measured(), attempts.ended, attempts.tally()))
    assert ANSWER_TEXT not in kept
    assert ERROR_TEXT not in kept


@routes
def test_an_error_event_after_the_last_event_is_a_failure(route: Route) -> None:
    # What the gateway writes when the stream it passed on, last event included, reported no usage.
    gateway_error = render_stream_event(Cause.ENGINE_ERROR, WireFormat(route.value))
    body = [(100, opening(route)), (200, text(route)), (300, ending(route))]
    attempts = Attempts()

    for late in (error(route), gateway_error, text(route) + gateway_error):
        attempt = replay(route, *body, (400, late), into=attempts)

        assert (attempt.outcome, attempt.status, attempt.failed) == (RequestOutcome.ERROR_EVENT, 200, True)
        # Nothing but the error is read after the last event.
        assert output_times(attempt) == (200, 200, 200)
    # Without an error, what follows the last event changes nothing.
    assert replay(route, *body, (400, text(route)), into=attempts).outcome is RequestOutcome.COMPLETED
    tally = attempts.tally()
    assert (tally.completed, tally.failed, tally.failures[RequestOutcome.ERROR_EVENT]) == (1, 3, 3)


@routes
def test_a_refusal_and_a_request_that_got_no_answer_are_told_by_the_status(route: Route) -> None:
    stream = opening(route) + text(route) + ending(route)

    refused = replay(route, (100, stream), status=429)
    lost = replay(route, status=None)

    assert (refused.outcome, refused.status) == (RequestOutcome.HTTP_REFUSAL, 429)
    assert (refused.first_output_ms, refused.usage) == (None, None)
    assert (lost.outcome, lost.status) == (RequestOutcome.TRANSPORT_ERROR, None)
    assert replay(route, status=None, broken=True).outcome is RequestOutcome.TRANSPORT_ERROR


@routes
def test_a_transport_that_fails_after_the_headers_is_a_transport_error_with_what_was_measured(route: Route) -> None:
    body = [(100, opening(route)), (200, text(route)), (300, text(route))]
    attempts = Attempts()

    broken = replay(route, *body, broken=True, into=attempts)
    closed = replay(route, *body, into=attempts)

    assert (broken.outcome, broken.status, broken.failed) == (RequestOutcome.TRANSPORT_ERROR, 200, True)
    assert (*output_times(broken), broken.ended_ms) == (200, 200, 300, 310)
    assert closed.outcome is RequestOutcome.TRUNCATED_STREAM
    failures = attempts.tally().failures
    assert (failures[RequestOutcome.TRANSPORT_ERROR], failures[RequestOutcome.TRUNCATED_STREAM]) == (1, 1)
    # A failure of the transport takes nothing from a stream that had ended, or from a refusal.
    assert replay(route, *body, (400, ending(route)), broken=True).outcome is RequestOutcome.COMPLETED
    assert replay(route, *body, (400, error(route)), broken=True).outcome is RequestOutcome.ERROR_EVENT
    assert replay(route, status=503, broken=True).outcome is RequestOutcome.HTTP_REFUSAL


@pytest.mark.parametrize(("route", "other"), [(route, other) for route in ROUTES for other in ROUTES if route != other])
def test_the_last_event_of_another_route_does_not_complete_a_stream(route: Route, other: Route) -> None:
    attempt = replay(route, (100, opening(route) + text(route)), (200, ending(other)))

    assert attempt.outcome is RequestOutcome.TRUNCATED_STREAM


@pytest.mark.parametrize(("route", "name"), [(MESSAGES, "message_stop"), (RESPONSES, "response.completed")])
def test_a_last_event_whose_data_is_not_whole_does_not_complete_a_stream(route: Route, name: str) -> None:
    attempt = replay(route, (100, opening(route) + text(route) + event('{"type": "' + name, name)))

    assert attempt.outcome is RequestOutcome.TRUNCATED_STREAM


def test_a_responses_stream_cut_short_at_a_limit_is_completed() -> None:
    cut_short = last_event(RESPONSES, 7, "response.incomplete")

    attempt = replay(RESPONSES, (100, opening(RESPONSES) + text(RESPONSES)), (200, cut_short))

    assert attempt.outcome is RequestOutcome.COMPLETED
    assert attempt.usage == Usage(input_tokens=20, cached_input_tokens=5, output_tokens=7)


# The accounting of a run.


def test_every_send_is_one_attempt_and_the_tally_accounts_for_each() -> None:
    attempts = Attempts()
    for route in ROUTES:
        start, body = (100, opening(route, usage=False)), [(200, text(route)), (300, text(route))]
        replay(route, (100, opening(route)), *body, (400, ending(route, 7)), into=attempts)
        replay(route, start, *body, (400, ending(route, None)), into=attempts)
        replay(route, start, *body, into=attempts)
        replay(route, start, (200, error(route)), into=attempts)
        replay(route, status=503, into=attempts)
        replay(route, status=None, into=attempts)
    waiting = attempts.send(MESSAGES, 500)

    tally = attempts.tally()

    assert (tally.attempts, tally.unfinished, len(attempts.ended)) == (19, 1, 18)
    assert tally.completed == 6
    assert tally.failures == {
        RequestOutcome.TRUNCATED_STREAM: 3,
        RequestOutcome.ERROR_EVENT: 3,
        RequestOutcome.HTTP_REFUSAL: 3,
        RequestOutcome.TRANSPORT_ERROR: 3,
    }
    assert tally.failed == 12
    assert tally.attempts == tally.completed + tally.failed + tally.unfinished
    # Only the first stream of each route reported a usage, and only it has a speed.
    assert (tally.usage_missing, tally.eligible) == (15, 3)

    # The reader that was waiting ends once, however often it is asked.
    ended = waiting.end(600)
    assert waiting.end(700) is ended
    assert attempts.ended[-1] is ended
    assert (attempts.tally().attempts, attempts.tally().unfinished) == (19, 0)

    # What each attempt measured is a request record the public format accepts.
    for number, attempt in enumerate(attempts.ended):
        record = RequestRecord(
            request=number,
            cell=0,
            student=0,
            conversation=0,
            turn=0,
            traffic=Traffic.BACKGROUND,
            trial=None,
            eligible_ms=0,
            finish_reason=None,
            **attempt.measured(),
        )
        assert (record.failed, record.usage_missing) == (attempt.failed, attempt.usage_missing)


@routes
def test_a_request_sent_again_is_a_retry_and_the_attempt_before_it_is_kept(route: Route) -> None:
    attempts = Attempts()

    first = replay(route, status=None, into=attempts)
    # The retry counts from its send, before anything of its answer.
    reader = attempts.send(route, 50, retry=1)
    sent = attempts.tally()
    assert (sent.attempts, sent.unfinished, sent.retries) == (2, 1, 1)
    reader.answered(200)
    reader.feed(opening(route) + text(route) + ending(route), 100)
    again = reader.end(110)

    assert attempts.ended == (first, again)
    assert (first.retry, first.outcome) == (0, RequestOutcome.TRANSPORT_ERROR)
    assert (again.retry, again.outcome) == (1, RequestOutcome.COMPLETED)
    tally = attempts.tally()
    assert (tally.attempts, tally.retries, tally.completed, tally.failed) == (2, 1, 1, 1)


# When the output came.


@routes
def test_reasoning_is_the_first_output_and_not_the_first_answer(route: Route) -> None:
    thought = [(100, opening(route)), (200, reasoning(route)), (300, reasoning(route))]

    answered = replay(route, *thought, (400, text(route)), (500, ending(route)))
    only_thought = replay(route, *thought, (500, ending(route)))

    assert output_times(answered) == (200, 400, 400)
    assert output_times(only_thought) == (200, None, 300)
    assert only_thought.outcome is RequestOutcome.COMPLETED


@routes
def test_a_chunk_of_pings_alone_moves_no_time(route: Route) -> None:
    ping = event({"type": "ping"}, "ping") if route is MESSAGES else b": keep-alive\n\n"

    attempt = replay(
        route,
        (50, ping),
        (100, opening(route)),
        (200, text(route)),
        (250, ping + ping),
        (300, text(route)),
        (350, ping),
        (400, ending(route)),
    )
    quiet = replay(route, (100, ping), (200, ping + ping))

    assert output_times(attempt) == (200, 200, 300)
    assert attempt.outcome is RequestOutcome.COMPLETED
    assert output_times(quiet) == (None, None, None)
    assert quiet.outcome is RequestOutcome.TRUNCATED_STREAM


@routes
def test_the_arguments_of_a_tool_call_are_answer_output_and_its_name_alone_is_not(route: Route) -> None:
    attempt = replay(
        route,
        (100, opening(route)),
        (200, tool_name(route)),
        (300, tool_arguments(route)),
        (400, tool_arguments(route)),
        (500, ending(route)),
    )

    assert output_times(attempt) == (300, 300, 400)


@routes
def test_events_that_arrive_together_share_one_time_and_a_cut_event_has_the_time_of_its_end(route: Route) -> None:
    piece = text(route)

    together = replay(route, (100, opening(route) + piece + piece + piece + ending(route, 3)))
    split = replay(
        route,
        (100, opening(route) + piece[:10]),
        (200, piece[10:] + piece[:10]),
        (300, piece[10:]),
        (400, ending(route, 3)),
    )

    assert together.outcome is RequestOutcome.COMPLETED
    assert together.usage == Usage(input_tokens=20, cached_input_tokens=5, output_tokens=3)
    assert output_times(together) == (100, 100, 100)
    # Three tokens in one moment: there is no time to divide by.
    assert (together.output_tokens_per_second, together.eligible) == (None, False)
    assert output_times(split) == (200, 200, 300)
    assert split.eligible


# Usage and speed.


@routes
def test_a_stream_that_reports_no_usage_is_completed_with_its_usage_missing(route: Route) -> None:
    body = [(100, opening(route, usage=False)), (200, text(route)), (300, text(route))]

    attempt = replay(route, *body, (400, ending(route, None)))

    assert (attempt.outcome, attempt.failed) == (RequestOutcome.COMPLETED, False)
    assert (attempt.usage, attempt.usage_missing) == (None, True)
    assert (attempt.output_tokens_per_second, attempt.eligible) == (None, False)


@routes
def test_an_output_count_is_the_answers_own_only_when_no_output_follows_it(route: Route) -> None:
    counted = replay(route, (100, opening(route) + text(route)), (200, ending(route, 7)))
    # The same count, reported before the last of the output: Responses reports it in a last event.
    early = usage_event(route, 7) or last_event(route, 7, "response.in_progress")
    followed = replay(route, (100, opening(route) + early + text(route)), (200, ending(route, None)))
    silent = replay(route, (100, opening(route, usage=False) + text(route)), (200, ending(route, None)))

    assert (counted.outcome, followed.outcome, silent.outcome) == (RequestOutcome.COMPLETED,) * 3
    assert (counted.output_counted, followed.output_counted, silent.output_counted) == (True, False, False)
    assert followed.usage is not None
    assert followed.usage.output_tokens == 7


def test_a_count_that_was_not_reported_is_missing_on_its_own() -> None:
    attempt = replay(MESSAGES, (100, opening(MESSAGES, usage=False) + text(MESSAGES)), (200, ending(MESSAGES, 7)))

    assert attempt.usage == Usage(input_tokens=None, cached_input_tokens=None, output_tokens=7)
    assert not attempt.usage_missing


@routes
def test_a_reply_of_one_token_is_completed_and_has_no_speed(route: Route) -> None:
    one_event = replay(route, (100, opening(route)), (200, text(route)), (300, ending(route, 1)))
    # One reported token is not enough, even when two events carried output at two times.
    two_events = replay(route, (100, opening(route)), (200, text(route)), (300, text(route)), (400, ending(route, 1)))

    for attempt in (one_event, two_events):
        assert attempt.outcome is RequestOutcome.COMPLETED
        assert attempt.usage is not None
        assert attempt.usage.output_tokens == 1
        assert (attempt.output_tokens_per_second, attempt.eligible) == (None, False)
    assert output_times(one_event) == (200, 200, 200)


@routes
def test_speed_is_the_output_tokens_after_the_first_over_the_seconds_from_first_output_to_last(route: Route) -> None:
    # Sent at 0 and ended at 4210: neither is in the rule. Reasoning is output, so the first is at 1500.
    body = [(1000, opening(route)), (1500, reasoning(route)), (2000, text(route)), (4000, text(route))]

    attempt = replay(route, *body, (4200, ending(route, 11)))

    assert attempt.eligible
    assert attempt.output_tokens_per_second == pytest.approx((11 - 1) / (4.0 - 1.5))


@pytest.mark.parametrize("route", [MESSAGES, CHAT])
def test_a_stream_that_did_not_complete_has_no_speed_whatever_it_reported(route: Route) -> None:
    body = [(1000, opening(route)), (1500, text(route)), (4000, text(route)), (4200, usage_event(route, 11))]

    attempt = replay(route, *body)

    assert attempt.outcome is RequestOutcome.TRUNCATED_STREAM
    assert attempt.usage is not None
    assert attempt.usage.output_tokens == 11
    assert (attempt.output_tokens_per_second, attempt.eligible) == (None, False)


# An answer that is not a stream.

WHOLE_USAGE = {
    Route.MESSAGES: {"input_tokens": 12, "output_tokens": 7},
    Route.RESPONSES: {"input_tokens": 12, "output_tokens": 7},
    Route.CHAT_COMPLETIONS: {"prompt_tokens": 12, "completion_tokens": 7},
}


@pytest.mark.parametrize("route", list(Route))
def test_an_answer_that_is_not_a_stream_is_completed_when_its_document_came_whole(route: Route) -> None:
    document = json.dumps({"id": "made-up", "error": None, "usage": WHOLE_USAGE[route]}).encode()
    refused = json.dumps({"type": "error", "error": {"message": ERROR_TEXT}}).encode()

    def read(*chunks: bytes, broken: bool = False, length: int | None = None) -> Attempt:
        reader = Attempts().send(route, 0)
        reader.answered(200, stream=False, length=length)
        for at, chunk in enumerate(chunks):
            reader.feed(chunk, at)
        return reader.end(10, transport_error=broken)

    whole = read(document[:9], document[9:])
    assert (whole.outcome, whole.usage) == (
        RequestOutcome.COMPLETED,
        Usage(input_tokens=12, cached_input_tokens=None, output_tokens=7),
    )
    # Nothing of it was output before all of it was: it has no time of output, and no speed.
    assert (output_times(whole), whole.eligible) == ((None, None, None), False)
    assert read(document[:-1]).outcome is RequestOutcome.TRUNCATED_STREAM
    assert read(document[:-1], broken=True).outcome is RequestOutcome.TRANSPORT_ERROR
    # A body that reads as a document and stops short of its declared length did not come whole.
    assert read(document, length=len(document)).outcome is RequestOutcome.COMPLETED
    assert read(document, length=len(document) + 1).outcome is RequestOutcome.TRUNCATED_STREAM
    assert read(document, broken=True).outcome is RequestOutcome.TRANSPORT_ERROR
    assert read(refused).outcome is RequestOutcome.ERROR_EVENT
