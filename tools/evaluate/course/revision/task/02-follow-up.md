Thanks. One more change: the depot now limits, on some days, how many parcels the van takes,
whatever they weigh. On those days the case carries one more field, `max_items`, a whole number:

```json
{"capacity": 30, "max_items": 3, "parcels": [...]}
```

Change `solver.py` so that, when a case carries `max_items`, it chooses at most that many parcels,
still the ones whose fees add up to the most within the capacity. A case without `max_items` has
no limit and must be solved exactly as before. Keep the command line and the format of what it
writes. To try it, copy `case.json` and add a limit to the copy.
