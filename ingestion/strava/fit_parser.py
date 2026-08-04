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

Every standardized activity is validated against data/schema/activity.schema.json
(--schema-path) *before* it's written to --output-dir. A record that fails
validation is treated exactly like any other parse failure: nothing is
written for it, the source entry stays in --input-dir, and the schema
validation error is logged -- the same path a corrupt FIT file would take.
This is what would have caught, automatically, the two real schema
mismatches found by manually re-checking the first batch's output (see
"Validated against real data" below) instead of needing a manual check.

Every run is a "batch" and gets a UTC-timestamp batch_id. Each batch writes
two paired artifacts, cross-referenced by that shared batch_id:
  - an error log (--log-dir, default data/logs/) with one line per failed or
    skipped entry, for debugging
  - a summary markdown (--summary-dir, default data/summaries/) with
    attempted/succeeded/failed/skipped counts, generated after every run
    (including runs that are interrupted partway through)

Entries this script cannot yet handle -- a pre-existing, non-activity folder
sitting in --input-dir, or a recognized-but-unsupported file type
(.tcx/.tcx.gz, .gpx -- see follow-up issues #2 and #3) -- are *skipped*, not
treated as failures: nothing is wrong with them, this parser just doesn't
process that shape/format yet. Skips and failures are both left in place in
--input-dir and both show up in the batch log/summary, but are counted and
reported separately so a skip doesn't read as something broken.

Usage:
    pip install -r requirements.txt
    python fit_parser.py \\
        --input-dir "H:\\My Drive\\David Personal\\Athletics\\export_3219872\\activities" \\
        --output-dir ../../data/processed \\
        --processed-dir "H:\\My Drive\\David Personal\\Athletics\\export_3219872\\processed" \\
        --log-dir ../../data/logs \\
        --summary-dir ../../data/summaries \\
        --batch-size 200

Validated against real data:
    A random 60-file sample of real .fit.gz entries from the actual export
    (see issue #1) confirmed the flat-file entry shape, 100% garmin/cycling
    manufacturer+sport combination, and surfaced two real bugs that are now
    fixed here:
      - parse_fit_file() was reading messages from a gzip stream *after*
        closing it (fitparse reads lazily via get_messages(), so this always
        raised "I/O operation on closed file" for every .fit.gz entry).
      - device_name was read from file_id["product"], but fitparse stores
        Garmin's product field under "garmin_product", not "product" --
        device_name was silently always None.
    guess_original_source() and the ENDURANCE_TYPES/STRENGTH_TYPES/
    CLASS_TYPES sport-string groupings matched real data as-is and did not
    need changes for the .fit.gz population (Hevy and Peloton activities do
    not appear in this export's FIT data at all -- see issues #2 and #3).

    A real first batch of 2 files (run end-to-end against the actual H:
    drive export, not synthetic data) then surfaced two schema mismatches
    that the parser itself didn't catch at the time -- only a manual
    jsonschema check afterward did:
      - elapsed_time_s/moving_time_s were typed integer-only in the schema,
        but real Garmin session data reports fractional seconds (e.g.
        780.532) -- schema widened to "number".
      - device_name leaked a raw int (3122) for a garmin_product code
        fitparse didn't recognize well enough to decode to a name --
        to_standard_schema() now stringifies it.
    Both are fixed, and validate_against_schema() below now runs on every
    record before it's written, so a future mismatch like this fails that
    one entry (logged, left in --input-dir) instead of writing invalid JSON
    silently.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from fitparse import FitFile

# Default location of the activity JSON schema, resolved relative to this
# file rather than the current working directory so it's correct regardless
# of where the script is invoked from (unlike --input-dir/--output-dir/etc.,
# which are explicit user-supplied paths).
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "data" / "schema" / "activity.schema.json"

# Sport-type groupings used to decide which metrics block to populate.
# Extend these sets as real data reveals additional sport type strings.
# Validated against a random 60-file sample of real .fit.gz entries: 100%
# were garmin/cycling, which "cycling" already covers below.
ENDURANCE_TYPES = {
    "running", "cycling", "swimming", "hiking", "walking",
    "rowing", "nordic_skiing", "alpine_skiing", "elliptical",
}
STRENGTH_TYPES = {"training", "strength_training"}
CLASS_TYPES = {"fitness_equipment", "cardio_training"}  # common Peloton FIT sport values

# File types this script actively parses.
SUPPORTED_EXTENSIONS = (".fit", ".fit.gz")

# File types confirmed present in the real export (see issue #1's inspection)
# that this script deliberately does not parse yet. These are tracked as
# separate follow-up issues rather than being treated as parse failures here:
#   - .tcx/.tcx.gz: all 92 real samples are Peloton rides -- issue #2
#   - .gpx: 8 real samples (2 with no track data at all) -- issue #3
KNOWN_UNSUPPORTED_EXTENSIONS = (".tcx.gz", ".tcx", ".gpx")


def _matches_any_suffix(name: str, suffixes: tuple[str, ...]) -> bool:
    """Case-insensitive suffix match against any of `suffixes`."""
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in suffixes)


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
        if _matches_any_suffix(entry.name, SUPPORTED_EXTENSIONS):
            return entry
        return None
    if entry.is_dir():
        candidates = sorted(list(entry.rglob("*.fit")) + list(entry.rglob("*.fit.gz")))
        if not candidates:
            return None
        # Assumes one FIT payload per archive folder. If real data has
        # multiple, this picks the first alphabetically -- revisit after
        # inspecting actual folder contents. No archive-folder-shaped
        # entries were observed in the real export (all 2,729 top-level
        # entries were flat files), so this branch is currently untested
        # against real data.
        return candidates[0]
    return None


def classify_skip_reason(entry: Path) -> str | None:
    """
    Decide whether `entry` should be skipped outright rather than parsed or
    reported as a failure. Returns a human-readable reason, or None if
    `entry` should proceed to find_fit_payload()/parsing as normal.

    Two cases confirmed from real data (see issue #1):
      - A directory containing no .fit/.fit.gz payload at all. Real example:
        a pre-existing, empty `Processed/` folder was found sitting directly
        inside the real export's activities/ directory -- a leftover Strava
        export artifact, not something this script created, and unrelated
        to --processed-dir (a sibling of activities/, not nested in it).
      - A file with a known-but-unsupported extension (.tcx/.tcx.gz/.gpx) --
        present in the real export but handled by separate follow-up issues
        (#2, #3), not this script.

    Anything else (an unrecognized file type, a directory with unexpected
    contents) falls through to normal parsing, where find_fit_payload()
    returning None becomes a genuine failure -- those cases are worth
    surfacing for investigation rather than silently skipping.
    """
    if entry.is_dir():
        if find_fit_payload(entry) is None:
            return "directory contains no .fit/.fit.gz payload (not an activity archive)"
        return None
    if entry.is_file() and _matches_any_suffix(entry.name, KNOWN_UNSUPPORTED_EXTENSIONS):
        return "unsupported file type -- not yet handled by this script (see issues #2/#3)"
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

    The gzip stream is read fully into memory before FitFile touches it.
    fitparse reads lazily -- get_messages() below only actually consumes the
    underlying stream when called, not when FitFile() is constructed -- so
    constructing FitFile from a `with gzip.open(...) as f` handle and then
    calling get_messages() after that `with` block exits fails with
    "I/O operation on closed file". Reading the bytes up front avoids this
    and confirmed fixed against real .fit.gz files from the export.
    """
    if filepath.suffix == ".gz":
        with gzip.open(filepath, "rb") as f:
            fit_bytes = f.read()
        fitfile = FitFile(fit_bytes)
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
    will fall back to 'unknown'.

    Validated against a random 60-file sample of real .fit.gz entries:
    manufacturer was 'garmin' in 100% of samples (never overwritten to
    'strava' for this export), so the garmin branch below is exercised and
    correct as-is.
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
    # fitparse only decodes garmin_product to a friendly string (e.g.
    # "edge810") for device IDs it recognizes; unrecognized ones come
    # through as a raw int (e.g. 3122 for a newer Edge model), confirmed
    # against real file_id data. device_name is a string field in the
    # schema, so cast it -- this keeps a numeric device id around (better
    # than dropping it) without leaking an int into a string field.
    device_name_raw = file_id.get("garmin_product") or file_id.get("product")
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
        # fitparse expands manufacturer-specific fields; Garmin's device
        # name/model lands under "garmin_product", not the generic
        # "product" key. Confirmed against real file_id dicts, which never
        # contained a "product" key at all -- device_name was silently
        # always None before this fix. Fall back to "product" in case a
        # non-Garmin manufacturer uses the generic key.
        "device_name": str(device_name_raw) if device_name_raw is not None else None,
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


def load_activity_schema(schema_path: Path) -> dict:
    """Load the activity JSON schema used to validate every record before it's written."""
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


def validate_against_schema(activity: dict, schema: dict) -> None:
    """
    Validate a standardized activity dict against data/schema/activity.schema.json
    before it's written to --output-dir.

    Raises jsonschema.exceptions.ValidationError (with a message naming the
    offending field, e.g. "780.532 is not of type 'integer', 'null'") if
    `activity` doesn't conform. The caller's existing per-entry try/except
    treats this exactly like any other parse failure: nothing is written,
    the source entry stays in --input-dir, and the error is logged -- so a
    mapping bug that produces schema-invalid output surfaces immediately
    instead of writing bad JSON that only a manual check would catch (this
    is exactly how two real mismatches were found during the first real
    batch -- see the module docstring).
    """
    jsonschema.validate(instance=activity, schema=schema)


def make_batch_id() -> str:
    """UTC timestamp used to name/pair this run's log and summary files."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def setup_batch_logger(log_dir: Path, batch_id: str) -> tuple[logging.Logger, Path]:
    """
    Create a dedicated logger for this batch run, writing to
    <log_dir>/batch_<batch_id>.log.

    A logger scoped to this batch_id (rather than the root logger) is used
    so that running main() multiple times in the same process -- e.g. under
    test -- doesn't accumulate duplicate handlers or bleed log lines between
    runs.
    """
    log_path = log_dir / f"batch_{batch_id}.log"
    logger = logging.getLogger(f"fit_parser.batch.{batch_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger, log_path


def write_batch_summary(
    summary_dir: Path,
    batch_id: str,
    log_path: Path,
    *,
    input_dir: Path,
    output_dir: Path,
    total_remaining_before: int,
    attempted: int,
    succeeded: int,
    failed: list[tuple[str, str]],
    skipped: list[tuple[str, str]],
) -> Path:
    """
    Write this batch's summary markdown to <summary_dir>/batch_<batch_id>.md.

    The summary stays high-level (counts, and just the entry names for any
    failures/skips) and points at the paired log file (same batch_id) for
    full detail -- the log is where the actual error messages/timestamps
    live, so the two aren't duplicating the same information.
    """
    summary_path = summary_dir / f"batch_{batch_id}.md"
    lines = [
        f"# Batch {batch_id}",
        "",
        f"- Input dir: `{input_dir}`",
        f"- Output dir: `{output_dir}`",
        f"- Log file: `{log_path.name}` (in `{log_path.parent}`) -- full failure/skip detail is there, not duplicated below",
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Entries remaining before this batch | {total_remaining_before} |",
        f"| Attempted this batch | {attempted} |",
        f"| Succeeded (moved to processed dir) | {succeeded} |",
        f"| Failed | {len(failed)} |",
        f"| Skipped (not a failure -- see reasons below) | {len(skipped)} |",
        "",
    ]

    if failed:
        lines.append("## Failed entries")
        lines.append("")
        for name, err in failed:
            lines.append(f"- `{name}` -- {err}")
        lines.append("")

    if skipped:
        lines.append("## Skipped entries")
        lines.append("")
        for name, reason in skipped:
            lines.append(f"- `{name}` -- {reason}")
        lines.append("")

    lines.append(f"Full detail (timestamps, complete error messages) is in the paired log file: `{log_path.name}`.")
    lines.append("")

    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


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
    parser.add_argument("--log-dir", default="data/logs",
                         help="Directory for this batch's error log (default: data/logs)")
    parser.add_argument("--summary-dir", default="data/summaries",
                         help="Directory for this batch's summary markdown (default: data/summaries)")
    parser.add_argument("--schema-path", default=str(DEFAULT_SCHEMA_PATH),
                         help="Path to the activity JSON schema used to validate every record "
                         "before it's written (default: data/schema/activity.schema.json in this repo)")
    parser.add_argument("--batch-size", type=int, default=None,
                         help="Max number of entries to process this run. "
                         "Omit to process all remaining entries in --input-dir.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    processed_dir = Path(args.processed_dir)
    log_dir = Path(args.log_dir)
    summary_dir = Path(args.summary_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)

    # Loaded once per run (not per-entry) and reused for every record's
    # validate_against_schema() call below. A missing/malformed schema file
    # is a setup problem, not a per-entry one -- let it raise here rather
    # than being swallowed by the per-entry try/except further down.
    activity_schema = load_activity_schema(Path(args.schema_path))

    batch_id = make_batch_id()
    logger, log_path = setup_batch_logger(log_dir, batch_id)
    summary_path_preview = summary_dir / f"batch_{batch_id}.md"
    logger.info(f"Batch {batch_id} starting. Summary will be written to {summary_path_preview}")

    # Only look at top-level entries still sitting in input_dir. Anything
    # already moved to processed_dir from a prior run won't reappear here,
    # which is what makes batching resumable across multiple invocations.
    all_entries = sorted(p for p in input_dir.iterdir() if p.name != ".gitkeep")
    total_remaining = len(all_entries)

    batch = all_entries[: args.batch_size] if args.batch_size else all_entries
    print(f"{total_remaining} entries remaining in {input_dir}")
    print(f"Processing {len(batch)} this run"
          + (f" (batch-size={args.batch_size})" if args.batch_size else " (no batch limit)"))
    print(f"Batch id: {batch_id} (log: {log_path}, summary: {summary_path_preview})")

    succeeded = 0
    errors: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    try:
        for i, entry in enumerate(batch, 1):
            skip_reason = classify_skip_reason(entry)
            if skip_reason is not None:
                skipped.append((entry.name, skip_reason))
                logger.info(f"SKIPPED {entry.name}: {skip_reason}")
                continue

            try:
                fit_path = find_fit_payload(entry)
                if fit_path is None:
                    raise ValueError("no .fit/.fit.gz payload found in this entry")

                raw = parse_fit_file(fit_path)
                activity = to_standard_schema(raw, entry, fit_path)
                # Catches a mapping bug before it ever reaches disk -- see
                # validate_against_schema()'s docstring for why this exists.
                validate_against_schema(activity, activity_schema)

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
                logger.error(f"FAILED {entry.name}: {e}")

            if i % 100 == 0:
                print(f"  ...{i}/{len(batch)} this batch")
    finally:
        # Write the summary even if the loop above was interrupted by an
        # unexpected exception outside a single entry's try/except, so a
        # crashed run still leaves a record of what it got through.
        summary_path = write_batch_summary(
            summary_dir, batch_id, log_path,
            input_dir=input_dir, output_dir=output_dir,
            total_remaining_before=total_remaining,
            attempted=succeeded + len(errors) + len(skipped),
            succeeded=succeeded, failed=errors, skipped=skipped,
        )
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    remaining_after = total_remaining - succeeded
    print(f"\nBatch done. {succeeded} succeeded and moved to {processed_dir}, "
          f"{len(errors)} failed and left in place, {len(skipped)} skipped (not activity entries).")
    print(f"{remaining_after} entries remain in {input_dir} "
          f"(including {len(errors)} failures and {len(skipped)} skips to resolve).")
    print(f"Log: {log_path}")
    print(f"Summary: {summary_path}")
    if errors:
        print("\nFailures (first 20):")
        for name, err in errors[:20]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
