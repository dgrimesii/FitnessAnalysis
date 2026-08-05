"""
build_dashboard_data.py

Regenerates analysis/reports/data/dashboard_data.json -- the pre-aggregated
chart data behind dashboard.html -- by re-running the same DuckDB queries
used to build the "Eleven Years, Two Very Different Riders" analysis
against the current contents of data/processed/*.json and
data/processed_tracks/*.parquet.

Run this after processing a new batch of activities so the dashboard
reflects the latest data:

    python analysis/reports/build_dashboard_data.py

data/context/life_events.json is NOT part of this file -- dashboard.html
fetches it directly and live on every page load, so there's nothing to
regenerate for it.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROCESSED_GLOB = str(REPO_ROOT / "data" / "processed" / "*.json")
TRACKS_GLOB = str(REPO_ROOT / "data" / "processed_tracks" / "*.parquet")
ACTIVITIES_CSV = Path(r"H:\My Drive\David Personal\Athletics\export_3219872\activities.csv")
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "dashboard_data.json"


def load_strength_sessions(csv_path: Path) -> list[str]:
    """Returns ISO dates (YYYY-MM-DD) of Weight Training activities.

    Weight Training rows have no FIT/TCX payload of their own (Strava's
    activities.csv is the only record of them), so they can't be reached
    via read_json_auto('data/processed/*.json') like everything else here.
    """
    if not csv_path.exists():
        return []
    dates = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("Activity Type") == "Weight Training":
                # "Activity Date" looks like "Jul 5, 2026, 6:15:00 AM"
                from datetime import datetime

                dt = datetime.strptime(row["Activity Date"], "%b %d, %Y, %I:%M:%S %p")
                dates.append(dt.date().isoformat())
    return dates


def main() -> None:
    con = duckdb.connect()

    activities = f"read_json_auto('{PROCESSED_GLOB}')"
    tracks = f"read_parquet('{TRACKS_GLOB}')"

    # ---- Header stats ----
    stats = con.execute(f"""
        SELECT
            count(*) FILTER (WHERE original_source != 'peloton') AS outdoor_rides,
            count(*) FILTER (WHERE original_source = 'peloton') AS peloton_rides,
            round(sum(endurance_metrics.distance_m) / 1000.0, 0) AS total_km,
            round(sum(elapsed_time_s) / 3600.0, 0) AS total_hours,
            round(sum(coalesce(endurance_metrics.elevation_gain_m, 0)), 0) AS total_elevation_m,
            round(sum(coalesce(calories, 0)), 0) AS total_calories,
            min(start_time) AS earliest,
            max(start_time) AS latest
        FROM {activities}
    """).fetchone()
    (
        outdoor_rides, peloton_rides, total_km, total_hours,
        total_elevation_m, total_calories, earliest, latest,
    ) = stats

    # ---- Rides + longest ride + avg HR by year (outdoor only) ----
    by_year = con.execute(f"""
        SELECT
            year(start_time) AS yr,
            count(*) AS n,
            round(sum(endurance_metrics.distance_m) / 1000.0, 1) AS total_km,
            round(max(endurance_metrics.distance_m) / 1000.0, 1) AS longest_km,
            round(avg(average_heart_rate), 1) AS avg_hr
        FROM {activities}
        WHERE original_source != 'peloton'
        GROUP BY 1 ORDER BY 1
    """).fetchall()

    # ---- Cadence / speed by year, from real per-second track data ----
    # Both filtered to > 0 -- otherwise stopped-at-a-light points (speed/cadence
    # near zero) drag the "riding pace" average down toward "time spent stopped".
    cadence_speed_by_year = con.execute(f"""
        SELECT
            year(t.timestamp) AS yr,
            round(avg(t.cadence) FILTER (WHERE t.cadence > 0), 1) AS avg_cadence,
            round(avg(t.speed_mps) FILTER (WHERE t.speed_mps > 0) * 3.6, 1) AS avg_speed_kmh
        FROM {tracks} t
        JOIN {activities} a ON a.id = t.activity_id
        WHERE a.original_source != 'peloton'
        GROUP BY 1 ORDER BY 1
    """).fetchall()

    # ---- Rides by local hour of day (UTC-5, no stored offset), zero-filled
    # for hours with no rides so the x-axis stays a continuous 0-23 ----
    hour_of_day_raw = dict(con.execute(f"""
        SELECT
            (hour(start_time) + 24 - 5) % 24 AS local_hour,
            count(*) AS n
        FROM {activities}
        WHERE original_source != 'peloton'
        GROUP BY 1 ORDER BY 1
    """).fetchall())
    hour_of_day = [[h, hour_of_day_raw.get(h, 0)] for h in range(24)]

    # ---- Peloton rides by year (zero-filled -- GROUP BY only emits years with
    # at least one ride, but the chart needs to show the multi-year gap) ----
    peloton_by_year_raw = dict(con.execute(f"""
        SELECT year(start_time) AS yr, count(*) AS n
        FROM {activities}
        WHERE original_source = 'peloton'
        GROUP BY 1 ORDER BY 1
    """).fetchall())
    overall_max_year = con.execute(f"SELECT max(year(start_time)) FROM {activities}").fetchone()[0]
    first_peloton_year = min(peloton_by_year_raw) if peloton_by_year_raw else overall_max_year
    peloton_by_year = [
        [yr, peloton_by_year_raw.get(yr, 0)]
        for yr in range(first_peloton_year, overall_max_year + 1)
    ]

    # ---- Rides per month, last 24 months (zero-filled) ----
    monthly_last24_raw = dict(con.execute(f"""
        SELECT strftime(start_time, '%Y-%m') AS ym, count(*) AS n
        FROM {activities}
        WHERE original_source != 'peloton'
          AND start_time >= (SELECT max(start_time) FROM {activities}) - INTERVAL 24 MONTH
        GROUP BY 1 ORDER BY 1
    """).fetchall())
    latest_ym_date = date.fromisoformat(str(latest)[:10]).replace(day=1)
    monthly_last24 = []
    y, m = latest_ym_date.year, latest_ym_date.month
    for _ in range(24):
        ym = f"{y:04d}-{m:02d}"
        monthly_last24.append([ym, monthly_last24_raw.get(ym, 0)])
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    monthly_last24.reverse()

    # ---- Recovery: Peloton sessions per week, June 2026 onward ----
    peloton_weekly = con.execute(f"""
        SELECT strftime(date_trunc('week', start_time), '%m-%d') AS wk, count(*) AS n
        FROM {activities}
        WHERE original_source = 'peloton' AND start_time >= DATE '2026-06-01'
        GROUP BY 1 ORDER BY 1
    """).fetchall()

    strength_dates = load_strength_sessions(ACTIVITIES_CSV)
    strength_weekly: dict[str, int] = defaultdict(int)
    for d in strength_dates:
        row = con.execute("SELECT strftime(date_trunc('week', ?::DATE), '%m-%d')", [d]).fetchone()
        strength_weekly[row[0]] += 1

    # Zero-fill every week from the injection date through the most recent
    # data -- a GROUP BY would silently drop weeks with no sessions at all,
    # which is exactly the shape ("did it restart, and hold?") this chart
    # needs to show.
    start_monday = date(2026, 6, 1)
    latest_date = date.fromisoformat(str(latest)[:10])
    end_monday = latest_date - timedelta(days=latest_date.weekday())
    recovery_weeks = []
    d = start_monday
    while d <= end_monday:
        recovery_weeks.append(d.strftime("%m-%d"))
        d += timedelta(days=7)

    peloton_map = dict(peloton_weekly)
    recovery_peloton = [peloton_map.get(w, 0) for w in recovery_weeks]
    recovery_strength = [strength_weekly.get(w, 0) for w in recovery_weeks]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_from": {
            "processed_glob": "data/processed/*.json",
            "tracks_glob": "data/processed_tracks/*.parquet",
            "activities_csv": "activities.csv (strength-training sessions only)",
        },
        "stats": {
            "totalRides": outdoor_rides + peloton_rides,
            "outdoorRides": outdoor_rides,
            "pelotonRides": peloton_rides,
            "totalKm": total_km,
            "totalMi": round(total_km * 0.621371, 0),
            "totalHours": total_hours,
            "totalDays": round(total_hours / 24, 1),
            "totalElevationM": total_elevation_m,
            "totalCalories": total_calories,
            "earliest": str(earliest)[:10],
            "latest": str(latest)[:10],
        },
        "byYear": [list(r) for r in by_year],
        "cadenceSpeedByYear": [list(r) for r in cadence_speed_by_year],
        "hourOfDay": [list(r) for r in hour_of_day],
        "pelotonByYear": [list(r) for r in peloton_by_year],
        "monthlyLast24": [list(r) for r in monthly_last24],
        "recoveryWeeks": recovery_weeks,
        "recoveryPeloton": recovery_peloton,
        "recoveryStrength": recovery_strength,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {OUTPUT_PATH} -- {outdoor_rides + peloton_rides} activities "
          f"({outdoor_rides} outdoor, {peloton_rides} Peloton), "
          f"{len(strength_dates)} strength sessions.")


if __name__ == "__main__":
    main()
