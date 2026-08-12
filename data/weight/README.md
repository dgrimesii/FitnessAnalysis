# data/weight/

Body-weight readings, extracted from a Google Fit Takeout export -- the one
biometric this project's Strava-based activity data can never see on its
own. Useful for connecting training volume/intensity to the actual goal
(muscle mass, weight) rather than just activity counts.

## Files

- **`weight.json`** -- the readings, an array of objects validated against
  `data/schema/weight.schema.json`. One entry per logged reading (not
  daily -- these are manual entries, logged whenever the user weighed in).
- **`validate_weight.py`** -- run this after re-extracting or hand-editing
  the file:
  ```
  python data/weight/validate_weight.py
  ```
  Checks schema conformance, duplicate `id`s, `weight_lb`/`weight_kg`
  consistency, and that `weight_kg` falls in a plausible human range (a
  quick catch for unit mistakes).

## Regenerating from a new Takeout export

Source: Google Takeout → Fit (weight is logged manually in the Google Fit
app, plus one reading synced in from Hevy). In the exported zip, the file
to use is the *merged* weight datastream -- already deduplicated across
every raw source, so it's the one file to read, not each raw source
separately:

```
Takeout/Fit/All Data/derived_com.google.weight_com.google.android.gms_merge_weight.json
```

**Takeout's web export can silently drop most of "All Data"** if the
browser tab closes before extraction finishes, or the zip only gets
partially unpacked -- if this file is missing from an export folder that
should have it, check the original `.zip` directly before assuming Google
never captured it:

```
unzip -l takeout-*.zip | grep -i weight
```

Then run:

```
python ingestion/google_fit/extract_weight.py \
    --input-file "<path to Takeout>/Fit/All Data/derived_com.google.weight_com.google.android.gms_merge_weight.json"
```

This overwrites `weight.json` with the full, re-deduplicated set from the
new export -- run `validate_weight.py` afterward, then commit.

## Using it in analysis

Plain JSON, so DuckDB reads it exactly like the activity data:

```sql
SELECT * FROM read_json_auto('data/weight/weight.json') ORDER BY recorded_at;

-- Weight readings alongside training volume for the same period:
SELECT date_trunc('week', a.start_time) AS wk, count(*) AS activities,
       (SELECT avg(weight_lb) FROM read_json_auto('data/weight/weight.json') w
        WHERE w.recorded_at::TIMESTAMP BETWEEN date_trunc('week', a.start_time)
          AND date_trunc('week', a.start_time) + INTERVAL 7 DAY) AS avg_weight_lb
FROM read_json_auto('data/processed/*.json') a
GROUP BY 1 ORDER BY 1
```
