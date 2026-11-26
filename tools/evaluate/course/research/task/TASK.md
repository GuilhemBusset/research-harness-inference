# Grounded research: insulation from a frozen packet of sources

`instance.json` holds a question about an insulation panel. Answer it from the sources in
`packet/` alone, with evidence for every number you use.

## The sources

`packet/` holds four sources, each with an ID: a laboratory report as a PDF (`S1.pdf`) and three
text sources (`S2.md`, `S3.md`, `S4.md`). They do not all agree, and they do not all measure the
same thing: read what each one is, and take every fact from the source that gives it for the
panels and the conditions the question names.

The command `search` searches the packet. `search cork density` prints the passages that match
best, each with its source ID and, where it has them, its page and table number;
`search --sources` lists the sources. No other source may be used: there is no network.

## The answer

Write `answer.json` in the working directory, a JSON object of two parts.

`evidence` is the evidence table: a list with one row for each fact that `instance.json` names
under `facts`, and no other row. A row gives:

- `fact`: the fact's identifier, as `instance.json` names it;
- `value`: the value the source gives, as a JSON number;
- `unit`: its unit, as text, for example `W/(m·K)`;
- `source`: the ID of the source it is taken from, for example `S1`;
- `page` and `table`: where in that source it is printed. Give the page number of a PDF, the
  table number of a table, or both. A text source has no pages.

`conclusion` is the result the question asks for. It gives these fields and no other (no
sentence of prose: the answer's sentence is written from them):

- `fact`: the result's identifier, as `instance.json` names it under `result`;
- `value` and `unit`: the result, as a JSON number and its unit;
- `citations`: for each fact the result is computed from, an object with that `fact`, its
  `source` and its `page` or `table`, as in the evidence table.

For example, a row:

```json
{"fact": "conductivity.straw.dry", "value": 0.05, "unit": "W/(m·K)", "source": "S9", "page": 4, "table": "7"}
```

The example is not from the packet: its identifier, its value and its place are made up.
