"""
backfill_activity_names.py

One-time backfill (see issue #5): populates the `name` field for
already-processed activities in data/processed/*.json, which has always
been null -- neither FIT's session message nor TCX's Lap block carries an
activity title. Strava's own activities.csv has a real Activity Name for
every entry, keyed by the same filename already stored as
raw_file_reference in each standardized record, so no new ID-derivation
logic is needed: just a filename join.

Confirmed against real data before writing this: all 2,720 committed
data/processed/*.json files have name: null (checked every file directly),
and activities.csv has a non-blank Activity Name for FIT-, TCX-, and
GPX-backed rows alike -- including the two GPX-backed rows whose own
payload file has no data in it at all.

This is a one-time backfill for records that already exist. Going forward,
new batches should capture the name at parse time instead of needing this
run again -- see issue #6.

Usage:
    python backfill_activity_names.py \\
        --activities-csv "H:\\My Drive\\David Personal\\Athletics\\export_3219872\\activities.csv" \\
        --processed-dir ../../data/processed
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import jsonschema

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "data" / "schema" / "activity.schema.json"


def load_activity_names(csv_path: Path) -> dict[str, str]:
    """
    Build a filename -> Activity Name lookup from Strava's activities.csv,
    keyed by the basename of the Filename column (e.g. "1001254666.fit.gz"),
    matching what's already stored as raw_file_reference in every
    standardized activity record. Rows with a blank Activity Name are
    skipped -- callers should treat "no entry in this dict" the same as
    "no name available", not backfill an empty string.
    """
    names = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filename = row.get("Filename", "")
            if not filename:
                continue
            basename = filename.rsplit("/", 1)[-1]
            activity_name = (row.get("Activity Name") or "").strip()
            if activity_name:
                names[basename] = activity_name
    return names


def backfill(processed_dir: Path, names: dict[str, str], schema: dict) -> dict[str, int]:
    """
    Update `name` in every data/processed/*.json that currently has none
    and has a match in `names`, re-validating each updated record against
    `schema` before writing -- so a backfill can never silently produce a
    schema-invalid file, the same guarantee fit_parser.py's own
    validate_against_schema() gives every record at parse time.

    Records that already have a name (e.g. from a future run of #6) are
    left untouched, not overwritten.
    """
    counts = {"updated": 0, "already_named": 0, "no_match": 0}
    for fp in sorted(processed_dir.glob("*.json")):
        activity = json.loads(fp.read_text(encoding="utf-8"))
        if activity.get("name"):
            counts["already_named"] += 1
            continue
        name = names.get(activity.get("raw_file_reference", ""))
        if not name:
            counts["no_match"] += 1
            continue
        activity["name"] = name
        jsonschema.validate(instance=activity, schema=schema)
        fp.write_text(json.dumps(activity, indent=2, default=str), encoding="utf-8")
        counts["updated"] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Backfill the `name` field in data/processed/*.json from Strava's activities.csv."
    )
    parser.add_argument("--activities-csv", required=True, help="Path to Strava's activities.csv")
    parser.add_argument("--processed-dir", default="data/processed",
                         help="Directory of standardized JSON files to update (default: data/processed)")
    parser.add_argument("--schema-path", default=str(DEFAULT_SCHEMA_PATH),
                         help="Path to the activity JSON schema, used to validate every updated record "
                         "(default: data/schema/activity.schema.json in this repo)")
    args = parser.parse_args()

    names = load_activity_names(Path(args.activities_csv))
    print(f"Loaded {len(names)} named activities from {args.activities_csv}")

    with open(args.schema_path, encoding="utf-8") as f:
        schema = json.load(f)

    counts = backfill(Path(args.processed_dir), names, schema)
    print(f"Updated: {counts['updated']}")
    print(f"Already had a name: {counts['already_named']}")
    print(f"No match in activities.csv: {counts['no_match']}")


if __name__ == "__main__":
    main()
