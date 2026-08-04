"""
fit_parser.py

Parses .fit files from a Strava bulk data export and converts them into the
standardized FitnessAnalysis JSON schema (see data/schema/activity.schema.json).

Strava's export contains activities originally recorded by different apps
(Garmin, Hevy, Peloton) and synced into Strava, so the FIT files vary widely
in what data they contain. This script maps whatever is present into a
common structure and leaves source-specific fields null when unavailable.

Usage:
    pip install fitparse
    python fit_parser.py --input-dir /path/to/fit_files --output-dir ../../data/processed

Accepts both raw .fit files and gzip-compressed .fit.gz files (Strava's
standard bulk export format uses .fit.gz).

Notes:
    - This is a first-pass stub. It has not yet been run against real data,
      since files live in the user's Google Drive and aren't accessible from
      this environment. Field mappings (especially original_source detection
      and strength/class metrics) will likely need refinement once run
      against actual files -- FIT field availability varies by manufacturer.
"""

import argparse
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from fitparse import FitFile

# Sport-type groupings used to decide which metrics block to populate.
# Extend these sets as real data reveals additional sport type strings.
ENDURANCE_TYPES = {
    "running", "cycling", "swimming", "hiking", "walking",
    "rowing", "nordic_skiing", "alpine_skiing", "elliptical",
}
STRENGTH_TYPES = {"training", "strength_training"}
CLASS_TYPES = {"fitness_equipment", "cardio_training"}  # common Peloton FIT sport values


def parse_fit_file(filepath: Path) -> dict:
    """
    Extract file_id, session-level, and record-level messages from a .fit
    file. Transparently handles gzip-compressed .fit.gz files, which is
    Strava's standard bulk export format.
    """
    if filepath.suffix == ".gz":
        with gzip.open(filepath, "rb") as f:
            fitfile = FitFile(f)
    else:
        fitfile = FitFile(str(filepath))

    file_id = {}
    for record in fitfile.get_messages("file_id"):
        for field in record:
            file_id[field.name] = field.value

    session = {}
    for record in fitfile.get_messages("session"):
        for field in record:
            session[field.name] = field.value

    return {"file_id": file_id, "session": session}


def guess_original_source(file_id: dict) -> str:
    """
    Best-effort guess at the original recording app/device based on FIT
    file_id manufacturer/product fields. Strava-synced files sometimes
    overwrite this with 'strava' as the manufacturer, in which case this
    will fall back to 'unknown' -- expect to refine this after inspecting
    real output.
    """
    manufacturer = str(file_id.get("manufacturer", "")).lower()
    if "garmin" in manufacturer:
        return "garmin"
    if "peloton" in manufacturer:
        return "peloton"
    if "hevy" in manufacturer:
        return "hevy"
    if manufacturer and manufacturer != "strava":
        return manufacturer  # capture it even if not in our known enum yet
    return "unknown"


def to_standard_schema(raw: dict, filepath: Path) -> dict:
    """Map raw FIT file_id/session data into the standardized activity schema."""
    session = raw["session"]
    file_id = raw["file_id"]
    sport = str(session.get("sport", "unknown")).lower()

    # filepath.stem only strips one suffix, so "12345.fit.gz" -> "12345.fit".
    # Strip both to get a clean id.
    activity_id = filepath.name
    for suffix in (".gz", ".fit"):
        if activity_id.endswith(suffix):
            activity_id = activity_id[: -len(suffix)]

    start_time = session.get("start_time")
    activity = {
        "id": activity_id,
        "source": "strava",
        "original_source": guess_original_source(file_id),
        "activity_type": session.get("sport", "unknown"),
        "name": None,  # Strava activity names aren't stored in the FIT file itself
        "start_time": start_time.isoformat() if hasattr(start_time, "isoformat") else start_time,
        "timezone": None,
        "elapsed_time_s": session.get("total_elapsed_time"),
        "moving_time_s": session.get("total_timer_time"),
        "calories": session.get("total_calories"),
        "average_heart_rate": session.get("avg_heart_rate"),
        "max_heart_rate": session.get("max_heart_rate"),
        "device_name": file_id.get("product"),
        "endurance_metrics": None,
        "strength_metrics": None,
        "class_metrics": None,
        "raw_file_reference": filepath.name,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

    if sport in ENDURANCE_TYPES:
        activity["endurance_metrics"] = {
            "distance_m": session.get("total_distance"),
            "average_speed_mps": session.get("avg_speed"),
            "max_speed_mps": session.get("max_speed"),
            "elevation_gain_m": session.get("total_ascent"),
            "average_cadence": session.get("avg_cadence"),
            "average_power_w": session.get("avg_power"),
            "max_power_w": session.get("max_power"),
            "gps_track_available": session.get("total_distance") is not None,
        }
    elif sport in STRENGTH_TYPES:
        activity["strength_metrics"] = {
            "exercises": []  # Per-set detail typically isn't in Strava's FIT
                              # export for synced strength activities; revisit
                              # if a direct Hevy export is added as a source.
        }
    elif sport in CLASS_TYPES:
        activity["class_metrics"] = {
            "instructor": None,
            "class_title": None,
            "class_type": session.get("sub_sport"),
            "output_kj": session.get("total_work"),
        }

    return activity


def main():
    parser = argparse.ArgumentParser(description="Parse .fit files into standardized JSON.")
    parser.add_argument("--input-dir", required=True, help="Directory containing .fit files")
    parser.add_argument("--output-dir", required=True, help="Directory to write standardized JSON")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fit_files = sorted(list(input_dir.glob("*.fit")) + list(input_dir.glob("*.fit.gz")))
    print(f"Found {len(fit_files)} .fit/.fit.gz files in {input_dir}")

    errors = []
    for i, filepath in enumerate(fit_files, 1):
        try:
            raw = parse_fit_file(filepath)
            activity = to_standard_schema(raw, filepath)
            out_path = output_dir / f"{activity['id']}.json"
            with open(out_path, "w") as f:
                json.dump(activity, f, indent=2, default=str)
        except Exception as e:
            errors.append((filepath.name, str(e)))

        if i % 100 == 0:
            print(f"  ...{i}/{len(fit_files)} processed")

    print(f"\nDone. {len(fit_files) - len(errors)} succeeded, {len(errors)} failed.")
    if errors:
        print("\nFailures (first 20):")
        for name, err in errors[:20]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
