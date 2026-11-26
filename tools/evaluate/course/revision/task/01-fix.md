# Task: the van's parcels come out wrong

A courier loads one delivery van a day. `solver.py` chooses which parcels it takes: the parcels
whose fees add up to the most, within what the van can carry. Run it on today's parcels:

```sh
python solver.py case.json answer.json
```

`case.json` holds the van's `capacity` in whole kilograms and the `parcels`, each with an `id`, a
`weight` in whole kilograms and a `fee`. Do not change it. The program writes `answer.json`: the
ids of the parcels it chooses, `chosen`, and what their fees add up to, `value`.

The answer it gives for `case.json` is wrong. Find out why, and fix `solver.py` so that it gives
the best choice. Keep its command line and the format of what it writes.

The program is run on other days' parcels too, with other weights, fees and capacities: it must
give the best choice for any case of this form, not only for `case.json`. When you are done, say
what was wrong.
