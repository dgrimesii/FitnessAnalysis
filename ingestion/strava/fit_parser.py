"""
fit_parser.py

Walks a directory of Strava activity export entries, parses each one into the
standardized FitnessAnalysis JSON schema (see data/schema/activity.schema.json),
and moves successfully processed entries out of the input directory into a
"processed" directory.

Each top-level entry under --input-dir can be either:
  - a single .fit or .fit.gz file, or
  - a folder containing exactly one .fit/.fit.gz file (an "archive folder")
Either shape is handled automatically.

Because completed entries are moved out of --input-dir on success, re-running
this script only ever sees remaining/unprocessed entries -- so processing a
large export in batches (via --batch-size) is naturally resumable. Entries
that fail to parse are left in place (not moved), so they surface on every
run until fixed, and never silently disappear.

Strava's export contains activities originally recorded by different apps
(Garmin, Hevy, Peloton) and synced into Strava, so the underlying FIT data
varies widely in what it contains. This script maps whatever is present into
a common structure and leaves source-specific fields null when unavailable.

Usage:
    pip install fitparse
    python fit_parser.py \\
        --input-dir "H:\\My Drive\\David Personal\\Athletics\\export_3219872\\activities" \\
        --output-dir ../../data/processed \\
        --processed-dir "H:\\My Drive\\David Personal\\Athletics\\export_3219872\\processed" \\
        --batch-size 200

Notes:
    - This is a first-pass stub. It has not yet been run against real data,
      since files live in the user's Google Drive and aren't accessible from
      this environment. Field mappings (especially original_source detection
      and strength/class metrics), as well as the archive-folder-vs-flat-file
      assumption above, will likely need refinement once run against actual
      data -- verify against a small batch first.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
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


def find_fit_payload(entry: Path) -> Path | None:
    """
    Given a top-level activity entry under --input-dir, locate the actual
    .fit/.fit.gz file to parse.

    Handles two possible export shapes:
      - entry is itself a .fit/.fit.gz file
      - entry is a folder ("archive folder") containing one .fit/.fit.gz file
    Returns None if no payload is found (caller treats this as a failure,
    entry stays in place for inspection).
    """
    if entry.is_file():
        if entry.name.endswith(".fit") or entry.name.endswith(".fit.gz"):
            return entry
        return None
    if entry.is_dir():
        candidates = sorted(list(entry.rglob("*.fit")) + list(entry.rglob("*.fit.gz")))
        if not candidates:
            return None
        # Assumes one FIT payload per archive folder. If real data has
        # multiple, this picks the first alphabetically -- revisit after
        # inspecting actual folder contents.
        return candidates[0]
    return None


def derive_activity_id(entry: Path) -> str:
    """Derive a clean activity id from the top-level entry name (file or folder)."""
    name = entry.name
    for suffix in (".gz", ".fit"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


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


def to_standard_schema(raw: dict, entry: Path, fit_path: Path) -> dict:
    """Map raw FIT file_id/session data into the standardized activity schema."""
    session = raw["session"]
    file_id = raw["file_id"]
    sport = str(session.get("sport", "unknown")).lower()

    start_time = session.get("start_time")
    activity = {
        "id": derive_activity_id(entry),
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
        "raw_file_reference": fit_path.name,
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
    parser = argparse.ArgumentParser(
        description="Walk a directory of Strava activity entries, parse each into "
        "standardized JSON, and move completed entries to a processed folder."
    )
    parser.add_argument("--input-dir", required=True,
                         help="Directory containing activity entries (files or archive folders)")
    parser.add_argument("--output-dir", required=True,
                         help="Directory to write standardized JSON (one file per activity)")
    parser.add_argument("--processed-dir", required=True,
                         help="Directory to move successfully processed entries into")
    parser.add_argument("--batch-size", type=int, default=None,
                         help="Max number of entries to process this run. "
                         "Omit to process all remaining entries in --input-dir.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    processed_dir = Path(args.processed_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    # Only look at top-level entries still sitting in input_dir. Anything
    # already moved to processed_dir from a prior run won't reappear here,
    # which is what makes batching resumable across multiple invocations.
    all_entries = sorted(p for p in input_dir.iterdir() if p.name != ".gitkeep")
    total_remaining = len(all_entries)

    batch = all_entries[: args.batch_size] if args.batch_size else all_entries
    print(f"{total_remaining} entries remaining in {input_dir}")
    print(f"Processing {len(batch)} this run"
          + (f" (batch-size={args.batch_size})" if args.batch_size else " (no batch limit)"))

    succeeded = 0
    errors = []
    for i, entry in enumerate(batch, 1):
        try:
            fit_path = find_fit_payload(entry)
            if fit_path is None:
                raise ValueError("no .fit/.fit.gz payload found in this entry")

            raw = parse_fit_file(fit_path)
            activity = to_standard_schema(raw, entry, fit_path)

            out_path = output_dir / f"{activity['id']}.json"
            with open(out_path, "w") as f:
                json.dump(activity, f, indent=2, default=str)

            # Only move the source entry to processed/ after the JSON write
            # above succeeds, so a crash mid-write can't lose an activity
            # (it just stays in input_dir and gets retried next run).
            shutil.move(str(entry), str(processed_dir / entry.name))
            succeeded += 1
        except Exception as e:
            # Leave failed entries in place (not moved) so they're retried
            # or inspected on the next run rather than silently skipped.
            errors.append((entry.name, str(e)))

        if i % 100 == 0:
            print(f"  ...{i}/{len(batch)} this batch")

    remaining_after = total_remaining - succeeded
    print(f"\nBatch done. {succeeded} succeeded and moved to {processed_dir}, "
          f"{len(errors)} failed and left in place.")
    print(f"{remaining_after} entries remain in {input_dir} "
          f"(including {len(errors)} failures to resolve).")
    if errors:
        print("\nFailures (first 20):")
        for name, err in errors[:20]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
