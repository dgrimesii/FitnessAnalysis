# FitnessAnalysis

Collects fitness data from apps like Strava, structures it into a standardized repository, and provides tools for analysis — including pre-defined reports and AI-driven insights.

## Overview

FitnessAnalysis is organized around three core functions:

1. **Collect** — pull raw activity data from external fitness platforms
2. **Structure** — normalize and store that data in a consistent, analysis-ready format
3. **Analyze** — explore the data through pre-defined reports and AI-assisted analysis

## Project Structure

```
FitnessAnalysis/
├── ingestion/       # Data collection from external sources
├── data/            # Standardized/processed data storage
├── analysis/        # Analysis tools and reports
└── README.md
```

### `ingestion/`
Handles authentication and data pulls from external fitness platforms.

- **`strava/`** — OAuth 2.0 flow, token refresh handling, and API calls to pull activities from Strava. First supported data source.
- *(Future sources — e.g. Garmin, Apple Health — will follow the same pattern as separate submodules.)*

### `data/`
Defines and stores the standardized schema that all sources get mapped into, regardless of origin platform. Keeping this layer source-agnostic means analysis tools only ever need to work against one consistent format.

- **`schema/`** — standardized data models (activities, metrics, athlete info)
- **`processed/`** — cleaned, structured data ready for analysis
- **`processed_tracks/`** — per-point time series (GPS, heart rate, cadence, power) as Parquet, one file per activity
- **`raw/`** — unmodified data pulled directly from source APIs (kept for auditability/reprocessing)
- **`context/`** — human-provided context (injuries, life events, equipment/training changes) that explains patterns the device data alone can't — see `data/context/README.md`

### `analysis/`
Tools for exploring and interpreting the structured fitness data.

- **`reports/`** — pre-defined analysis (trends, summaries, performance metrics)
- **`ai/`** — AI-assisted analysis and natural-language interaction with the dataset

## Data Sources

| Source | Status |
|--------|--------|
| Strava | In progress |

## Setup

*(To be added: environment variables, dependencies, first-run instructions)*
