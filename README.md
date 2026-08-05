# FitnessAnalysis

Collects fitness data from apps like Strava, structures it into a standardized repository, and provides tools for analysis — including pre-defined reports and AI-driven insights.

This is a personal, long-running project. This README doubles as a status snapshot — what's built, what's known to be missing, and where the rough edges are — meant to be read before planning the next round of work rather than re-derived from scratch each time.

## Overview

FitnessAnalysis is organized around three core functions:

1. **Collect** — pull raw activity data from external fitness platforms
2. **Structure** — normalize and store that data in a consistent, analysis-ready format
3. **Analyze** — explore the data through pre-defined reports and AI-assisted analysis

## Status at a glance

| Area | Status |
|---|---|
| Strava bulk export ingestion (Garmin `.fit.gz`) | Done — 2,628 activities processed |
| Peloton ingestion (`.tcx.gz`) | Done — 92 activities processed |
| Per-point time series (GPS/HR/cadence/power → Parquet) | Done — one file per activity, 2,720 total |
| GPX ingestion | Declined — 8 files, closed as won't-fix (see [Known gaps](#known-gaps--deliberately-out-of-scope)) |
| Activity names | Done — backfilled for existing data, captured going forward |
| Human-provided context (injuries, life events) | Done — structured, schema-validated, queryable alongside activity data |
| Local analysis dashboard | Done — reads project data directly, no hosting/publishing step |
| Automated tests | 103 passing (`ingestion/strava/tests/`) |
| Live Strava API ingestion (OAuth, incremental sync) | Not started — everything so far is from a one-time bulk export |

## Project Structure

```
FitnessAnalysis/
├── ingestion/       # Data collection from external sources
├── data/            # Standardized/processed data storage
├── analysis/        # Analysis tools and reports
└── README.md
```

### `ingestion/`

- **`strava/`** — parses a bulk Strava data export into the standardized schema below.
  - `fit_parser.py` — the main pipeline. Walks an input directory of `.fit`/`.fit.gz`/`.tcx`/`.tcx.gz` entries (a flat file or a folder containing one), parses each into standardized JSON, extracts per-point track data to Parquet, and moves the source entry to a "processed" folder so re-running only ever sees what's left. Supports `--batch-size` for working through a large export incrementally.
  - `backfill_activity_names.py` — one-time backfill that populates `name` on already-processed activities from Strava's `activities.csv` (the FIT/TCX payloads themselves don't carry the activity title).
  - `tests/` — 103 tests covering both scripts (`test_fit_parser.py`, `test_backfill_activity_names.py`).
  - *(Future sources — e.g. live Garmin/Apple Health APIs — would follow the same pattern as separate submodules. Nothing here does live API/OAuth ingestion yet; see the table above.)*

### `data/`

Defines and stores the standardized schema that all sources get mapped into, regardless of origin platform. Keeping this layer source-agnostic means analysis tools only ever need to work against one consistent format.

- **`schema/`**
  - `activity.schema.json` — the standardized per-activity record (JSON Schema draft-07). One object per activity: identity/timing fields at the top level, then one of `endurance_metrics` / `strength_metrics` / `class_metrics` populated depending on activity type (cardio/GPS, strength, or instructor-led class).
  - `life_event.schema.json` — schema for entries in `context/life_events.json` (see below).
- **`processed/`** — 2,720 standardized JSON activity records, one file per activity, named by activity ID. This is the primary analysis-ready dataset.
- **`processed_tracks/`** — per-point time series (timestamp, lat/lon, altitude, heart rate, cadence, speed, power) as Parquet, one file per activity, joinable back to `processed/` on activity ID. This is what makes anything below session-summary granularity possible (e.g. "average cadence excluding stopped time").
- **`raw/`** — intended location for unmodified source data pulled directly from an API (kept for auditability/reprocessing). Not populated yet — the current pipeline reads directly from the bulk export on the mapped Google Drive location rather than staging a local raw copy first.
- **`context/`** — human-provided context (injuries, life events, equipment/training changes) that explains patterns the device data alone can't. See `data/context/README.md` for the schema and how to add entries; see [Known gaps](#known-gaps--deliberately-out-of-scope) for what this currently does and doesn't cover.
- **`logs/`** / **`summaries/`** — per-batch output from `fit_parser.py` runs: `logs/batch_<id>.log` has per-failure detail, `summaries/batch_<id>.md` has processed/succeeded/failed/skipped counts for that run. One pair per batch run, kept for traceability of how the full dataset was built up.

### `analysis/`

- **`reports/`** — pre-defined analysis.
  - `dashboard.html` — a local HTML page ("Eleven Years, Two Very Different Riders") covering ride volume/distance over time, cadence/speed trends, commute time-of-day patterns, Peloton adoption, and a recovery timeline tied to `data/context/life_events.json`. Runs entirely off local project files — open it via VS Code's **Live Server** extension (or any local HTTP server); it will not work opened directly as a `file://` path, since browsers block `fetch()` from that origin. Not hosted or published anywhere.
  - `build_dashboard_data.py` — regenerates `analysis/reports/data/dashboard_data.json`, the pre-aggregated chart data behind the dashboard, by re-running a set of DuckDB queries against `data/processed/*.json` + `data/processed_tracks/*.parquet` (plus Strava's `activities.csv` directly, for strength-training sessions — see [Known gaps](#known-gaps--deliberately-out-of-scope)). Run this after processing a new batch of activities so the dashboard reflects the latest data — see [Keeping things current](#keeping-things-current).
  - `requirements.txt` — `duckdb`, used only by the build script above.
- **`ai/`** — placeholder for AI-assisted, natural-language analysis over the dataset. Not started.

## Known gaps / deliberately out of scope

Worth reading before planning new work — some of these are real limitations, not oversights:

- **GPX ingestion was declined** (issue #3, closed as won't-fix). Only 8 files in the export, 2 of them empty shells; the other 6 carry no calorie/HR/cadence/power data at all (lat/lon/ele/time only). Judged not worth a parser path for 8 statistically negligible activities. If GPX-only activity types become more common in future exports, this decision is worth revisiting.
- **Strength-training set/rep detail isn't captured.** Strava's own FIT export for Hevy-synced strength activities has no underlying GPS/sensor file — confirmed during the very first ingestion work (issue #1) — so there's nothing for `fit_parser.py` to parse. `strength_metrics.exercises` exists in the schema but stays empty until a richer source (e.g. a direct Hevy export) is added. The dashboard's recovery chart currently reads strength-session *dates* directly out of `activities.csv` as a workaround, not through the standard pipeline.
- **No timezone offset in the source data.** FIT/TCX timestamps are UTC with no stored offset. Hour-of-day and weekday analysis (e.g. in the dashboard) assumes a fixed US Eastern offset, which will drift slightly around DST boundaries. There's no per-activity timezone field to correct this properly without an external lookup (e.g. by GPS coordinate).
- **Peloton sometimes splits one class into multiple synced activities** (warm-up / main set / cool-down as separate files), so raw Peloton activity counts run a bit higher than real workout sessions. Not corrected in `data/processed/`; only worked around ad hoc in dashboard prose, not systematically.
- **The dashboard's bulk chart data is not live**, unlike `data/context/life_events.json` (which the page fetches fresh on every load). A browser has no way to glob a folder of thousands of files the way DuckDB's CLI can, so `build_dashboard_data.py` has to be re-run manually after new activities are processed. See [Keeping things current](#keeping-things-current).
- **`data/raw/` is defined in the schema doc but not actually populated** — the pipeline currently reads directly from the mapped Google Drive export rather than staging raw copies locally first. Fine for a one-time bulk import; would need revisiting for a recurring/incremental sync.
- **No live Strava API ingestion.** Everything currently in `data/processed/` came from a one-time bulk data export, not the Strava API. OAuth, token refresh, and incremental "just pull what's new" ingestion don't exist — `fit_parser.py` and `backfill_activity_names.py` only ever process a static export folder on disk.
- **GitHub issue #1 is technically still open** even though the work described in it is complete (2,720 activities processed, all acceptance criteria met per the commit history) — housekeeping was missed, not a sign of unfinished work.

## Keeping things current

Two different update paths, depending on what changed:

- **New human context (an injury update, equipment change, etc.):** edit `data/context/life_events.json` directly (see `data/context/README.md`), run `python data/context/validate_life_events.py`, then just reload the dashboard — no other step needed.
- **New activities processed:** re-run the ingestion batch (see below), then regenerate the dashboard's chart data:
  ```
  python analysis/reports/build_dashboard_data.py
  ```

## Running the ingestion pipeline

```
python ingestion/strava/fit_parser.py \
    --input-dir "<path to the export's activities folder>" \
    --output-dir data/processed \
    --processed-dir "<path to a sibling 'processed' folder in the export>" \
    --tracks-dir data/processed_tracks \
    --log-dir data/logs \
    --summary-dir data/summaries \
    --activities-csv "<path to the export's activities.csv>" \
    --batch-size 200
```

`--processed-dir` lives inside the export location (not this repo) and acts as the record of what's already been ingested — entries move there on success, so re-running the same command only picks up what's left. Omit `--batch-size` to process everything remaining in one run.

To backfill activity names on already-processed data after the fact (normally unnecessary now that `fit_parser.py` captures names during processing):

```
python ingestion/strava/backfill_activity_names.py --activities-csv "<path to activities.csv>"
```

## Setup

```
pip install -r ingestion/strava/requirements.txt      # fitparse, jsonschema, pyarrow
pip install -r ingestion/strava/requirements-dev.txt   # + pytest, for running tests
pip install -r analysis/reports/requirements.txt       # duckdb, for the dashboard build script
```

Run the test suite from `ingestion/strava/`:

```
pytest
```

## Data Sources

| Source | Status |
|--------|--------|
| Strava (bulk export) | Ingested — 2,720 activities (2,628 Garmin, 92 Peloton) |
| Strava (live API) | Not started |
