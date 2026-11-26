"""The trusted grader of the grounded-research task: what a run answered, against its key and the packet.

A run writes one file, `answer.json` (`task/TASK.md`): an *evidence table*, one row for each fact
the question needs, and a *conclusion*, the result the question asks for. Every row and the
conclusion name the fact they state by the identifier the instance gives it, its value and its
unit; a row cites the source and the page or table it takes the value from, and the conclusion
cites, for each fact it rests on, where that fact is. The grade answers the four properties of
`PROPERTIES`, in that order, each on its own, over the evidence table and the conclusion alike:

- *fact*: every fact of the question has a row, every row of it states the key's value, no row
  states a fact the question does not name, and the conclusion states the result the question
  asks for (its identifier) with the key's value, and nothing else: a conclusion is its checked
  fields, and the sentence a reader is shown is written from them (`Conclusion.sentence`), so
  that no prose of a run can say what its grade does not;
- *unit*: every row and the conclusion give a unit of the fact's quantity;
- *source*: every row, and the conclusion for every fact it rests on, cites the packet's source
  that holds the fact;
- *location*: every such citation names a page or a table, and that page or table of the fact's
  source is where the packet gives the fact: a neighbouring table of the same source fails.

**The key** (`Key`) is the trusted answer of an instance: for each fact its value as the source
prints it, its unit, its source and where in it the fact is, and the result's value and unit. It
is checked against the packet when it is read (`Key.parse`): each place it names must be a
passage of the packet's index that prints the fact's figure. So a citation the key accepts is one
the packet bears out. The grader compares an answer with the key and the packet, never with the
reference answer: any answer that states the same facts, in any unit of their quantity, with any
valid citation, is accepted.

**Tolerance.** A value may differ from the key's by `VALUE_TOLERANCE`, relative, once converted
to the key's unit: a result rounded to three figures is within it, the neighbouring value of a
table is not. A value given in a unit of another quantity is compared as it is written, so a right
number with a wrong unit breaks the unit alone.

It runs nothing a run submitted. A file that is missing or cannot be read is a rejection, with the
fault said: `grade` raises only on what the trusted side gave it.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

ANSWER = "answer.json"

VALUE_TOLERANCE = 0.01


class Property(StrEnum):
    FACT = "fact"
    UNIT = "unit"
    SOURCE = "source"
    LOCATION = "location"


# What a grade answers, in the order of the suite's `required_properties`.
PROPERTIES = tuple(Property)


# --- Units -----------------------------------------------------------------------------------
#
# A unit is parsed into the powers of its base units and a factor to them, so that W/(m·K),
# W/m/K, W m^-1 K^-1 and W·m⁻¹·K⁻¹ are one unit and (W/m)·K another. A product is written with a
# space, `·`, `⋅`, `*`, `×` or `.`, or with nothing between two symbols (`kgm-3`); a power with or
# without a caret, in digits or superscripts, its minus a hyphen, `−` or `⁻`. What follows a `/`
# up to the next `/` or the end of its parenthesis divides: W/m·K is read as W/(m·K), as the
# sources print it; a divisor that is a parenthesis ends with it, so kg/(m³)·m is kg·m/m³. A
# prefix binds to the symbol it is written against, as SI writes it, and a prefixed symbol is
# never read as the product of its parts: mg is a milligram, not a metre times a gram, mm a
# millimetre, so kg/mm is a mass per length, and mW/(m·mK) is a milliwatt per metre per
# millikelvin. One notation is the exception, the datasheets' divisor written run together: where
# the SI reading measures no quantity, a metre written against another symbol as the first factor
# of a divisor is the two of them, so W/mK, which measures none as a watt per millikelvin, is
# W/(m·K). A unit whose factor is out of range, such as km^200, is no unit, whatever another
# reading of it would measure. A unit of a quantity of `QUANTITIES` is known; any other is no unit
# here.

# The prefixes a symbol of `_PREFIXED` may carry, and their factors.
_PREFIXES: Mapping[str, float] = {
    "M": 1e6, "k": 1e3, "h": 1e2, "da": 1e1, "d": 1e-1, "c": 1e-2, "m": 1e-3, "µ": 1e-6, "μ": 1e-6, "n": 1e-9,
}  # fmt: skip
# The unprefixed symbols that take a prefix: their factor to the base units, and their powers.
_PREFIXED: Mapping[str, tuple[float, Mapping[str, int]]] = {
    "K": (1.0, {"K": 1}),
    "W": (1.0, {"W": 1}),
    "g": (1e-3, {"kg": 1}),
    "m": (1.0, {"m": 1}),
    "L": (1e-3, {"m": 3}),
    "l": (1e-3, {"m": 3}),
}

# Each symbol: its factor to the base units, and its powers of them.
SYMBOLS: Mapping[str, tuple[float, Mapping[str, int]]] = {
    **_PREFIXED,
    **{
        prefix + symbol: (scale * factor, powers)
        for symbol, (factor, powers) in _PREFIXED.items()
        for prefix, scale in _PREFIXES.items()
    },
}

QUANTITIES: Mapping[str, Mapping[str, int]] = {
    "thermal conductivity": {"W": 1, "m": -1, "K": -1},
    "density": {"kg": 1, "m": -3},
    "mass per area": {"kg": 1, "m": -2},
}

_SIGNS = str.maketrans({"⁻": "-", "−": "-", "–": "-", "¹": "1", "²": "2", "³": "3", "⋅": "·", "×": "·"})
_GAP = re.compile(r"(?P<space>\s+)|(?P<sign>[·*./()])")
_POWER = re.compile(r"\^?(-?[0-9]+)")
# The symbols, the longest first: a prefix binds to the symbol it is written against.
_BY_LENGTH = sorted(SYMBOLS, key=len, reverse=True)
# The longest unit text read, and the most readings of one tried: a bound on the work.
_LONGEST_UNIT = 64
_MOST_READINGS = 64

type _Token = str | tuple[float, dict[str, int]]


class _NoUnit(Exception):
    """A text that is no unit the grader reads."""


class _OutOfRange(Exception):
    """A unit with a factor no float holds: no reading of it is taken."""


def _in_range(factor: float) -> float:
    if not (math.isfinite(factor) and factor > 0):
        raise _OutOfRange
    return factor


# Each symbol, and the symbols of its base: m, mm and km are all metres.
_SAME_BASE: Mapping[str, frozenset[str]] = {
    symbol: frozenset(other for other, (_factor, theirs) in SYMBOLS.items() if theirs == powers)
    for symbol, (_scale, powers) in SYMBOLS.items()
}


def _symbol_at(text: str, at: int) -> str | None:
    """The longest symbol a text gives at a place, or None."""
    return next((symbol for symbol in _BY_LENGTH if text.startswith(symbol, at)), None)


def _readings(unit: str) -> Iterator[list[_Token]]:
    """The ways to read a unit as factors and signs, a product written out as `·`, the SI one first.

    The symbols of a run of letters are matched the longest first, so that the first reading binds
    each prefix to its symbol; a later one splits a prefixed symbol into a metre and the next
    symbol, only as the first factor of a divisor and only where that symbol is no metre (W/mK).
    A symbol raised out of the range of a float raises `_OutOfRange`.
    """
    text = unit.translate(_SIGNS).strip()
    if len(text) > _LONGEST_UNIT:
        return

    def joined(tokens: tuple[_Token, ...], token: _Token) -> tuple[_Token, ...]:
        # Two factors side by side, with a space or nothing between them, are a product.
        if (
            tokens
            and (not isinstance(tokens[-1], str) or tokens[-1] == ")")
            and (not isinstance(token, str) or token == "(")
        ):
            return (*tokens, "·", token)
        return (*tokens, token)

    # Where no reading of the rest of the text ends: whether one does depends on the place alone.
    dead: set[int] = set()

    def onward(at: int, tokens: tuple[_Token, ...]) -> Iterator[list[_Token]]:
        while at < len(text) and (gap := _GAP.match(text, at)):
            at = gap.end()
            if gap["sign"]:  # a space is a product between two factors, nothing next to a sign
                tokens = joined(tokens, "·" if gap["sign"] in "*." else gap["sign"])
        if at == len(text):
            yield list(tokens)
            return
        if at in dead:
            return
        ended = False
        # Only the first factor of a divisor may be split, as datasheets run it together (W/mK).
        divisor = tokens[-1:] == ("/",) or tokens[-2:] == ("/", "(")
        longest = None
        for symbol in _BY_LENGTH:
            if text.startswith(symbol, at):
                if longest is None:
                    longest = symbol
                elif not divisor or _symbol_at(text, at + len(symbol)) in (None, *_SAME_BASE[symbol]):
                    continue  # a prefixed symbol is one symbol: mg a milligram, mm a millimetre
                factor, powers = SYMBOLS[symbol]
                raised = _POWER.match(text, at + len(symbol))
                power = int(raised[1]) if raised else 1
                try:
                    scaled = _in_range(factor**power)
                except OverflowError:
                    raise _OutOfRange from None
                token = (scaled, {base: exponent * power for base, exponent in powers.items()})
                for reading in onward(raised.end() if raised else at + len(symbol), joined(tokens, token)):
                    ended = True
                    yield reading
        if not ended:
            dead.add(at)

    yield from itertools.islice(onward(0, ()), _MOST_READINGS)


def _parse(tokens: list[_Token]) -> tuple[float, dict[str, int]]:
    """The factor of a reading of a unit to its base units, and its powers of them."""
    at = 0

    def product() -> tuple[float, dict[str, int]]:
        nonlocal at
        factor, powers = factor_of()
        while at < len(tokens) and tokens[at] == "·":
            at += 1
            more = factor_of()
            factor, powers = factor * more[0], _times(powers, more[1], 1)
        return factor, powers

    def quotient() -> tuple[float, dict[str, int]]:
        nonlocal at
        factor, powers = product()
        while at < len(tokens) and tokens[at] in ("/", "·"):
            sign = tokens[at]
            at += 1
            if sign == "·":  # after a parenthesized divisor, what follows it multiplies
                more = product()
                factor, powers = factor * more[0], _times(powers, more[1], 1)
                continue
            # A parenthesis is the whole divisor: what follows it multiplies.
            divisor = factor_of() if at < len(tokens) and tokens[at] == "(" else product()
            factor, powers = factor / divisor[0], _times(powers, divisor[1], -1)
        return factor, powers

    def factor_of() -> tuple[float, dict[str, int]]:
        nonlocal at
        if at >= len(tokens):
            raise _NoUnit
        token = tokens[at]
        at += 1
        if token == "(":
            inner = quotient()
            if at >= len(tokens) or tokens[at] != ")":
                raise _NoUnit
            at += 1
            return inner
        if isinstance(token, str):
            raise _NoUnit
        return token

    whole = quotient()
    if at != len(tokens):
        raise _NoUnit
    return whole


def _times(powers: Mapping[str, int], more: Mapping[str, int], sign: int) -> dict[str, int]:
    together = dict(powers)
    for base, exponent in more.items():
        together[base] = together.get(base, 0) + sign * exponent
    return {base: exponent for base, exponent in together.items() if exponent}


def _unit(unit: str) -> tuple[str, float] | None:
    """The quantity a unit measures and its factor to the quantity's base unit, or None.

    The first reading of the unit (`_readings`) that measures a quantity is the one taken. A
    factor out of the range of a float, in any reading tried, is no unit: the whole unit is
    rejected, and no other reading of it is taken.
    """
    try:
        for reading in _readings(unit):
            try:
                factor, powers = _parse(reading)
            except _NoUnit:
                continue
            _in_range(factor)
            for quantity, base in QUANTITIES.items():
                if powers == base:
                    return quantity, factor
    except (_OutOfRange, OverflowError, ZeroDivisionError):
        return None
    return None


# --- The packet and the key ------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """A page, a table, or both: where in a source a fact is, or where a citation says it is."""

    page: int | None
    table: str | None

    def within(self, place: Location) -> bool:
        """Whether a citation names this place: each of a page and a table it gives is the place's."""
        given = [(self.page, place.page), (self.table, place.table)]
        return any(mine is not None for mine, _theirs in given) and all(
            mine is None or mine == theirs for mine, theirs in given
        )


@dataclass(frozen=True)
class Passage:
    source: str
    location: Location
    text: str


@dataclass(frozen=True)
class Packet:
    """The packet as the search tool's index maps it: its sources and their passages."""

    sources: frozenset[str]
    passages: tuple[Passage, ...]

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Packet:
        passages = tuple(
            Passage(one["source"], Location(one["page"], one["table"]), one["text"]) for one in raw["passages"]
        )
        packet = cls(sources=frozenset(raw["sources"]), passages=passages)
        if any(passage.source not in packet.sources for passage in passages):
            raise ValueError("a passage of the packet's index belongs to no source of it")
        return packet

    def prints(self, source: str, location: Location, figure: str) -> bool:
        """Whether the passage of a source at exactly this place prints a figure."""
        printed = re.compile(rf"(?<![\d.]){re.escape(figure)}(?![\d])")
        return any(
            passage.source == source and passage.location == location and printed.search(passage.text)
            for passage in self.passages
        )


@dataclass(frozen=True)
class Fact:
    fact_id: str
    value: float
    unit: str
    source: str
    locations: tuple[Location, ...]


@dataclass(frozen=True)
class Result:
    fact_id: str
    value: float
    unit: str
    rests_on: tuple[str, ...]


@dataclass(frozen=True)
class Key:
    """The trusted answer of one instance, as `key/<instance>.json` gives it."""

    facts: Mapping[str, Fact]
    result: Result

    @classmethod
    def parse(cls, raw: Mapping[str, Any], packet: Packet) -> Key:
        """The key, held to the packet: a key the packet does not bear out is refused."""
        facts = {}
        for fact_id, one in raw["facts"].items():
            locations = tuple(Location(place.get("page"), place.get("table")) for place in one["locations"])
            fact = Fact(fact_id, float(one["figure"]), one["unit"], one["source"], locations)
            if not locations or not all(packet.prints(fact.source, place, one["figure"]) for place in locations):
                raise ValueError(f"the packet does not print {fact_id} where its key says")
            facts[fact_id] = fact
        result = raw["result"]
        key = cls(facts, Result(result["fact"], float(result["value"]), result["unit"], tuple(result["rests_on"])))
        if not set(key.result.rests_on) <= set(facts) or not key.result.rests_on:
            raise ValueError("the result of a key rests on facts the key does not hold")
        if any(_unit(unit) is None for unit in (key.result.unit, *(fact.unit for fact in facts.values()))):
            raise ValueError("a key gives a unit the grader does not know")
        return key


# --- The answer ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    fact_id: str
    source: str
    location: Location


@dataclass(frozen=True)
class Row:
    fact_id: str
    value: float
    unit: str
    citation: Citation


@dataclass(frozen=True)
class Conclusion:
    fact_id: str
    value: float
    unit: str
    citations: tuple[Citation, ...]
    # The fields of the conclusion that are none of the above, such as a sentence of prose.
    unchecked: tuple[str, ...] = ()

    def sentence(self) -> str:
        """The conclusion as a reader is shown it: written from the fields the grader checks."""
        cited = "; ".join(f"{one.fact_id}: {one.source}, {_where(one.location)}" for one in self.citations)
        return f"{self.fact_id} = {self.value:g} {self.unit}" + (f" ({cited})" if cited else "")


CONCLUSION_FIELDS = frozenset({"fact", "value", "unit", "citations"})


@dataclass(frozen=True)
class Answer:
    evidence: tuple[Row, ...]
    conclusion: Conclusion


@dataclass(frozen=True)
class Report:
    """What the grader found: each fault with the property it breaks."""

    faults: tuple[tuple[Property, str], ...]
    # The conclusion as a reader is shown it (`Conclusion.sentence`), when the answer was read.
    conclusion: str | None = None

    def holds(self, one: Property) -> bool:
        return all(broken is not one for broken, _fault in self.faults)

    @property
    def properties(self) -> tuple[bool, ...]:
        """One answer for each property, in the order of `PROPERTIES`."""
        return tuple(self.holds(one) for one in PROPERTIES)

    @property
    def accepted(self) -> bool:
        return not self.faults


class _Unreadable(Exception):
    """An answer that does not hold what the task asks for."""


def grade(key: Key, packet: Packet, artifacts: Path) -> Report:
    """Grade the `answer.json` of a folder against an instance's key and the packet."""
    try:
        answer = _read_answer(artifacts / ANSWER)
    except _Unreadable as error:
        # No answer: none of its properties is established.
        return Report(tuple((one, str(error)) for one in PROPERTIES))
    faults: list[tuple[Property, str]] = []
    for fact in key.facts.values():
        rows = [row for row in answer.evidence if row.fact_id == fact.fact_id]
        if not rows:
            faults += [(one, f"the evidence table has no row for {fact.fact_id}") for one in PROPERTIES]
        for row in rows:
            what = f"the row of {fact.fact_id}"
            faults += _value_faults(what, row.value, row.unit, fact.value, fact.unit)
            faults += _citation_faults(what, row.citation, fact, packet)
    # A row of a fact the question does not name is evidence the key cannot bear out.
    for row in answer.evidence:
        if row.fact_id not in key.facts:
            faults.append(
                (Property.FACT, f"the evidence table states {row.fact_id!r}, which the question does not name")
            )
            if row.citation.source not in packet.sources:
                faults.append(
                    (
                        Property.SOURCE,
                        f"the row of {row.fact_id!r} cites {row.citation.source!r}, no source of the packet",
                    )
                )
    faults += _conclusion_faults(answer.conclusion, key, packet)
    return Report(tuple(faults), conclusion=answer.conclusion.sentence())


def _conclusion_faults(conclusion: Conclusion, key: Key, packet: Packet) -> list[tuple[Property, str]]:
    result = key.result
    # The conclusion a reader is shown is written from its checked fields; any other field, such
    # as a sentence of prose, could say what the grade does not, so it is not taken.
    faults: list[tuple[Property, str]] = [
        (Property.FACT, f"the conclusion gives `{field}`, which the task does not ask for and no grade checks")
        for field in conclusion.unchecked
    ]
    if conclusion.fact_id != result.fact_id:
        faults.append(
            (Property.FACT, f"the conclusion states {conclusion.fact_id}, and the question asks {result.fact_id}")
        )
    faults += _value_faults("the conclusion", conclusion.value, conclusion.unit, result.value, result.unit)
    for fact_id in result.rests_on:
        cited = [citation for citation in conclusion.citations if citation.fact_id == fact_id]
        if not cited:
            faults += [
                (one, f"the conclusion cites nothing for {fact_id}") for one in (Property.SOURCE, Property.LOCATION)
            ]
        for citation in cited:
            faults += _citation_faults(f"the conclusion's citation of {fact_id}", citation, key.facts[fact_id], packet)
    faults += [
        (Property.SOURCE, f"the conclusion cites {citation.fact_id}, on which its result does not rest")
        for citation in conclusion.citations
        if citation.fact_id not in result.rests_on
    ]
    return faults


def _value_faults(
    what: str, value: float, unit: str, expected: float, expected_unit: str
) -> list[tuple[Property, str]]:
    faults: list[tuple[Property, str]] = []
    given, wanted = _unit(unit), _unit(expected_unit)
    assert wanted is not None  # Key.parse refuses a key whose unit is unknown
    if given is None or given[0] != wanted[0]:
        faults.append((Property.UNIT, f"{what} gives {unit!r}, which is no unit of {wanted[0]}"))
        scale = 1.0
    else:
        scale = given[1] / wanted[1]
    converted = value * scale
    if not abs(converted - expected) <= VALUE_TOLERANCE * abs(expected):
        faults.append(
            (Property.FACT, f"{what} states {value:g} {unit}, which is not the key's {expected:g} {expected_unit}")
        )
    return faults


def _citation_faults(what: str, citation: Citation, fact: Fact, packet: Packet) -> list[tuple[Property, str]]:
    faults: list[tuple[Property, str]] = []
    if citation.source not in packet.sources:
        faults.append((Property.SOURCE, f"{what} cites {citation.source!r}, which is no source of the packet"))
    elif citation.source != fact.source:
        faults.append((Property.SOURCE, f"{what} cites {citation.source}, which does not give {fact.fact_id}"))
    # The place is judged within the fact's source, so a wrong source alone breaks the source alone.
    if citation.location.page is None and citation.location.table is None:
        faults.append((Property.LOCATION, f"{what} cites no page and no table"))
    elif not any(citation.location.within(place) for place in fact.locations):
        faults.append((Property.LOCATION, f"{what} cites {_where(citation.location)}, where {fact.fact_id} is not"))
    return faults


def _where(location: Location) -> str:
    parts = ([f"page {location.page}"] if location.page is not None else []) + (
        [f"Table {location.table}"] if location.table is not None else []
    )
    return ", ".join(parts)


# --- Reading the answer ----------------------------------------------------------------------

_TABLE = re.compile(r"(?:table|tab\.?)?\s*([0-9A-Za-z]{1,8})", re.IGNORECASE)
_PAGE = re.compile(r"[0-9]{1,6}")


def _text(raw: Mapping[str, Any], field: str, where: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _Unreadable(f"{ANSWER}: {where} has no text `{field}`")
    return value.strip()


def _number(raw: Mapping[str, Any], field: str, where: str) -> float:
    value = raw.get(field)
    # A boolean is no value, and neither is NaN, an infinity or an integer no float holds.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Unreadable(f"{ANSWER}: {where} has no number `{field}`")
    try:
        number = float(value)
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        raise _Unreadable(f"{ANSWER}: {where} has a `{field}` that is not finite")
    return number


def _location(raw: Mapping[str, Any], where: str) -> Location:
    page, table = raw.get("page"), raw.get("table")
    # Digits in ASCII only: `str.isdigit` also takes a superscript, which no `int` reads.
    if isinstance(page, str) and _PAGE.fullmatch(page.strip()):
        page = int(page)
    if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
        raise _Unreadable(f"{ANSWER}: {where} has a `page` that is no page number")
    if isinstance(table, int) and not isinstance(table, bool):
        table = str(table)
    if table is not None:
        matched = _TABLE.fullmatch(table.strip()) if isinstance(table, str) else None
        if matched is None:
            raise _Unreadable(f"{ANSWER}: {where} has a `table` that is no table number")
        table = matched.group(1)
    return Location(page, table)


def _citation(raw: object, fact_id: str | None, where: str) -> Citation:
    if not isinstance(raw, dict):
        raise _Unreadable(f"{ANSWER}: {where} is not a JSON object")
    return Citation(fact_id or _text(raw, "fact", where), _text(raw, "source", where), _location(raw, where))


def _read_answer(path: Path) -> Answer:
    if not path.is_file():
        raise _Unreadable(f"{ANSWER} was not written")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise _Unreadable(f"{ANSWER} is not a JSON document") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("evidence"), list) or not raw["evidence"]:
        raise _Unreadable(f"{ANSWER} is not a JSON object with an evidence table, a list of rows")
    rows = []
    for number, row in enumerate(raw["evidence"], 1):
        where = f"row {number} of the evidence table"
        if not isinstance(row, dict):
            raise _Unreadable(f"{ANSWER}: {where} is not a JSON object")
        fact_id = _text(row, "fact", where)
        value, unit = _number(row, "value", where), _text(row, "unit", where)
        rows.append(Row(fact_id, value, unit, _citation(row, fact_id, where)))
    conclusion = raw.get("conclusion")
    if not isinstance(conclusion, dict) or not isinstance(conclusion.get("citations"), list):
        raise _Unreadable(f"{ANSWER} has no conclusion with a list of citations")
    citations = tuple(
        _citation(one, None, f"citation {number} of the conclusion")
        for number, one in enumerate(conclusion["citations"], 1)
    )
    return Answer(
        evidence=tuple(rows),
        conclusion=Conclusion(
            fact_id=_text(conclusion, "fact", "the conclusion"),
            value=_number(conclusion, "value", "the conclusion"),
            unit=_text(conclusion, "unit", "the conclusion"),
            citations=citations,
            unchecked=tuple(sorted(str(field) for field in conclusion if field not in CONCLUSION_FIELDS)),
        ),
    )
