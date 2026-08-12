"""
build_report_data.py

Regenerates analysis/reports/data/report_data.json -- the pre-aggregated
data behind post_treatment_report.html -- by re-running the DuckDB
queries against the current contents of data/processed/*.json and
activities.csv (strength-session dates), for the window since the
June 4, 2026 back treatment (see data/context/life_events.json).

Run this after processing a new batch of activities so the report
reflects the latest data:

    python analysis/reports/build_report_data.py

data/context/life_events.json is NOT part of this file -- the report
fetches it directly and live on every page load, so there's nothing to
regenerate for it.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROCESSED_GLOB = str(REPO_ROOT / "data" / "processed" / "*.json")
TRACKS_GLOB = str(REPO_ROOT / "data" / "processed_tracks" / "*.parquet")
ACTIVITIES_CSV = Path(r"H:\My Drive\David Personal\Athletics\export_3219872\activities.csv")
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "report_data.json"

WINDOW_START = date(2026, 6, 1)

# Trainer-led strength sessions ("Trainer Session N" / "Training Session N")
# get logged into Hevy after the fact -- their recorded duration is how long
# the after-the-fact entry took, not the session. See the
# "trainer-session-logging-quirk" entry in data/context/life_events.json.
TRAINER_LED_RE = re.compile(r"Trainer Session|Training Session", re.I)
TRAINER_LED_ASSUMED_MINUTES = 60  # 50 min training + 2x5 min stretch

# Peloton syncs one class as several separate activities (warm-up / main set /
# cool-down); back-to-back splits of the same ride land 10-120s apart, while
# unrelated activities are separated by hours -- so a gap-based cutoff cleanly
# tells them apart (verified against actual data for this window).
PELOTON_BUNDLE_GAP_S = 600

# A ride ending this close before a strength session's start is that
# session's warm-up rather than an unrelated same-day activity. Observed gaps
# split cleanly: 3-11 minutes for an actual pre-session warm-up ride vs.
# 88+ minutes when a ride and a strength session just happen to share a day.
PRE_STRENGTH_GAP_S = 20 * 60

WARMUP_NAME_RE = re.compile(r"warm.?up", re.I)
COOLDOWN_NAME_RE = re.compile(r"cool.?down", re.I)

# A main segment counts as high-intensity if either signal says so -- HR and
# power capture different aspects of effort (e.g. climbing/resistance formats
# like Rolling Hills push watts up without spiking HR the way sprint
# intervals do). Thresholds are derived from this window's own HIIT/Tabata
# vs. Low Impact clusters, which separate cleanly at these values.
HIGH_INTENSITY_HR = 128
HIGH_INTENSITY_WATTS = 150


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def load_strength_sessions(csv_path: Path) -> list[dict]:
    """Weight Training sessions since WINDOW_START, from activities.csv.

    These have no FIT/TCX payload of their own (Strava's activities.csv is
    the only record of them), so they can't be reached via
    read_json_auto('data/processed/*.json') like everything else here.
    """
    if not csv_path.exists():
        return []
    sessions = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("Activity Type") != "Weight Training":
                continue
            dt = datetime.strptime(row["Activity Date"], "%b %d, %Y, %I:%M:%S %p")
            if dt.date() < WINDOW_START:
                continue
            name = (row.get("Activity Name") or "").encode("ascii", "ignore").decode("ascii").strip()
            elapsed = float(row["Elapsed Time"]) if row.get("Elapsed Time") else None
            logged_minutes = round(elapsed / 60.0, 1) if elapsed else None
            trainer_led = bool(TRAINER_LED_RE.search(name))
            session = {
                "date": dt.date().isoformat(),
                "name": name,
                "minutes": TRAINER_LED_ASSUMED_MINUTES if trainer_led else logged_minutes,
                "_start": dt,
            }
            if trainer_led:
                session["logged_minutes"] = logged_minutes
                session["trainer_led"] = True
            sessions.append(session)
    sessions.sort(key=lambda s: s["date"])
    return sessions


def bundle_peloton_rows(rows: list[dict]) -> list[list[dict]]:
    """Group Peloton's separately-synced warm-up/main/cool-down splits of one
    ride back into a single logical session, based on the gap between them."""
    bundles: list[list[dict]] = []
    prev_end = None
    for r in rows:
        if prev_end is not None and (r["start"] - prev_end).total_seconds() <= PELOTON_BUNDLE_GAP_S:
            bundles[-1].append(r)
        else:
            bundles.append([r])
        prev_end = r["start"] + timedelta(seconds=r["elapsed_s"])
    return bundles


def build_peloton_sessions(rows: list[dict], strength_sessions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Bundle raw Peloton activities into rides, classify each ride's main
    segment by actual HR/power rather than its name, and tag rides that
    immediately precede a strength session as that session's warm-up.

    Returns (sessions, warmup_only) -- `warmup_only` holds orphan warm-up/
    cool-down splits with no main ride bundled to them (Peloton still synced
    them as their own activity), which are informative but not a "ride" in
    their own right, so they're kept out of the main sessions list.
    """
    strength_starts = sorted(s["_start"] for s in strength_sessions)

    def preceding_strength(bundle_end: datetime) -> datetime | None:
        for dt in strength_starts:
            gap = (dt - bundle_end).total_seconds()
            if 0 <= gap <= PRE_STRENGTH_GAP_S:
                return dt
        return None

    def mark_strength_warmup(dt: datetime, ride_name: str) -> None:
        for s in strength_sessions:
            if s["_start"] == dt:
                s["warmup_ride"] = ride_name
                return

    sessions, warmup_only = [], []
    for bundle in bundle_peloton_rows(rows):
        main = max(bundle, key=lambda r: r["elapsed_s"])
        bundle_end = max(r["start"] + timedelta(seconds=r["elapsed_s"]) for r in bundle)
        pre_strength_dt = preceding_strength(bundle_end)
        is_orphan_warmup = len(bundle) == 1 and (WARMUP_NAME_RE.search(main["name"]) or COOLDOWN_NAME_RE.search(main["name"]))

        if is_orphan_warmup:
            warmup_only.append({
                "date": main["start"].date().isoformat(),
                "name": main["name"],
                "hr": round(main["hr"], 1) if main["hr"] is not None else None,
                "minutes": round(main["elapsed_s"] / 60.0, 1),
                "pre_strength_for": pre_strength_dt.date().isoformat() if pre_strength_dt else None,
            })
            if pre_strength_dt:
                mark_strength_warmup(pre_strength_dt, main["name"])
            continue

        warmups = [r for r in bundle if r is not main and r["start"] < main["start"]]
        cooldowns = [r for r in bundle if r is not main and r["start"] > main["start"]]
        warmup_hrs = [r["hr"] for r in warmups if r["hr"] is not None]
        cooldown_hrs = [r["hr"] for r in cooldowns if r["hr"] is not None]
        warmup_hr = round(mean(warmup_hrs), 1) if warmup_hrs else None
        cooldown_hr = round(mean(cooldown_hrs), 1) if cooldown_hrs else None

        watts = (main["kj"] * 1000 / main["elapsed_s"]) if main["kj"] and main["elapsed_s"] else None
        high_intensity = bool(
            (main["hr"] is not None and main["hr"] >= HIGH_INTENSITY_HR)
            or (watts is not None and watts >= HIGH_INTENSITY_WATTS)
        )
        bundle_kj = sum(r["kj"] for r in bundle if r["kj"]) or None

        sessions.append({
            "date": main["start"].date().isoformat(),
            "name": main["name"],
            "hr": round(main["hr"], 1) if main["hr"] is not None else None,
            "max_hr": main["max_hr"],
            "minutes": round(main["elapsed_s"] / 60.0, 1),
            "total_minutes": round(sum(r["elapsed_s"] for r in bundle) / 60.0, 1),
            "watts": round(watts, 1) if watts is not None else None,
            "kj": round(bundle_kj, 1) if bundle_kj is not None else None,
            "calories": main["calories"],
            "high_intensity": high_intensity,
            "warmup_hr": warmup_hr,
            "cooldown_hr": cooldown_hr,
            "hr_recovery": round(main["hr"] - cooldown_hr, 1) if (main["hr"] is not None and cooldown_hr is not None) else None,
            "pre_strength_for": pre_strength_dt.date().isoformat() if pre_strength_dt else None,
        })
        if pre_strength_dt:
            mark_strength_warmup(pre_strength_dt, main["name"])

    return sessions, warmup_only


def main() -> None:
    con = duckdb.connect()
    activities = f"read_json_auto('{PROCESSED_GLOB}')"

    max_date = con.execute(f"SELECT max(start_time) FROM {activities}").fetchone()[0].date()

    weeks = []
    w = week_start(WINDOW_START)
    end_w = week_start(max_date)
    while w <= end_w:
        weeks.append(w)
        w += timedelta(days=7)
    week_keys = [w.isoformat() for w in weeks]
    week_labels = [f"{w.strftime('%b')} {w.day}" for w in weeks]

    outdoor_rows = con.execute(f"""
        SELECT date_trunc('week', start_time)::DATE AS wk,
               count(*) AS n, round(sum(elapsed_time_s)/60.0,1) AS min,
               round(sum(endurance_metrics.distance_m)/1000.0,1) AS km,
               round(avg(average_heart_rate),1) AS avg_hr,
               round(avg(endurance_metrics.average_cadence),1) as cadence,
               round(avg(endurance_metrics.average_speed_mps)*3.6,1) as speed_kmh
        FROM {activities}
        WHERE original_source != 'peloton' AND start_time >= '{WINDOW_START.isoformat()}'
        GROUP BY 1
    """).fetchall()
    outdoor_map = {r[0].isoformat(): r for r in outdoor_rows}

    peloton_rows = con.execute(f"""
        SELECT date_trunc('week', start_time)::DATE AS wk,
               count(*) AS n, round(sum(elapsed_time_s)/60.0,1) AS min,
               round(avg(average_heart_rate),1) AS avg_hr,
               round(sum(try_cast(class_metrics.output_kj AS DOUBLE)),1) AS kj
        FROM {activities}
        WHERE original_source = 'peloton' AND start_time >= '{WINDOW_START.isoformat()}'
        GROUP BY 1
    """).fetchall()
    peloton_map = {r[0].isoformat(): r for r in peloton_rows}

    strength_sessions = load_strength_sessions(ACTIVITIES_CSV)
    strength_weekly: dict[str, dict] = defaultdict(lambda: {"n": 0, "min": 0.0})
    for s in strength_sessions:
        wk = week_start(date.fromisoformat(s["date"])).isoformat()
        strength_weekly[wk]["n"] += 1
        strength_weekly[wk]["min"] += s["minutes"] or 0

    weekly = []
    for wk in week_keys:
        o = outdoor_map.get(wk)
        p = peloton_map.get(wk)
        s = strength_weekly.get(wk, {"n": 0, "min": 0.0})
        weekly.append({
            "week": wk,
            "outdoor_n": o[1] if o else 0, "outdoor_min": o[2] if o else 0, "outdoor_km": o[3] if o else 0,
            "outdoor_hr": o[4] if o else None, "outdoor_cadence": o[5] if o else None, "outdoor_speed": o[6] if o else None,
            "peloton_n": p[1] if p else 0, "peloton_min": p[2] if p else 0, "peloton_hr": p[3] if p else None, "peloton_kj": p[4] if p else 0,
            "strength_n": s["n"], "strength_min": round(s["min"], 1),
        })

    peloton_activity_rows = con.execute(f"""
        SELECT start_time, name, average_heart_rate, max_heart_rate, elapsed_time_s, calories,
               try_cast(class_metrics.output_kj AS DOUBLE)
        FROM {activities}
        WHERE original_source = 'peloton' AND start_time >= '{WINDOW_START.isoformat()}'
        ORDER BY start_time
    """).fetchall()
    peloton_raw = [{
        "start": r[0], "name": r[1].encode("ascii", "ignore").decode("ascii"),
        "hr": r[2], "max_hr": r[3], "elapsed_s": r[4], "calories": r[5], "kj": r[6],
    } for r in peloton_activity_rows]
    peloton_sessions, peloton_warmup_only = build_peloton_sessions(peloton_raw, strength_sessions)

    monthly = con.execute(f"""
        SELECT strftime(start_time, '%Y-%m') AS ym, original_source, count(*) AS n
        FROM {activities}
        WHERE start_time >= '{WINDOW_START.replace(month=1, day=1).isoformat()}'
        GROUP BY 1,2 ORDER BY 1
    """).fetchall()
    monthly_map: dict[str, dict] = {}
    for ym, src, n in monthly:
        e = monthly_map.setdefault(ym, {"outdoor": 0, "peloton": 0})
        e["peloton" if src == "peloton" else "outdoor"] += n
    monthly_series = [{"month": ym, **v} for ym, v in sorted(monthly_map.items())]

    outdoor_total = con.execute(f"""
        SELECT count(*), round(sum(elapsed_time_s)/60.0,0), round(sum(endurance_metrics.distance_m)/1000.0,0)
        FROM {activities} WHERE original_source != 'peloton' AND start_time >= '{WINDOW_START.isoformat()}'
    """).fetchone()
    peloton_total = con.execute(f"""
        SELECT count(*), round(sum(elapsed_time_s)/60.0,0)
        FROM {activities} WHERE original_source = 'peloton' AND start_time >= '{WINDOW_START.isoformat()}'
    """).fetchone()
    strength_total_n = len(strength_sessions)
    strength_total_min = round(sum(s["minutes"] or 0 for s in strength_sessions), 1)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_start": WINDOW_START.isoformat(),
        "data_through": max_date.isoformat(),
        "week_keys": week_keys,
        "week_labels": week_labels,
        "weekly": weekly,
        "peloton_sessions": peloton_sessions,
        "peloton_warmup_only": peloton_warmup_only,
        "strength_sessions": [{k: v for k, v in s.items() if not k.startswith("_")} for s in strength_sessions],
        "monthly_2026": monthly_series,
        "headline": {
            "outdoor_n": outdoor_total[0], "outdoor_min": outdoor_total[1], "outdoor_km": outdoor_total[2],
            "peloton_n": peloton_total[0], "peloton_min": peloton_total[1],
            "strength_n": strength_total_n, "strength_min": strength_total_min,
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {OUTPUT_PATH} -- {outdoor_total[0]} outdoor, {peloton_total[0]} Peloton, "
          f"{strength_total_n} strength sessions since {WINDOW_START.isoformat()}.")


if __name__ == "__main__":
    main()
