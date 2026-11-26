"""The ten causes of an error the gateway produces (system design, section 6.2, step 8).

Students paste errors into public issues, so no error carries text from AWS, from the engine or
from an exception (section 5.3). An error names one cause of this list instead, and says one
sentence, written here and nowhere else. Each cause has the HTTP status of the design's table.

Three causes ask the client to come back, with a `Retry-After` header: `retry_after` is the number
of seconds, and it is `None` for the seven others. The three numbers are the defaults of the
design's section 17; whoever renders an error may give others for these three.

A sentence holds no number and no name of the deployment: limits, budgets and delays are
configuration, and a sentence is the same in every deployment.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self


class Cause(StrEnum):
    """Why the gateway did not answer a request; the value is the name the error carries."""

    status: int
    retry_after: int | None
    sentence: str

    def __new__(cls, name: str, status: int, retry_after: int | None, sentence: str) -> Self:
        cause = str.__new__(cls, name)
        cause._value_ = name
        cause.status = status
        cause.retry_after = retry_after
        cause.sentence = sentence
        return cause

    INVALID_KEY = ("invalid_key", 401, None, "The API key is missing or is not valid.")
    NOT_FOUND = ("not_found", 404, None, "This endpoint has no such route.")
    REQUEST_REFUSED = ("request_refused", 400, None, "The body of the request was refused.")
    CONCURRENCY_LIMIT = (
        "concurrency_limit",
        429,
        5,
        "You are at your limit of concurrent requests: retry when one of them has ended.",
    )
    WARMING = ("warming", 503, 30, "The model is starting: retry in a moment.")
    CAPACITY_UNAVAILABLE = (
        "capacity_unavailable",
        503,
        60,
        "No machine is available for this model at the moment: retry later.",
    )
    # No Retry-After: the gateway does not know when the next session starts (section 5.6).
    NO_SESSION = ("no_session", 503, None, "No session is on: this endpoint serves only during class sessions.")
    ENGINE_ERROR = ("engine_error", 502, None, "The model failed to answer this request.")
    TIME_LIMIT = ("time_limit", 504, None, "The request reached its time limit.")
    # The engine refused a prompt longer than the pool's context. The sentence begins with the words
    # of Anthropic's own error for it, by which Claude Code knows to compact the session.
    CONTEXT_LENGTH_EXCEEDED = (
        "context_length_exceeded",
        400,
        None,
        "prompt is too long: the conversation is longer than this model's context.",
    )
