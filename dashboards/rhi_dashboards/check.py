"""Holds a built dashboard to the rules of `rhi_dashboards.rules` (system design, section 7.2).

`violations` reads a dashboard as the build wrote it, its JSON, whatever built it: a panel made
without the helpers is held to the same rules as one made with them. It reports:

| Rule | What breaks it |
| --- | --- |
| `data-source` | A query, or anything else in the dashboard, that does not name one of the two fixed data sources |
| `rate` | `rate`, `irate` or `increase` of anything but a plain selector: a sum, a subquery |
| `asleep` | A panel that queries an engine metric and does not take the pool's state from the scaler: a query not kept by `rules.awake`, no `rules.asleep` query, shown, whose zero reads "asleep", or, in a panel of one value, a query over the range. An engine series read over the session's minutes (`rules.over_sessions`) is kept by `rules.awake` inside the gate, at each minute, and needs no "asleep": it has no "now" |
| `engine-row` | An engine metric queried for no pool, for a pool the configuration does not hold, or for a pool whose engine names it otherwise or leaves its quantity out. An engine metric is any name of the per-engine table, its further quantities included |
| `percentile` | A `histogram_quantile` whose panel does not show the count behind it: a query, shown, that is the summed increase of the same histogram's count, for the same series over the same window, and nothing else. Buckets read over the session's minutes need the count read over the same minutes, by the same `rules.over_sessions` |
| `undeclared` | A metric name no declaration covers: neither a declared family nor that family's name followed by a sample's suffix |
| `declaration` | A declared file that is not in the repository or does not contain the name; an exporter with no exact version |
| `unreadable` | An expression this module cannot read |

Only the Prometheus queries are read for metric names. An expression is read by its tokens, not
parsed: enough to tell a metric name from a function, a label and a keyword, and to find what a
function was called on. A query is shown unless it is disabled (`hide`): Grafana does not run a
disabled query, so what it would return is on no panel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from rhi_common.engine_metrics import ENGINES, Kind, further_names, metric_names
from rhi_dashboards import rules

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from rhi_common.pool_config import Pool

REPO_ROOT = Path(__file__).resolve().parents[2]

# The data sources a dashboard may name: identifier and type.
DATA_SOURCES = {rules.PROMETHEUS_UID: "prometheus", rules.CLOUDWATCH_UID: "cloudwatch"}
# The functions that read a counter, which restarts from zero with its instance.
COUNTER_FUNCTIONS = frozenset({"rate", "irate", "increase"})
# The panels that show one value for each series, not its course over the range.
ONE_VALUE_PANELS = frozenset({"stat", "gauge", "bargauge"})
SAMPLES = (rules.COUNTER_SAMPLE, *rules.HISTOGRAM_SAMPLES)
# One version, in full: no range, no "or later".
EXACT_VERSION = re.compile(r"\d+(\.\d+)+([.+-][0-9A-Za-z.+-]+)?")

_TOKEN = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)
    | (?P<variable>\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*)
    | (?P<number>\.?\d[A-Za-z0-9_.]*)
    | (?P<name>[A-Za-z_:][A-Za-z0-9_:]*)
    | (?P<operator>=~|!~|!=|==|>=|<=|.)
    """,
    re.VERBOSE | re.DOTALL,
)
_CLOSING = {"(": ")", "{": "}", "[": "]"}
# Names that are followed by a list of labels, and the other words that are not metrics.
_LABEL_LISTS = frozenset({"by", "without", "on", "ignoring", "group_left", "group_right"})
_KEYWORDS = frozenset({"and", "or", "unless", "bool", "offset", "atan2", "inf", "nan"})
_AGGREGATIONS = frozenset(
    {"sum", "avg", "min", "max", "count", "group", "stddev", "stdvar", "topk", "bottomk", "quantile", "count_values"}
    | {"limitk", "limit_ratio"}
)


@dataclass(frozen=True)
class Violation:
    """One rule a dashboard breaks: the rule, the panel (or the dashboard) and what was found."""

    rule: str
    where: str
    detail: str


class _Unreadable(ValueError):
    """The expression is not one this module can read."""


@dataclass(frozen=True)
class _Selector:
    """A metric's selector: its name, its matchers, its range (None without one) and its tokens' span."""

    name: str
    matchers: frozenset[tuple[str, str, str]]
    window: str | None
    start: int
    end: int


@dataclass(frozen=True)
class _Call:
    """A function call: its name and the span of the tokens between its parentheses."""

    function: str
    start: int
    end: int


@dataclass(frozen=True)
class _Expression:
    tokens: tuple[str, ...]
    selectors: tuple[_Selector, ...]
    calls: tuple[_Call, ...]

    def argument(self, call: _Call) -> _Selector | None:
        """The selector that is the whole of the call's argument, or None when it is anything else."""
        return next((one for one in self.selectors if (one.start, one.end) == (call.start, call.end)), None)


def _tokens(expression: str) -> tuple[list[str], list[str]]:
    """The expression's tokens and the kind of each, spaces left out."""
    texts: list[str] = []
    kinds: list[str] = []
    for match in _TOKEN.finditer(expression):
        if match.lastgroup != "space":
            texts.append(match.group())
            kinds.append(str(match.lastgroup))
    return texts, kinds


def _closing(texts: Sequence[str], opening: int) -> int:
    """The index of the bracket that closes the one at `opening`."""
    depth = 0
    for index in range(opening, len(texts)):
        if texts[index] == texts[opening]:
            depth += 1
        elif texts[index] == _CLOSING[texts[opening]]:
            depth -= 1
            if depth == 0:
                return index
    raise _Unreadable("a bracket is never closed")


def _unquoted(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text[1:-1]) if text[0] != "`" else text[1:-1]


def _matchers(texts: Sequence[str], kinds: Sequence[str]) -> frozenset[tuple[str, str, str]]:
    """The matchers between a selector's braces, each a label, an operator and a value."""
    found = set()
    index = 0
    while index < len(texts):
        if texts[index] == ",":
            index += 1
            continue
        triple = kinds[index : index + 3]
        if triple != ["name", "operator", "string"] or texts[index + 1] not in {"=", "!=", "=~", "!~"}:
            raise _Unreadable("a selector's matchers cannot be read")
        found.add((texts[index], texts[index + 1], _unquoted(texts[index + 2])))
        index += 3
    return frozenset(found)


def _read(expression: str) -> _Expression:
    """The expression's metric selectors and function calls."""
    texts, kinds = _tokens(expression)
    selectors: list[_Selector] = []
    calls: list[_Call] = []
    index = 0
    while index < len(texts):
        text = texts[index]
        following = texts[index + 1] if index + 1 < len(texts) else ""
        if text == "{":
            raise _Unreadable("a selector names no metric")
        if text == "[":
            # The range of a subquery: no metric is in it.
            index = _closing(texts, index) + 1
        elif kinds[index] != "name" or text.lower() in _KEYWORDS:
            index += 1
        elif text.lower() in _LABEL_LISTS:
            index = _closing(texts, index + 1) + 1 if following == "(" else index + 1
        elif following == "(":
            calls.append(_Call(text, index + 2, _closing(texts, index + 1)))
            index += 1
        elif text.lower() in _AGGREGATIONS:
            index += 1
        else:
            end = index + 1
            matchers: frozenset[tuple[str, str, str]] = frozenset()
            if following == "{":
                closing = _closing(texts, end)
                matchers = _matchers(texts[end + 1 : closing], kinds[end + 1 : closing])
                end = closing + 1
            window = None
            if end < len(texts) and texts[end] == "[":
                closing = _closing(texts, end)
                window = "".join(texts[end + 1 : closing])
                end = closing + 1
            selectors.append(_Selector(text, matchers, window, index, end))
            index = end
    return _Expression(tuple(texts), tuple(selectors), tuple(calls))


@dataclass(frozen=True)
class _Session:
    """A span of tokens that is what `rules.over_sessions` writes: the function, the span of the
    whole call, the span of the expression it gates, and the subquery's range."""

    function: str
    start: int
    end: int
    inner_start: int
    inner_end: int
    range: str

    @property
    def key(self) -> str:
        return f"{self.function}[{self.range}]"


def _session_gate() -> list[str]:
    """The tokens `rules.in_session` puts after the expression it gates."""
    return _tokens(rules.in_session("x"))[0][3:]


def _sessions(tokens: Sequence[str]) -> list[_Session]:
    """Every span of the tokens that `rules.over_sessions` writes."""
    gate = _session_gate()
    found = []
    for index, text in enumerate(tokens):
        if not text.endswith("_over_time") or tokens[index + 1 : index + 3] != ("(", "("):
            continue
        try:
            call_end = _closing(tokens, index + 1)
            group_end = _closing(tokens, index + 2)
            if tokens[group_end + 1 : group_end + 2] != ("[",) or tokens[index + 3] != "(":
                continue
            range_end = _closing(tokens, group_end + 1)
            inner_end = _closing(tokens, index + 3)
        except _Unreadable:
            continue
        if range_end + 1 != call_end or list(tokens[inner_end + 1 : group_end]) != gate:
            continue
        span_range = "".join(tokens[group_end + 2 : range_end])
        found.append(_Session(text, index, call_end + 1, index + 4, inner_end, span_range))
    return found


def _session_of(sessions: Sequence[_Session], start: int, end: int) -> _Session | None:
    """The innermost session span that holds the tokens from `start` to `end`."""
    holding = sorted(
        (one for one in sessions if one.inner_start <= start and end <= one.inner_end),
        key=lambda one: one.end - one.start,
    )
    return holding[0] if holding else None


def _opening(tokens: Sequence[str], closing: int) -> int:
    """The index of the bracket that the one at `closing` closes."""
    opening = {value: key for key, value in _CLOSING.items()}[tokens[closing]]
    depth = 0
    for index in range(closing, -1, -1):
        if tokens[index] == tokens[closing]:
            depth += 1
        elif tokens[index] == opening:
            depth -= 1
            if depth == 0:
                return index
    raise _Unreadable("a bracket is never opened")


def _awake_in_session(expression: _Expression, selector: _Selector, pool: str) -> bool:
    """Whether the selector is in an expression that `rules.awake` keeps for the pool, itself in a
    span that `rules.over_sessions` writes: kept to the pool in service at every minute."""
    gate = _tokens(rules.awake(pool, "x"))[0][3:]
    tokens = expression.tokens
    sessions = _sessions(tokens)
    for index in range(1, len(tokens) - len(gate) + 1):
        if list(tokens[index : index + len(gate)]) != gate or tokens[index - 1] != ")":
            continue
        try:
            opening = _opening(tokens, index - 1)
        except _Unreadable:
            continue
        if opening < selector.start and selector.end < index and _session_of(sessions, opening, index + len(gate)):
            return True
    return False


def _engine_names() -> dict[str, list[tuple[str, rules.EngineQuantity]]]:
    """Every series name of the per-engine table, the further quantities' included: the engines and
    the quantity it names."""
    names: dict[str, list[tuple[str, rules.EngineQuantity]]] = {}
    for engine in ENGINES:
        row: list[tuple[rules.EngineQuantity, Kind, str]] = [
            *((quantity, quantity.kind, family) for quantity, family in metric_names(engine).items()),
            *((quantity, family.kind, family) for quantity, family in further_names(engine).items()),
        ]
        for quantity, kind, family in row:
            samples = {
                Kind.GAUGE: ("",),
                Kind.COUNTER: ("", rules.COUNTER_SAMPLE),
                Kind.HISTOGRAM: ("", *rules.HISTOGRAM_SAMPLES),
            }[kind]
            for sample in samples:
                names.setdefault(family + sample, []).append((engine, quantity))
    return names


def _panels(panels: Iterable[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    """Every panel, those inside a row included."""
    for panel in panels:
        yield panel
        yield from _panels(panel.get("panels") or ())


def _data_sources(value: object) -> Iterator[object]:
    """Every data source named anywhere in the dashboard."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "datasource":
                yield item
            else:
                yield from _data_sources(item)
    elif isinstance(value, list):
        for item in value:
            yield from _data_sources(item)


def _is_fixed(source: object) -> bool:
    """Whether a data source is named by one of the two fixed identifiers, with its type."""
    if not isinstance(source, dict):
        return False
    uid = source.get("uid")
    return uid in DATA_SOURCES and source.get("type") == DATA_SOURCES[uid]


def _declaration_problems(declared: Iterable[rules.Declared], root: Path) -> Iterator[Violation]:
    for one in declared:
        if isinstance(one, rules.EmittedBy):
            path = PurePosixPath(one.path)
            file = root / path
            token = re.compile(rf"(?<![A-Za-z0-9_:]){re.escape(one.name)}(?![A-Za-z0-9_:])")
            if path.is_absolute() or ".." in path.parts or not file.is_file():
                yield Violation("declaration", one.name, f"{one.path} is not a file of this repository")
            elif token.search(file.read_text(encoding="utf-8")) is None:
                yield Violation("declaration", one.name, f"{one.path} does not contain the name")
        elif isinstance(one, rules.ExportedBy) and (not one.exporter or EXACT_VERSION.fullmatch(one.version) is None):
            yield Violation("declaration", one.name, "the exporter is not named with one exact version")


def _keeps_awake(expression: _Expression, pool: str) -> bool:
    """Whether the whole expression is what `rules.awake` writes for the pool."""
    gate = _tokens(rules.awake(pool, "x"))[0][3:]
    tokens = list(expression.tokens)
    if tokens[-len(gate) :] != gate or tokens[0] != "(":
        return False
    try:
        return _closing(tokens, 0) == len(tokens) - len(gate) - 1
    except _Unreadable:
        return False


def _is_shown(target: Mapping[str, Any]) -> bool:
    """Whether the query's result reaches its panel: a disabled query is not run."""
    return not target.get("hide")


def _is_prometheus(target: Mapping[str, Any]) -> bool:
    source = target.get("datasource")
    return isinstance(source, dict) and source.get("uid") == rules.PROMETHEUS_UID


def _counted(expression: _Expression) -> _Selector | None:
    """The series whose increase, summed, is all the expression returns; None when it returns anything else.

    What `rules.awake` adds for the series' own pool is not counted against it. A mean, a ratio or
    a comparison that holds the same increase is not a count. A count summed over the session's
    minutes (`rules.over_sessions`, with `sum_over_time`) is a count too, of the series over its
    window within those minutes.
    """
    tokens = expression.tokens
    whole = [one for one in _sessions(tokens) if (one.start, one.end) == (0, len(tokens))]
    if whole and whole[0].function == "sum_over_time":
        session = whole[0]
        inner = _counted(_read(" ".join(tokens[session.inner_start : session.inner_end])))
        if inner is None:
            return None
        return _Selector(inner.name, inner.matchers, f"{inner.window}@{session.key}", inner.start, inner.end)
    start, end = 0, len(tokens)
    if len(expression.selectors) == 2:
        pools = [
            value
            for label, operator, value in expression.selectors[0].matchers
            if (label, operator) == (rules.POOL, "=")
        ]
        if len(pools) != 1 or not _keeps_awake(expression, pools[0]):
            return None
        start, end = 1, _closing(tokens, 0)
    elif len(expression.selectors) != 1:
        return None
    selector = expression.selectors[0]
    increases = [call for call in expression.calls if call.function == "increase"]
    if len(increases) != 1 or expression.argument(increases[0]) != selector:
        return None
    index = start
    while index < end:
        if index == selector.start:
            index = selector.end
        elif tokens[index].lower() in {"by", "without"} and tokens[index + 1 : index + 2] == ("(",):
            index = _closing(tokens, index + 1) + 1
        elif tokens[index].lower() in {"sum", "increase", "(", ")"}:
            index += 1
        else:
            return None
    return selector


def _reads_asleep(panel: Mapping[str, Any], pool: str) -> bool:
    """Whether the panel holds `rules.asleep` for the pool, as a query, shown, whose zero reads "asleep"."""
    state = _tokens(rules.asleep(pool))[0]
    references = {
        target.get("refId")
        for target in panel.get("targets") or ()
        if _is_shown(target) and _is_prometheus(target) and _tokens(target.get("expr") or "")[0] == state
    }
    for override in (panel.get("fieldConfig") or {}).get("overrides") or ():
        matcher = override.get("matcher") or {}
        if matcher.get("id") != "byFrameRefID" or matcher.get("options") not in references:
            continue
        for value in override.get("properties") or ():
            for mapping in (value.get("value") or ()) if value.get("id") == "mappings" else ():
                zero = (mapping.get("options") or {}).get("0") if mapping.get("type") == "value" else None
                if isinstance(zero, dict) and zero.get("text") == rules.ASLEEP:
                    return True
    return False


def _panel_problems(
    panel: Mapping[str, Any],
    covered: frozenset[rules.EngineQuantity],
    families: frozenset[str],
    pools: Mapping[str, Pool],
) -> Iterator[Violation]:
    where = f"panel {panel.get('title')!r}"
    engine_names = _engine_names()
    targets = panel.get("targets") or ()
    read: list[_Expression] = []
    counts: set[tuple[str, frozenset[tuple[str, str, str]], str | None]] = set()
    asleep_for: set[str] = set()
    for target in targets:
        if target.get("datasource") is None:
            yield Violation("data-source", where, f"query {target.get('refId')!r} names no data source")
        if not _is_prometheus(target):
            continue
        try:
            expression = _read(target.get("expr") or "")
        except _Unreadable as problem:
            yield Violation("unreadable", where, f"query {target.get('refId')!r}: {problem}")
            continue
        read.append(expression)
        counted = _counted(expression) if _is_shown(target) else None
        if counted is not None:
            counts.add((counted.name, counted.matchers, counted.window))

        for call in expression.calls:
            argument = expression.argument(call)
            plain = argument is not None and argument.window is not None and ":" not in argument.window
            if call.function in COUNTER_FUNCTIONS and not plain:
                yield Violation("rate", where, f"{call.function} of something other than one metric's series")

        for selector in expression.selectors:
            rows = engine_names.get(selector.name)
            if rows is None:
                name = selector.name
                if not any(name == family or name in {family + sample for sample in SAMPLES} for family in families):
                    yield Violation("undeclared", where, f"{name} is not declared")
                continue
            if not any(quantity in covered for _, quantity in rows):
                yield Violation("undeclared", where, f"{selector.name} is not declared from the per-engine table")
            named = [value for label, operator, value in selector.matchers if (label, operator) == (rules.POOL, "=")]
            pool = pools.get(named[0]) if len(named) == 1 else None
            if pool is None:
                yield Violation("engine-row", where, f"{selector.name} is not queried for one configured pool")
            elif all(engine != pool.engine for engine, _ in rows):
                yield Violation("engine-row", where, f"{selector.name} is not in the row of the pool's engine")
            elif not _awake_in_session(expression, selector, pool.name):
                asleep_for.add(pool.name)
                if not _keeps_awake(expression, pool.name):
                    yield Violation("asleep", where, f"a query of {selector.name} is not kept to the pool in service")

    for pool in sorted(asleep_for):
        if not _reads_asleep(panel, pool):
            yield Violation("asleep", where, f"no query of the in-service count of {pool!r} reads asleep at zero")
    if asleep_for and panel.get("type") in ONE_VALUE_PANELS and not all(target.get("instant") for target in targets):
        yield Violation("asleep", where, "a panel of one value asks for the range, and would keep an old value")

    for expression in read:
        sessions = _sessions(expression.tokens)
        for call in expression.calls:
            if call.function != "histogram_quantile":
                continue
            buckets = [
                one
                for one in expression.selectors
                if call.start <= one.start < call.end and one.name.endswith("_bucket")
            ]
            if not buckets:
                yield Violation("percentile", where, "a percentile of no histogram's buckets")
            for bucket in buckets:
                count = bucket.name.removesuffix("_bucket") + "_count"
                session = _session_of(sessions, bucket.start, bucket.end)
                window = bucket.window if session is None else f"{bucket.window}@{session.key}"
                if (count, bucket.matchers, window) not in counts:
                    yield Violation("percentile", where, f"no query shows the increase of {count} over the same window")


def violations(
    document: Mapping[str, Any],
    declared: Iterable[rules.Declared],
    pools: Iterable[Pool],
    root: Path = REPO_ROOT,
) -> list[Violation]:
    """Every rule the dashboard breaks; none when it follows them all.

    `document` is the dashboard's JSON, `declared` what its module declares of its metric names
    (the helpers' own are added), `pools` the pools of the configuration it was built from, and
    `root` the repository the declared files are in.
    """
    declared = (*rules.METRICS, *declared)
    found = list(_declaration_problems(declared, root))
    found += [
        Violation("data-source", "the dashboard", "a data source that is not one of the two fixed ones")
        for source in _data_sources(document)
        if not _is_fixed(source)
    ]
    covered = frozenset(one.quantity for one in declared if isinstance(one, rules.FromEngineTable))
    families = frozenset(one.name for one in declared if not isinstance(one, rules.FromEngineTable))
    by_name = {pool.name: pool for pool in pools}
    for panel in _panels(document.get("panels") or ()):
        found += _panel_problems(panel, covered, families, by_name)
    return found
