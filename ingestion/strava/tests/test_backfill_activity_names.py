"""
test_backfill_activity_names.py

Automated tests for backfill_activity_names.py (issue #5).

Covers the CSV -> filename lookup (including the "blank name" and
"missing Filename column" edge cases confirmed against real
activities.csv rows) and the backfill pass itself: which records get
updated, which are left alone, and that every updated record is
re-validated against the real activity schema before being written.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import jsonschema
import pytest

import backfill_activity_names as backfill_module

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "data" / "schema" / "activity.schema.json"


@pytest.fixture(scope="session")
def activity_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def write_csv(path: Path, rows: list[dict]) -> Path:
    fieldnames = sorted({key for row in rows for key in row})
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _minimal_activity(**overrides) -> dict:
    activity = {
        "id": "1001254666", "source": "strava", "original_source": "garmin",
        "activity_type": "cycling", "name": None, "start_time": "2024-01-01T00:00:00",
        "raw_file_reference": "1001254666.fit.gz",
    }
    activity.update(overrides)
    return activity


# ---------------------------------------------------------------------------
# load_activity_names
# ---------------------------------------------------------------------------

def test_load_activity_names_builds_basename_keyed_lookup(tmp_path):
    csv_path = write_csv(tmp_path / "activities.csv", [
        {"Filename": "activities/1001254666.fit.gz", "Activity Name": "Morning Commute"},
        {"Filename": "activities/13553776569.tcx.gz", "Activity Name": "20 min Power Zone Ride"},
    ])
    names = backfill_module.load_activity_names(csv_path)
    assert names["1001254666.fit.gz"] == "Morning Commute"
    assert names["13553776569.tcx.gz"] == "20 min Power Zone Ride"


def test_load_activity_names_skips_blank_names(tmp_path):
    """Confirmed against real data: some rows have an empty Activity Name -- must not backfill an empty string."""
    csv_path = write_csv(tmp_path / "activities.csv", [
        {"Filename": "activities/1001254666.fit.gz", "Activity Name": ""},
    ])
    names = backfill_module.load_activity_names(csv_path)
    assert "1001254666.fit.gz" not in names


def test_load_activity_names_skips_rows_with_no_filename(tmp_path):
    """Confirmed against real data: rows for manually-logged activities (e.g. Hevy strength) have an empty Filename."""
    csv_path = write_csv(tmp_path / "activities.csv", [
        {"Filename": "", "Activity Name": "Trainer Session 5"},
    ])
    names = backfill_module.load_activity_names(csv_path)
    assert names == {}


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------

def test_backfill_updates_matching_records_and_validates_against_schema(tmp_path, activity_schema):
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    (processed_dir / "1001254666.json").write_text(json.dumps(_minimal_activity()), encoding="utf-8")

    counts = backfill_module.backfill(processed_dir, {"1001254666.fit.gz": "Morning Commute"}, activity_schema)

    updated = json.loads((processed_dir / "1001254666.json").read_text(encoding="utf-8"))
    assert updated["name"] == "Morning Commute"
    assert counts == {"updated": 1, "already_named": 0, "no_match": 0}
    jsonschema.validate(instance=updated, schema=activity_schema)


def test_backfill_leaves_unmatched_records_null(tmp_path, activity_schema):
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    (processed_dir / "1001254666.json").write_text(json.dumps(_minimal_activity()), encoding="utf-8")

    counts = backfill_module.backfill(processed_dir, {}, activity_schema)

    unchanged = json.loads((processed_dir / "1001254666.json").read_text(encoding="utf-8"))
    assert unchanged["name"] is None
    assert counts == {"updated": 0, "already_named": 0, "no_match": 1}


def test_backfill_does_not_overwrite_an_existing_name(tmp_path, activity_schema):
    """A record that already has a name (e.g. from a future #6 run) must be left alone, not overwritten."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    (processed_dir / "1001254666.json").write_text(
        json.dumps(_minimal_activity(name="Already Named")), encoding="utf-8"
    )

    counts = backfill_module.backfill(processed_dir, {"1001254666.fit.gz": "Different Name"}, activity_schema)

    unchanged = json.loads((processed_dir / "1001254666.json").read_text(encoding="utf-8"))
    assert unchanged["name"] == "Already Named"
    assert counts == {"updated": 0, "already_named": 1, "no_match": 0}


def test_backfill_handles_multiple_files_independently(tmp_path, activity_schema):
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    (processed_dir / "1001254666.json").write_text(
        json.dumps(_minimal_activity(id="1001254666", raw_file_reference="1001254666.fit.gz")), encoding="utf-8"
    )
    (processed_dir / "13553776569.json").write_text(
        json.dumps(_minimal_activity(id="13553776569", raw_file_reference="13553776569.tcx.gz",
                                      original_source="peloton", activity_type="Biking")), encoding="utf-8"
    )

    counts = backfill_module.backfill(
        processed_dir,
        {"1001254666.fit.gz": "Morning Commute", "13553776569.tcx.gz": "20 min Power Zone Ride"},
        activity_schema,
    )

    assert counts["updated"] == 2
    assert json.loads((processed_dir / "1001254666.json").read_text())["name"] == "Morning Commute"
    assert json.loads((processed_dir / "13553776569.json").read_text())["name"] == "20 min Power Zone Ride"
