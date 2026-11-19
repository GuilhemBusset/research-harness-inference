"""A small imitation of an engine's prefix cache (system design, section 5.5).

An agent re-sends its whole transcript on every turn, and a real engine keeps what it has computed
for it: a turn that reaches the engine that served the previous one pays only for its new tokens.
The mock computes nothing, so this cache only counts. It lets the mock pool show what a real pool
shows: the cached share of the input, and what a student loses by reaching another instance.

- **One input for each salt.** The cache salt is per student (section 6.6), so a prefix is reused
  only by the student who sent it. For each salt the cache keeps the input of the last request
  with that salt that this engine served, as its tokens. A new request with the same salt has, as
  cached, the tokens it shares with that input from its start, and its own input then takes the
  place of the one kept. An input under another salt is never compared.
- **The last token is computed.** An engine computes at least the last token of a prompt, which
  the first token of its answer comes from, so a request that repeats the last one whole has all
  its tokens cached but that one. No answer reports an input read entirely from the cache.
- **A capacity, in tokens.** When the inputs kept hold more tokens than the capacity, the cache
  forgets whole inputs, first the salt whose last request is the oldest, until they fit: the
  engine forgets the prefixes of idle sessions first. An input larger than the capacity is
  therefore not kept at all.

What a token is belongs to the caller: here an input is a sequence, and a token is one item of it.
"""

from __future__ import annotations

from collections.abc import Sequence


class PrefixCache:
    """The last input of each salt, up to `capacity` tokens in all."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("the capacity is a number of tokens, one or more")
        self.capacity = capacity
        # In the order of each salt's last request, the oldest first: a dict keeps the order of insertion.
        self._inputs: dict[str, Sequence[str]] = {}
        self._kept = 0

    @property
    def kept(self) -> int:
        """The tokens of the inputs kept."""
        return self._kept

    @property
    def use(self) -> float:
        """The share of the capacity in use, from 0 to 1."""
        return self._kept / self.capacity

    def serve(self, salt: str, tokens: Sequence[str]) -> int:
        """The cached tokens of a request this engine serves; its input is then the one kept for its salt."""
        last = self._inputs.pop(salt, ())
        self._kept -= len(last)
        # All but the last token of the request can be cached.
        shared = min(len(last), max(len(tokens) - 1, 0))
        cached = next((at for at in range(shared) if last[at] != tokens[at]), shared)

        self._inputs[salt] = tokens
        self._kept += len(tokens)
        while self._kept > self.capacity:
            oldest = next(iter(self._inputs))
            self._kept -= len(self._inputs.pop(oldest))
        return cached
