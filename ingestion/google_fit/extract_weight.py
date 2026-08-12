"""
extract_weight.py

Parses body-weight readings out of a Google Fit Takeout export into the
standardized schema at data/schema/weight.schema.json, writing a single
consolidated data/weight/weight.json -- a small, human-scale dataset (tens
of readings, not thousands), so one file rather than the one-file-per-record
pattern used for the much larger activity dataset in data/processed/.

Reads Google Fit's *merged* weight datastream
(derived:com.google.weight:com.google.android.gms:merge_weight), which is
already the deduplicated union of every raw weight source (manual entries
in the Fit app, Hevy-synced readings, etc.) -- so that's the one file to
read here, not each raw source separately. In a Takeout export it lives at:

    Takeout/Fit/All Data/derived_com.google.weight_com.google.android.gms_merge_weight.json

Note: Google Takeout's web UI export sometimes silently drops most of the
"All Data" folder if the browser tab closes mid-extraction or the zip is
only partially unpacked -- if this file is missing from an export that
should contain it, check the original .zip directly before assuming the
export itself is incomplete:

    unzip -l takeout-*.zip | grep -i weight

Usage:
    python ingestion/google_fit/extract_weight.py \\
        --input-file "<path to Takeout>/Fit/All Data/derived_com.google.weight_com.google.android.gms_merge_weight.json"
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "weight" / "weight.json"

KG_TO_LB = 2.2046226218

# Maps Google Fit's originDataSourceId (which raw source a merged point came
# from) to this project's original_source enum. Add new entries here if a
# future export contains a source not seen before -- parse() fails loudly
# rather than silently mis-tagging or dropping an unrecognized source.
ORIGIN_MAP = {
    "raw:com.google.weight:com.google.android.apps.fitness:user_input": "google_fit_manual_entry",
    "raw:com.google.weight:com.hevy:health_platform": "hevy",
}


def parse(input_file: Path) -> list[dict]:
    with open(input_file, encoding="utf-8") as f:
        raw = json.load(f)

    records = []
    for point in raw["Data Points"]:
        origin_id = point.get("originDataSourceId", "")
        original_source = ORIGIN_MAP.get(origin_id)
        if original_source is None:
            raise ValueError(f"Unrecognized originDataSourceId {origin_id!r} -- add it to ORIGIN_MAP")

        recorded_at = datetime.fromtimestamp(int(point["startTimeNanos"]) / 1e9, tz=timezone.utc)
        weight_kg = point["fitValue"][0]["value"]["fpVal"]

        records.append({
            "id": str(point["startTimeNanos"]),
            "source": "google_fit",
            "original_source": original_source,
            "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
            "weight_kg": round(weight_kg, 2),
            "weight_lb": round(weight_kg * KG_TO_LB, 1),
            "ingested_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        })

    records.sort(key=lambda r: r["recorded_at"])
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-file", required=True, type=Path,
                         help="Path to derived_com.google.weight_..._merge_weight.json from a Google Fit Takeout export")
    parser.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT,
                         help=f"Where to write the standardized output (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    records = parse(args.input_file)

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)

    print(f"Wrote {len(records)} weight reading(s) to {args.output_file} "
          f"({records[0]['recorded_at'][:10]} to {records[-1]['recorded_at'][:10]}).")


if __name__ == "__main__":
    main()
