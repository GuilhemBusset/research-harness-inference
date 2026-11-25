"""Holds a built dashboard to the rules of `rhi_dashboards.rules` (system design, section 7.2).

`violations` reads a dashboard as the build wrote it, its JSON, whatever built it: a panel made
without the helpers is held to the same rules as one made with them. It reports:

| Rule | What breaks it |
| --- | --- |
| `data-source` | A query, or anything else in the dashboard, that names none of the two fixed data sources, nor one of Grafana's own two: its server-side expressions, or the mixed source of a panel whose queries each name their own; a query that names the mixed source itself |
| `rate` | `rate`, `irate` or `increase` of anything but a plain selector: a sum, a subquery |
| `pool-state` | A panel that queries an engine metric and does not take the pool's state from the scaler: a query not kept by `rules.serving`; a state of `rules.STATES` with no query of `rules.state_queries`, shown, whose zero reads the state's text; or, in a panel of one value, a query over the range. An engine counter, or a histogram's buckets or count, read as what each of its samples added in its pool's session (`rules.session_increment`, which `rules.over_sessions` sums) needs neither: a series no instance updates any more adds nothing, so nothing an instance last reported is counted again. An engine gauge always does: it falls as well as rises, and what its samples "added" is no count |
| `session` | In a panel of one value, a query, shown, that reads a session's gauge and asks for the range: each of its points looks back over a range of its own, and the panel's last one can show a session before the dashboard's range. Such a query is an instant one, read at the range's end over the range |
| `engine-row` | An engine metric queried for no pool, for a pool the configuration does not hold, or for a pool whose engine names it otherwise or leaves its quantity out. An engine metric is any name of the per-engine table, its further quantities included |
| `percentile` | A `histogram_quantile` whose panel does not show the count behind it: a query, shown, that is the summed increase of the same histogram's count, for the same series over the same window, and nothing else |
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
# Grafana's own, which read nothing themselves: identifier and type.
BUILT_IN = {str(one.uid): str(one.type_val) for one in (rules.EXPRESSIONS, rules.MIXED)}
# The functions that read a counter, which restarts from zero with its instance.
COUNTER_FUNCTIONS = frozenset({"rate", "irate", "increase"})
# The panels that show one value for each series, not its course over the range.
ONE_VALUE_PANELS = frozenset({"stat", "gauge", "bargauge", "table"})
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


# A name that stands for a selector in what a helper writes.
_PLACEHOLDER = "x"


def _increment_spans(tokens: Sequence[str], series: Sequence[str], session: rules.Session) -> Iterator[tuple[int, int]]:
    """Every span of the tokens that is `rules.session_increment` of the series' tokens, in the session."""
    for previous in rules.PREVIOUS:
        pattern: list[str] = []
        for token in _tokens(rules.session_increment(_PLACEHOLDER, previous, session))[0]:
            pattern.extend(series if token == _PLACEHOLDER else (token,))
        for index in range(len(tokens) - len(pattern) + 1):
            if list(tokens[index : index + len(pattern)]) == pattern:
                yield index, index + len(pattern)


def _counted_per_sample(expression: _Expression, selector: _Selector, session: rules.Session) -> bool:
    """Whether the selector is read as what each of its samples added, taken in the session: a
    pool's, by its name, or any pool's (`rules.session_increment`). A series no instance updates
    any more adds nothing, so nothing an instance last reported is counted again."""
    tokens = expression.tokens
    own = list(tokens[selector.start : selector.end])
    if selector.window is not None:
        own = own[: own.index("[")]
    return any(
        start <= selector.start and selector.end <= end for start, end in _increment_spans(tokens, own, session)
    )


def _engine_rows() -> Iterator[tuple[str, rules.EngineQuantity, Kind, str]]:
    """Every family of the per-engine table, the further quantities' included: its engine, the
    quantity it names, its kind and its name."""
    for engine in ENGINES:
        for quantity, family in metric_names(engine).items():
            yield engine, quantity, quantity.kind, family
        for quantity, family in further_names(engine).items():
            yield engine, quantity, family.kind, family


def _engine_names() -> dict[str, list[tuple[str, rules.EngineQuantity]]]:
    """Every series name of the per-engine table, the further quantities' included: the engines and
    the quantity it names."""
    names: dict[str, list[tuple[str, rules.EngineQuantity]]] = {}
    for engine, quantity, kind, family in _engine_rows():
        samples = {
            Kind.GAUGE: ("",),
            Kind.COUNTER: ("", rules.COUNTER_SAMPLE),
            Kind.HISTOGRAM: ("", *rules.HISTOGRAM_SAMPLES),
        }[kind]
        for sample in samples:
            names.setdefault(family + sample, []).append((engine, quantity))
    return names


def _rising_names() -> frozenset[tuple[str, str]]:
    """Every engine and series name of the per-engine table that only rises while its process
    runs, so that a fall is a restart: a counter's, and a histogram's buckets and count. A gauge
    falls as well as rises, and what each of its samples "added" means nothing."""
    rising = {
        Kind.GAUGE: (),
        Kind.COUNTER: ("", rules.COUNTER_SAMPLE),
        Kind.HISTOGRAM: ("_bucket", "_count"),
    }
    return frozenset(
        (engine, family + sample) for engine, _, kind, family in _engine_rows() for sample in rising[kind]
    )


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
    """Whether a data source is named by one of the two fixed identifiers, or of Grafana's own two, with its type."""
    if not isinstance(source, dict):
        return False
    uid = source.get("uid")
    known = DATA_SOURCES | BUILT_IN
    return uid in known and source.get("type") == known[uid]


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


def _kept_while_serving(expression: _Expression, pool: str) -> bool:
    """Whether the whole expression is what `rules.serving` writes for the pool."""
    gate = _tokens(rules.serving(pool, _PLACEHOLDER))[0][3:]
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

    What `rules.serving` adds for the series' own pool, the scaler's two gauges of it, is not
    counted against it. A mean, a ratio or a comparison that holds the same increase is not a count.
    """
    tokens = expression.tokens
    start, end = 0, len(tokens)
    if len(expression.selectors) == 3:
        pools = [
            value
            for label, operator, value in expression.selectors[0].matchers
            if (label, operator) == (rules.POOL, "=")
        ]
        if len(pools) != 1 or not _kept_while_serving(expression, pools[0]):
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


def _reads_state(panel: Mapping[str, Any], pool: str, state: rules.State) -> bool:
    """Whether the panel holds the state's query for the pool (`rules.state_queries`), shown, whose
    zero reads the state's text. The value is matched by its query, or, in a table, by the name
    Grafana gives it (`rules.table_value`)."""
    expression = _tokens(rules.state_queries(pool)[state])[0]
    references = {
        target.get("refId")
        for target in panel.get("targets") or ()
        if _is_shown(target) and _is_prometheus(target) and _tokens(target.get("expr") or "")[0] == expression
    }
    matchers = [("byFrameRefID", reference) for reference in references]
    matchers += [("byName", rules.table_value(reference)) for reference in references if isinstance(reference, str)]
    for override in (panel.get("fieldConfig") or {}).get("overrides") or ():
        matcher = override.get("matcher") or {}
        if (matcher.get("id"), matcher.get("options")) not in matchers:
            continue
        for value in override.get("properties") or ():
            for mapping in (value.get("value") or ()) if value.get("id") == "mappings" else ():
                zero = (mapping.get("options") or {}).get("0") if mapping.get("type") == "value" else None
                if isinstance(zero, dict) and zero.get("text") == state.text:
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
    rising = _rising_names()
    targets = panel.get("targets") or ()
    read: list[tuple[Mapping[str, Any], _Expression]] = []
    counts: set[tuple[str, frozenset[tuple[str, str, str]], str | None]] = set()
    state_for: set[str] = set()
    for target in targets:
        source = target.get("datasource")
        if source is None:
            yield Violation("data-source", where, f"query {target.get('refId')!r} names no data source")
        elif isinstance(source, dict) and source.get("uid") == rules.MIXED.uid:
            # The mixed source only routes a panel's queries to their own: a query needs a real one.
            yield Violation("data-source", where, f"query {target.get('refId')!r} names the mixed source, not its own")
        if not _is_prometheus(target):
            continue
        try:
            expression = _read(target.get("expr") or "")
        except _Unreadable as problem:
            yield Violation("unreadable", where, f"query {target.get('refId')!r}: {problem}")
            continue
        read.append((target, expression))
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
            elif (pool.engine, selector.name) not in rising or not _counted_per_sample(
                expression, selector, pool.name
            ):
                state_for.add(pool.name)
                if not _kept_while_serving(expression, pool.name):
                    yield Violation("pool-state", where, f"a query of {selector.name} is not kept to the pool while it serves")

    for pool in sorted(state_for):
        for state in rules.STATES:
            if not _reads_state(panel, pool, state):
                yield Violation("pool-state", where, f"no query of the state of {pool!r} reads {state.text!r}")
    one_value = panel.get("type") in ONE_VALUE_PANELS
    if state_for and one_value and not all(target.get("instant") for target in targets):
        yield Violation("pool-state", where, "a panel of one value asks for the range, and would keep an old value")
    for target, expression in read:
        reads_session = any(one.name == rules.SESSION_ACTIVE for one in expression.selectors)
        if one_value and reads_session and _is_shown(target) and not target.get("instant"):
            yield Violation("session", where, f"query {target.get('refId')!r} is read over the session and asks for the range")

    for _, expression in read:
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
                if (count, bucket.matchers, bucket.window) not in counts:
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
        Violation("data-source", "the dashboard", "a data source that is neither one of the two fixed ones nor Grafana's own")
        for source in _data_sources(document)
        if not _is_fixed(source)
    ]
    covered = frozenset(one.quantity for one in declared if isinstance(one, rules.FromEngineTable))
    families = frozenset(one.name for one in declared if not isinstance(one, rules.FromEngineTable))
    by_name = {pool.name: pool for pool in pools}
    for panel in _panels(document.get("panels") or ()):
        found += _panel_problems(panel, covered, families, by_name)
    return found
