# data/context/

Human-provided context that explains patterns in the activity data the raw
FIT/TCX/GPX telemetry can't explain on its own -- injuries, life events,
equipment changes, training decisions. This is the kind of thing that's
obvious to you but invisible to any pipeline reading device data: a
multi-year drop in ride distance reads as an ambiguous fitness trend without
it, and as a documented, time-bound response to an injury with it.

## Files

- **`life_events.json`** -- the actual events, an array of objects validated
  against `data/schema/life_event.schema.json`.
- **`validate_life_events.py`** -- run this after editing the file:
  ```
  python data/context/validate_life_events.py
  ```
  Checks schema conformance, duplicate `id`s, dangling `related_event_ids`,
  and `date_end` not preceding `date`.

## Adding a new entry

Each event needs at minimum: a unique kebab-case `id`, a `date`
(`YYYY-MM-DD`), a `category` (`health` / `life-event` / `equipment` /
`training` / `other`), a short `title`, and a fuller free-text
`description` -- write that part the way you'd actually explain it, detail
included. Optional fields:

- `date_end` -- for something that spans a period, not a single day.
- `date_precision: "approximate"` -- when you don't know the exact date
  (e.g. "sometime in early 2020"). Defaults to `"exact"`.
- `ongoing: true` -- for a state that started at `date` and is still true,
  with no end date yet.
- `tags` -- free-form, for filtering.
- `related_event_ids` -- IDs of other events in this file that this one
  follows from or connects to (e.g. a treatment linking back to its
  diagnosis).

Run the validator, then commit.

## Using it in analysis

The file is plain JSON, so DuckDB (or pandas) reads it exactly like the
activity data:

```sql
SELECT * FROM read_json_auto('data/context/life_events.json');

-- Join events onto rides that happened during them:
SELECT a.id, a.start_time, e.title
FROM read_json_auto('data/processed/*.json') a
JOIN read_json_auto('data/context/life_events.json') e
  ON a.start_time::TIMESTAMP >= e.date::DATE
 AND a.start_time::TIMESTAMP <= coalesce(e.date_end::DATE, e.date::DATE + INTERVAL 1 DAY)
```

Use this to annotate trend charts, filter out or flag periods explained by
a known event, or just answer "what was going on when this happened?"
without having to remember or re-explain it each time.
