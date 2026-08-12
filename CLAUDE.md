# CLAUDE.md

Guidance for Claude Code (and any other agent) working in this repository.

## What this project is

A personal fitness-analysis pipeline: ingest activity data (Strava bulk export
today, Strava API later) and biometric data (Google Fit), process it into local
files, and render it as interactive dashboards. `README.md` is the human-facing
overview — read it once for orientation, but treat it as descriptive, not as a
spec.

The project is mid-restructure: moving from "hand-write a dashboard by prompting
Claude Code in a terminal" to an interactive local studio. **#7** is the design
rationale for that restructure — read it if an issue's "why" isn't clear from the
issue itself. **#27** is the live roadmap: phases, per-issue dependencies, and
checkboxes tracking what's actually done. Always check #27 before starting an
issue — an issue's stated dependency being *open* in #27 means don't assume its
output exists yet, even if this repo's current state suggests otherwise.

## Before starting an issue

1. The issue's **Acceptance Criteria** section is the definition of done. Build to
   that list, not to your own sense of reasonable scope — if something feels
   missing from the AC that seems clearly necessary, say so rather than silently
   adding scope.
2. Check the issue's stated dependencies against #27. If a dependency isn't
   checked off there, stop and flag it rather than building against a guess at
   what an unfinished upstream issue will produce.
3. Some Acceptance Criteria items are marked **(requires you)**. These need the
   repo owner directly — an OAuth consent flow, registering an API application, a
   second physical device to test network isolation, a final look at rendered
   pixels in an actual browser. Do not attempt to simulate, mock past, or claim
   completion of these. Implement everything else in the issue, then stop and
   report back the specific, concrete list of what you need the owner to do —
   exact steps, not "some manual setup is needed."
4. When every unmarked AC item is done and verified, check the issue's box on
   **#27**. Leave the GitHub issue itself open for the owner to close, unless
   they've told you otherwise for the session.

## Non-negotiable invariants

These recur across many issues. They were chosen deliberately — if one seems to
be getting in the way of an issue's task, flag it and ask rather than working
around it.

- **Dashboards must render under Live Server alone**, with the FastAPI server
  (#20) not running. The dashboard engine reads only static JSON files. If a
  change makes *viewing* a dashboard depend on the server being up, that's a
  regression — the server is only required to *change* things.
- **Acquire and Derive are separate operations**, never merged into one
  "refresh." Acquire talks to the network and writes to `data/processed*` /
  `data/raw/`. Derive is fully offline and only writes to `data/derived/` and
  `data/catalog/`. Keep them separately triggerable even when it would be
  simpler to chain them by default.
- **The FastAPI server binds `127.0.0.1` only.** Never change this to `0.0.0.0`
  without being asked explicitly and told why.
- **`data/derived/`, `data/catalog/`, and `analysis/dashboards/data/` are fully
  rebuildable and gitignored.** Don't commit their contents, and don't treat
  them as a source of truth for anything that isn't itself reconstructible from
  `data/processed*`, `data/measurements/`, and the dashboard definitions.
- **Secrets never enter git.** `.env` and `config.json` are gitignored. If a task
  needs a new secret or config value, add the *key* (not the value) to
  `config.example.json`, and read the real value from `.env` / `config.json` at
  runtime.

## Code conventions (established by the existing ingestion code — follow them for new code)

- **No top-level package.** Each module directory (`ingestion/strava/`,
  `ingestion/google_fit/`, `analysis/reports/` today; `analysis/core/`,
  `analysis/server/` as they're built) has its own `requirements.txt`, and its
  own `requirements-dev.txt` where it has tests. Don't introduce a root
  `pyproject.toml` or a single shared venv unless explicitly asked.
- **Tests are pytest**, in a `tests/` subdirectory of the module, named
  `test_<module>.py`. Where the module isn't an installed package (most of
  them), follow the pattern in `ingestion/strava/tests/conftest.py` — it adds
  the module's own directory to `sys.path` for the test session rather than
  requiring installation. Don't `pip install -e .` something that isn't set up
  to be installed.
- **JSON Schema validation is load-bearing, not decorative.** Any new data shape
  gets a schema under `data/schema/`, and writers validate against it before
  persisting — same pattern as `activity.schema.json` and the existing
  extractors.
- **DuckDB is the query engine**, pinned `>=1.0` (see `analysis/reports/
  requirements.txt`). Don't introduce a second query engine for new work in
  `analysis/core/`.
- **`data/sources/<id>/` (per-source sync state and manifests) is operational,
  reconstructible bookkeeping** — gitignore it alongside `data/derived/` and
  `data/catalog/`, for the same reason: it changes on every sync run and isn't
  needed for reproducibility.

## Translating Acceptance Criteria into tests

Most Acceptance Criteria checkboxes are written to be directly testable — treat
each one as a test spec, not a manual check-off. Where a checkbox says "verified
by X," write a test that does X and asserts on the result. Where fixture or
synthetic data is referenced and doesn't exist yet, creating it is part of the
issue's scope, not a separate task. Where a checkbox is explicitly manual (see
above), it stays manual — leave a short note (a doc comment, or a line in the
issue) describing exactly what was done and what the owner still needs to
verify, so the reasoning is visible even without an assertion behind it.

## Directory map

| Path | What it is | Canonical or derived? |
|---|---|---|
| `data/processed/`, `data/processed_tracks/` | Per-activity JSON + Parquet | Canonical |
| `data/measurements/` | Sparse biometric readings (was `data/weight/`) | Canonical |
| `data/context/life_events.json` | Hand-authored timeline | Canonical — fetched live by dashboards, never materialized |
| `data/raw/<source>/archive/` | Staged source payloads that passed validation | Canonical, retained for reprocessing |
| `data/raw/<source>/incoming/`, `quarantine/` | In-flight / rejected payloads | Transient — gitignored |
| `data/derived/` | Flattened/joined query surface (e.g. `activities.parquet`) | Rebuildable — gitignored |
| `data/catalog/` | Data catalog, semantic metrics layer, analysis index | Rebuildable — gitignored |
| `data/sources/<id>/` | Per-source sync state, manifests | Operational — gitignored |
| `analysis/core/` | Shared Python: catalog, metrics compiler, source registry, materializer | — |
| `analysis/server/` | FastAPI app | — |
| `analysis/dashboards/engine/` | Generic rendering engine (JS/HTML/CSS) | — |
| `analysis/dashboards/definitions/<id>/` | Dashboard versions + draft | Canonical |
| `analysis/dashboards/data/<id>/` | Materialized chart data per version | Rebuildable — gitignored |
| `analysis/studio/` | Studio + builder UI | — |
