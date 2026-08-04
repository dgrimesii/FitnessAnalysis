"""
test_fit_parser.py

Automated tests for ingestion/strava/fit_parser.py.

Covers, in order:
  1. Entry-shape detection (find_fit_payload) and skip classification
     (classify_skip_reason) -- including the two real-world cases confirmed
     against the actual export in issue #1: a stray non-activity directory,
     and known-but-unsupported file types (.tcx.gz/.gpx, see issues #2/#3).
  2. Field-mapping helpers (derive_activity_id, guess_original_source,
     to_standard_schema), including regression tests for the two real bugs
     found and fixed in this pass (device_name/garmin_product, and the
     gzip-closed-file bug in parse_fit_file).
  3. Batch-level plumbing: the per-batch logger, the summary markdown
     writer, and a full end-to-end run of main() over a synthetic
     --input-dir, asserting on succeeded/failed/skipped counts, what moved
     where, and the log/summary cross-reference.
  4. A schema-conformance check that runs produced JSON through the actual
     data/schema/activity.schema.json via jsonschema, automating the
     "spot-check output against the schema" step called for in issue #1.
  5. Track-point extraction and Parquet output (issue #4): GPS semicircle
     conversion, points with a missing GPS fix, the enhanced_* field
     fallback, a Parquet round-trip, and the gps_track_available fix
     (real record-level GPS presence, not a total_distance guess).

None of these tests touch real Strava export data or the H: drive --
.fit.gz parsing is exercised via a fake FitFile stand-in (see
FakeFitFile below) rather than real binary FIT data, since fitparse
requires a real binary FIT structure that isn't practical to hand-author
as a test fixture and shouldn't depend on the user's personal export.
"""

from __future__ import annotations

import gzip
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pyarrow.parquet as pq
import pytest

import fit_parser


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "data" / "schema" / "activity.schema.json"


@pytest.fixture(scope="session")
def activity_schema() -> dict:
    """Load the real repo schema once per test session."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def make_fit_gz(path: Path, content: bytes = b"not-real-fit-bytes") -> Path:
    """
    Write a syntactically-valid gzip file at `path`. The *contents* are
    arbitrary -- tests that need parse_fit_file() to do something
    meaningful monkeypatch fitparse.FitFile (see FakeFitFile) rather than
    relying on real FIT binary parsing, since real FIT data isn't practical
    to hand-author as a fixture.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(content)
    return path


class FakeField:
    """Stand-in for a fitparse field: exposes .name/.value like the real one."""

    def __init__(self, name, value):
        self.name = name
        self.value = value


class FakeMessage:
    """Stand-in for a fitparse message: iterating it yields FakeFields."""

    def __init__(self, fields):
        self._fields = fields

    def __iter__(self):
        return iter(self._fields)


class FakeFitFile:
    """
    Stand-in for fitparse.FitFile used to test parse_fit_file() without
    needing real binary FIT data.

    Critically, this defers checking whether `source` is still usable until
    get_messages() is called -- not __init__ -- because that's what real
    fitparse does (it reads lazily). This is what makes it possible to
    reproduce/guard against the original gzip-closed-file bug: code that
    passes a `with gzip.open(...) as f:` handle to FitFile and then calls
    get_messages() *after* that `with` block exits will hit this check with
    an already-closed stream and fail loudly, exactly like the real bug did.
    Code that reads the gzip bytes fully before constructing FitFile (the
    fix) passes plain `bytes`, which has no `.closed` attribute, so the
    check is skipped and parsing proceeds.
    """

    _MESSAGES = {
        "file_id": [FakeMessage([FakeField("manufacturer", "garmin"), FakeField("garmin_product", "edge810")])],
        "session": [FakeMessage([FakeField("sport", "cycling"), FakeField("start_time", "2024-01-01T00:00:00")])],
        "record": [
            FakeMessage([
                FakeField("timestamp", datetime(2024, 1, 1, 0, 0, 0)),
                FakeField("position_lat", 420306253),
                FakeField("position_long", -964454572),
                FakeField("altitude", 147.4),
                FakeField("heart_rate", 111),
                FakeField("cadence", 45),
                FakeField("speed", 2.325),
                FakeField("distance", 2.33),
            ]),
        ],
    }

    def __init__(self, source):
        self._source = source

    def get_messages(self, name):
        if hasattr(self._source, "closed") and self._source.closed:
            # Mirrors the real fitparse/gzip error this bug produced.
            raise ValueError("I/O operation on closed file.")
        return self._MESSAGES.get(name, [])


# ---------------------------------------------------------------------------
# find_fit_payload / classify_skip_reason
# ---------------------------------------------------------------------------

def test_find_fit_payload_flat_fit_file(tmp_path):
    entry = tmp_path / "123.fit"
    entry.write_bytes(b"x")
    assert fit_parser.find_fit_payload(entry) == entry


def test_find_fit_payload_flat_fit_gz_file(tmp_path):
    entry = make_fit_gz(tmp_path / "123.fit.gz")
    assert fit_parser.find_fit_payload(entry) == entry


def test_find_fit_payload_is_case_insensitive(tmp_path):
    """Real exports could plausibly vary casing; matching is deliberately robust to it."""
    entry = tmp_path / "123.FIT.GZ"
    make_fit_gz(entry)
    assert fit_parser.find_fit_payload(entry) == entry


def test_find_fit_payload_archive_folder_with_one_fit_file(tmp_path):
    folder = tmp_path / "archive_entry"
    folder.mkdir()
    payload = make_fit_gz(folder / "activity.fit.gz")
    assert fit_parser.find_fit_payload(folder) == payload


def test_find_fit_payload_archive_folder_with_no_fit_file_returns_none(tmp_path):
    """
    Real-world case: the pre-existing, empty `Processed/` folder found
    inside the real export's activities/ directory (see issue #1) has no
    .fit/.fit.gz payload -- this is what makes classify_skip_reason()
    recognize it as a non-activity entry.
    """
    folder = tmp_path / "Processed"
    folder.mkdir()
    assert fit_parser.find_fit_payload(folder) is None


def test_find_fit_payload_non_fit_file_returns_none(tmp_path):
    entry = tmp_path / "readme.txt"
    entry.write_text("hello")
    assert fit_parser.find_fit_payload(entry) is None


def test_classify_skip_reason_empty_directory_is_skipped(tmp_path):
    folder = tmp_path / "Processed"
    folder.mkdir()
    reason = fit_parser.classify_skip_reason(folder)
    assert reason is not None
    assert "no .fit/.fit.gz payload" in reason


def test_classify_skip_reason_archive_folder_with_payload_is_not_skipped(tmp_path):
    folder = tmp_path / "archive_entry"
    folder.mkdir()
    make_fit_gz(folder / "activity.fit.gz")
    assert fit_parser.classify_skip_reason(folder) is None


@pytest.mark.parametrize("filename", ["1234.tcx.gz", "1234.tcx", "1234.gpx"])
def test_classify_skip_reason_known_unsupported_extensions_are_skipped(tmp_path, filename):
    """
    .tcx/.tcx.gz/.gpx are confirmed present in the real export (92 + 8
    files respectively) but are handled by separate follow-up issues (#2,
    #3), not this script -- they must be skipped, not reported as failures.
    """
    entry = tmp_path / filename
    entry.write_bytes(b"x")
    reason = fit_parser.classify_skip_reason(entry)
    assert reason is not None
    assert "issues #2/#3" in reason


def test_classify_skip_reason_fit_gz_file_is_not_skipped(tmp_path):
    entry = make_fit_gz(tmp_path / "123.fit.gz")
    assert fit_parser.classify_skip_reason(entry) is None


def test_classify_skip_reason_unrecognized_file_falls_through_to_normal_parsing(tmp_path):
    """
    A totally unrecognized file type isn't silently skipped -- it falls
    through to find_fit_payload()/parsing, where it becomes a genuine
    failure worth investigating, unlike the known-safe skip cases above.
    """
    entry = tmp_path / "mystery.xyz"
    entry.write_bytes(b"x")
    assert fit_parser.classify_skip_reason(entry) is None
    assert fit_parser.find_fit_payload(entry) is None  # -> failure in main(), not a skip


# ---------------------------------------------------------------------------
# derive_activity_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, expected_id",
    [
        ("1234567.fit.gz", "1234567"),
        ("1234567.fit", "1234567"),
        ("archive_entry", "archive_entry"),  # folder name, no suffix to strip
    ],
)
def test_derive_activity_id(tmp_path, name, expected_id):
    entry = tmp_path / name
    assert fit_parser.derive_activity_id(entry) == expected_id


# ---------------------------------------------------------------------------
# guess_original_source
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "manufacturer, expected",
    [
        ("garmin", "garmin"),
        ("Garmin", "garmin"),  # confirmed case-sensitivity handling via .lower()
        ("peloton", "peloton"),
        ("hevy", "hevy"),
        ("strava", "unknown"),  # Strava sometimes overwrites this field -- falls back
        ("", "unknown"),
        ("some_other_brand", "some_other_brand"),  # captured verbatim, not dropped
    ],
)
def test_guess_original_source(manufacturer, expected):
    assert fit_parser.guess_original_source({"manufacturer": manufacturer}) == expected


def test_guess_original_source_missing_manufacturer_key():
    assert fit_parser.guess_original_source({}) == "unknown"


# ---------------------------------------------------------------------------
# to_standard_schema
# ---------------------------------------------------------------------------

def _raw(sport="cycling", manufacturer="garmin", records=None, **session_overrides):
    session = {"sport": sport, "start_time": datetime(2024, 1, 1, tzinfo=timezone.utc)}
    session.update(session_overrides)
    return {
        "file_id": {"manufacturer": manufacturer, "garmin_product": "edge810"},
        "session": session,
        "records": records if records is not None else [],
    }


def test_to_standard_schema_endurance_routing(tmp_path):
    entry = tmp_path / "1.fit.gz"
    activity = fit_parser.to_standard_schema(_raw(sport="cycling"), entry, entry)
    assert activity["endurance_metrics"] is not None
    assert activity["strength_metrics"] is None
    assert activity["class_metrics"] is None


def test_to_standard_schema_strength_routing(tmp_path):
    entry = tmp_path / "1.fit.gz"
    activity = fit_parser.to_standard_schema(_raw(sport="training"), entry, entry)
    assert activity["strength_metrics"] == {"exercises": []}
    assert activity["endurance_metrics"] is None
    assert activity["class_metrics"] is None


def test_to_standard_schema_class_routing(tmp_path):
    entry = tmp_path / "1.fit.gz"
    activity = fit_parser.to_standard_schema(_raw(sport="fitness_equipment"), entry, entry)
    assert activity["class_metrics"] is not None
    assert activity["endurance_metrics"] is None
    assert activity["strength_metrics"] is None


def test_to_standard_schema_unknown_sport_populates_no_metrics_block(tmp_path):
    entry = tmp_path / "1.fit.gz"
    activity = fit_parser.to_standard_schema(_raw(sport="curling"), entry, entry)
    assert activity["endurance_metrics"] is None
    assert activity["strength_metrics"] is None
    assert activity["class_metrics"] is None


def test_to_standard_schema_device_name_uses_garmin_product(tmp_path):
    """
    Regression test for the device_name bug: file_id["product"] never
    matched real data (fitparse names the field "garmin_product" for
    Garmin devices), so device_name was silently always None before the
    fix.
    """
    entry = tmp_path / "1.fit.gz"
    raw = _raw()
    raw["file_id"]["garmin_product"] = "edge1030"
    activity = fit_parser.to_standard_schema(raw, entry, entry)
    assert activity["device_name"] == "edge1030"


def test_to_standard_schema_device_name_falls_back_to_generic_product_key(tmp_path):
    entry = tmp_path / "1.fit.gz"
    raw = _raw()
    del raw["file_id"]["garmin_product"]
    raw["file_id"]["product"] = "some_generic_device"
    activity = fit_parser.to_standard_schema(raw, entry, entry)
    assert activity["device_name"] == "some_generic_device"


def test_to_standard_schema_device_name_stringifies_raw_numeric_product_code(tmp_path):
    """
    Regression test found via the real first batch: fitparse only decodes
    garmin_product to a friendly name (e.g. "edge810") for device IDs it
    recognizes -- an unrecognized one (a real example: a newer Edge model
    came through as the raw int 3122) would otherwise leak a non-string
    into device_name, which is a schema-declared string field, and failed
    jsonschema validation ("3122 is not of type 'string', 'null'").
    """
    entry = tmp_path / "1.fit.gz"
    raw = _raw()
    raw["file_id"]["garmin_product"] = 3122
    activity = fit_parser.to_standard_schema(raw, entry, entry)
    assert activity["device_name"] == "3122"
    assert isinstance(activity["device_name"], str)


def test_to_standard_schema_start_time_handles_datetime_and_plain_string(tmp_path):
    entry = tmp_path / "1.fit.gz"
    dt_activity = fit_parser.to_standard_schema(_raw(start_time=datetime(2024, 5, 1, tzinfo=timezone.utc)), entry, entry)
    assert dt_activity["start_time"] == "2024-05-01T00:00:00+00:00"

    str_activity = fit_parser.to_standard_schema(_raw(start_time="2024-05-01T00:00:00"), entry, entry)
    assert str_activity["start_time"] == "2024-05-01T00:00:00"


def test_sport_type_groupings_do_not_overlap():
    """Sanity guard: a sport string routed to more than one metrics block would be ambiguous."""
    assert fit_parser.ENDURANCE_TYPES.isdisjoint(fit_parser.STRENGTH_TYPES)
    assert fit_parser.ENDURANCE_TYPES.isdisjoint(fit_parser.CLASS_TYPES)
    assert fit_parser.STRENGTH_TYPES.isdisjoint(fit_parser.CLASS_TYPES)


# ---------------------------------------------------------------------------
# parse_fit_file -- gzip-closed-file regression
# ---------------------------------------------------------------------------

def test_parse_fit_file_reads_gzip_fully_before_fitfile_reads_messages(tmp_path, monkeypatch):
    """
    Regression test for the "I/O operation on closed file" bug: the
    original code constructed FitFile from a `with gzip.open(...) as f`
    handle and called get_messages() after that block exited, closing the
    stream. FakeFitFile.get_messages() raises the same error real fitparse
    would if it's ever handed an already-closed stream -- so this test
    fails loudly if the bug is reintroduced.
    """
    monkeypatch.setattr(fit_parser, "FitFile", FakeFitFile)
    gz_path = make_fit_gz(tmp_path / "123.fit.gz")

    result = fit_parser.parse_fit_file(gz_path)

    assert result["file_id"] == {"manufacturer": "garmin", "garmin_product": "edge810"}
    assert result["session"] == {"sport": "cycling", "start_time": "2024-01-01T00:00:00"}
    assert len(result["records"]) == 1
    assert result["records"][0]["heart_rate"] == 111


def test_parse_fit_file_uncompressed_fit_also_works(tmp_path, monkeypatch):
    monkeypatch.setattr(fit_parser, "FitFile", FakeFitFile)
    fit_path = tmp_path / "123.fit"
    fit_path.write_bytes(b"not-real-fit-bytes")

    result = fit_parser.parse_fit_file(fit_path)

    assert result["file_id"]["manufacturer"] == "garmin"


# ---------------------------------------------------------------------------
# to_track_points / write_track_points / gps_track_available (issue #4)
# ---------------------------------------------------------------------------

def test_to_track_points_converts_semicircles_to_degrees():
    # 420306253/-964454572 semicircles is a real (lat, lon) pair sampled from the export (issue #4).
    lat_semicircles, lon_semicircles = 420306253, -964454572
    records = [{"timestamp": datetime(2024, 1, 1), "position_lat": lat_semicircles, "position_long": lon_semicircles}]
    points = fit_parser.to_track_points({"records": records}, "activity_1")
    assert points[0]["lat"] == pytest.approx(lat_semicircles * (180 / 2 ** 31))
    assert points[0]["lon"] == pytest.approx(lon_semicircles * (180 / 2 ** 31))
    # Sanity-check against the real-world range confirmed in issue #4's survey (North Carolina).
    assert 34 < points[0]["lat"] < 36
    assert -81 < points[0]["lon"] < -76


def test_to_track_points_keeps_points_with_missing_gps_fix():
    """
    Regression for a real finding: a ride's first several points had
    heart_rate/cadence/speed/distance but no position_lat/position_long yet
    (normal GPS-acquisition lag) -- those points must be kept with lat/lon
    as None, not dropped, since every other sensor reading is still real.
    """
    records = [{"timestamp": datetime(2024, 1, 1), "heart_rate": 88, "cadence": 0, "distance": 2.64}]
    points = fit_parser.to_track_points({"records": records}, "activity_1")
    assert len(points) == 1
    assert points[0]["lat"] is None
    assert points[0]["lon"] is None
    assert points[0]["heart_rate"] == 88


def test_to_track_points_prefers_enhanced_altitude_and_speed():
    records = [{"timestamp": datetime(2024, 1, 1), "altitude": 100.0, "enhanced_altitude": 100.4,
                "speed": 2.3, "enhanced_speed": 2.325}]
    points = fit_parser.to_track_points({"records": records}, "activity_1")
    assert points[0]["altitude_m"] == 100.4
    assert points[0]["speed_mps"] == 2.325


def test_to_track_points_falls_back_to_base_altitude_and_speed_when_enhanced_absent():
    records = [{"timestamp": datetime(2024, 1, 1), "altitude": 100.0, "speed": 2.3}]
    points = fit_parser.to_track_points({"records": records}, "activity_1")
    assert points[0]["altitude_m"] == 100.0
    assert points[0]["speed_mps"] == 2.3


def test_to_track_points_activity_id_is_attached_to_every_row():
    records = [{"timestamp": datetime(2024, 1, 1)}, {"timestamp": datetime(2024, 1, 1, 0, 0, 1)}]
    points = fit_parser.to_track_points({"records": records}, "activity_42")
    assert [p["activity_id"] for p in points] == ["activity_42", "activity_42"]


def test_to_track_points_handles_missing_records_key():
    """raw dicts built by lightweight test doubles that omit "records" shouldn't crash this."""
    assert fit_parser.to_track_points({}, "activity_1") == []


def test_write_track_points_round_trips_through_parquet(tmp_path):
    points = [{
        "activity_id": "activity_1", "timestamp": datetime(2024, 1, 1, 12, 0, 0),
        "lat": 35.229757, "lon": -80.839731, "altitude_m": 147.4,
        "heart_rate": 111, "cadence": 45, "speed_mps": 2.325, "distance_m": 2.33,
    }]
    out_path = tmp_path / "activity_1.parquet"

    fit_parser.write_track_points(points, out_path)

    table = pq.read_table(out_path)
    assert table.schema.equals(fit_parser.TRACK_POINT_SCHEMA)
    row = table.to_pylist()[0]
    assert row["activity_id"] == "activity_1"
    assert row["heart_rate"] == 111
    assert row["lat"] == pytest.approx(35.229757)


def test_write_track_points_handles_zero_points(tmp_path):
    """An activity with no record messages still gets a valid, correctly-typed (empty) Parquet file."""
    out_path = tmp_path / "activity_empty.parquet"
    fit_parser.write_track_points([], out_path)
    table = pq.read_table(out_path)
    assert table.num_rows == 0
    assert table.schema.equals(fit_parser.TRACK_POINT_SCHEMA)


@pytest.mark.parametrize(
    "records, expected",
    [
        ([{"position_lat": 420306253, "position_long": -964454572}], True),
        ([{"heart_rate": 88}], False),  # no GPS field at all on this point
        ([{"position_lat": None, "position_long": None}], False),  # keys present but null
        ([], False),
    ],
)
def test_has_real_gps_fix(records, expected):
    assert fit_parser._has_real_gps_fix(records) is expected


def test_to_standard_schema_gps_track_available_reflects_real_gps_not_total_distance(tmp_path):
    """
    Regression test for the gps_track_available fix: the old guess
    (session.get("total_distance") is not None) would say True here even
    though there's no real GPS fix anywhere in the ride -- distance can
    also come from a wheel/speed sensor with no GPS at all. The new check
    looks at the actual record-level position fields instead.
    """
    entry = tmp_path / "1.fit.gz"
    raw = _raw(sport="cycling", total_distance=5000.0, records=[{"heart_rate": 120, "cadence": 80}])
    activity = fit_parser.to_standard_schema(raw, entry, entry)
    assert activity["endurance_metrics"]["gps_track_available"] is False


def test_to_standard_schema_gps_track_available_true_when_gps_present(tmp_path):
    entry = tmp_path / "1.fit.gz"
    raw = _raw(sport="cycling", records=[{"position_lat": 420306253, "position_long": -964454572}])
    activity = fit_parser.to_standard_schema(raw, entry, entry)
    assert activity["endurance_metrics"]["gps_track_available"] is True


# ---------------------------------------------------------------------------
# load_activity_schema / validate_against_schema
# ---------------------------------------------------------------------------

def test_load_activity_schema_loads_the_real_schema_file():
    schema = fit_parser.load_activity_schema(fit_parser.DEFAULT_SCHEMA_PATH)
    assert schema["title"] == "FitnessAnalysis Activity"
    assert "id" in schema["required"]


def test_validate_against_schema_passes_for_valid_activity(tmp_path, activity_schema):
    entry = tmp_path / "1.fit.gz"
    activity = fit_parser.to_standard_schema(_raw(), entry, entry)
    fit_parser.validate_against_schema(activity, activity_schema)  # no exception raised


def test_validate_against_schema_raises_for_invalid_activity(activity_schema):
    invalid_activity = {"id": "x", "source": "strava"}  # missing several required fields
    with pytest.raises(jsonschema.exceptions.ValidationError):
        fit_parser.validate_against_schema(invalid_activity, activity_schema)


# ---------------------------------------------------------------------------
# Batch logger / summary
# ---------------------------------------------------------------------------

def test_setup_batch_logger_writes_to_expected_path(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    logger, log_path = fit_parser.setup_batch_logger(log_dir, "20260101_000000")

    assert log_path == log_dir / "batch_20260101_000000.log"
    logger.info("hello")
    logger.error("something failed")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    content = log_path.read_text(encoding="utf-8")
    assert "[INFO] hello" in content
    assert "[ERROR] something failed" in content


def test_write_batch_summary_references_paired_log_file(tmp_path):
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    log_path = tmp_path / "logs" / "batch_20260101_000000.log"

    summary_path = fit_parser.write_batch_summary(
        summary_dir, "20260101_000000", log_path,
        input_dir=tmp_path / "activities", output_dir=tmp_path / "processed_json",
        total_remaining_before=10, attempted=3, succeeded=1,
        failed=[("bad.fit.gz", "boom")], skipped=[("Processed", "no payload")],
    )

    assert summary_path == summary_dir / "batch_20260101_000000.md"
    content = summary_path.read_text(encoding="utf-8")
    # Counts show up in the summary...
    assert "| Succeeded (moved to processed dir) | 1 |" in content
    assert "| Failed | 1 |" in content
    assert "| Skipped (not a failure -- see reasons below) | 1 |" in content
    # ...and full detail is deferred to the log file, referenced by name.
    assert "batch_20260101_000000.log" in content
    assert "bad.fit.gz" in content
    assert "Processed" in content


# ---------------------------------------------------------------------------
# End-to-end: main()
# ---------------------------------------------------------------------------

@pytest.fixture
def batch_workspace(tmp_path):
    """
    A synthetic --input-dir mirroring the real export's confirmed shapes
    (see issue #1): a valid .fit.gz entry, a broken one, the stray
    non-activity directory, and an unsupported-but-known file type.
    Nothing here touches the real H: drive or real FIT binary data.
    """
    input_dir = tmp_path / "activities"
    output_dir = tmp_path / "data" / "processed"
    processed_dir = tmp_path / "processed"
    tracks_dir = tmp_path / "data" / "processed_tracks"
    log_dir = tmp_path / "data" / "logs"
    summary_dir = tmp_path / "data" / "summaries"

    make_fit_gz(input_dir / "good_1.fit.gz")
    make_fit_gz(input_dir / "bad_1.fit.gz")
    (input_dir / "Processed").mkdir(parents=True)  # real-world stray folder
    (input_dir / "peloton_1.tcx.gz").write_bytes(b"x")  # known-unsupported type
    (input_dir / ".gitkeep").write_bytes(b"")  # pre-existing exclusion, untouched by this change

    return {
        "input_dir": input_dir, "output_dir": output_dir, "processed_dir": processed_dir,
        "tracks_dir": tracks_dir, "log_dir": log_dir, "summary_dir": summary_dir,
    }


def test_main_end_to_end_batch(batch_workspace, monkeypatch):
    """
    Runs the real main() over a synthetic workspace, faking only the
    FIT-parsing boundary (parse_fit_file) so "good_1" succeeds and "bad_1"
    fails deterministically -- everything else (entry classification,
    skip/failure/success bookkeeping, file moves, log/summary writing) is
    exercised for real.
    """
    ws = batch_workspace

    def fake_parse_fit_file(filepath):
        if "bad" in filepath.name:
            raise ValueError("simulated corrupt FIT data")
        return {"file_id": {"manufacturer": "garmin", "garmin_product": "edge810"},
                "session": {"sport": "cycling", "start_time": "2024-01-01T00:00:00"},
                "records": [{"timestamp": datetime(2024, 1, 1), "position_lat": 420306253,
                             "position_long": -964454572, "heart_rate": 111}]}

    monkeypatch.setattr(fit_parser, "parse_fit_file", fake_parse_fit_file)
    monkeypatch.setattr(sys, "argv", [
        "fit_parser.py",
        "--input-dir", str(ws["input_dir"]),
        "--output-dir", str(ws["output_dir"]),
        "--processed-dir", str(ws["processed_dir"]),
        "--tracks-dir", str(ws["tracks_dir"]),
        "--log-dir", str(ws["log_dir"]),
        "--summary-dir", str(ws["summary_dir"]),
    ])

    fit_parser.main()

    # Succeeded: JSON written, source moved out of input_dir.
    output_files = list(ws["output_dir"].glob("*.json"))
    assert [f.name for f in output_files] == ["good_1.json"]
    assert (ws["processed_dir"] / "good_1.fit.gz").exists()
    assert not (ws["input_dir"] / "good_1.fit.gz").exists()

    # Track Parquet written only for the succeeded entry, with the point
    # data from fake_parse_fit_file's "records" correctly carried through.
    track_files = list(ws["tracks_dir"].glob("*.parquet"))
    assert [f.name for f in track_files] == ["good_1.parquet"]
    track_table = pq.read_table(track_files[0])
    assert track_table.num_rows == 1
    assert track_table.to_pylist()[0]["heart_rate"] == 111

    # Failed: left in place, not moved.
    assert (ws["input_dir"] / "bad_1.fit.gz").exists()

    # Skipped: left in place, not moved, not reported as a failure.
    assert (ws["input_dir"] / "Processed").exists()
    assert (ws["input_dir"] / "peloton_1.tcx.gz").exists()

    # .gitkeep was never a candidate entry at all (pre-existing behavior).
    assert (ws["input_dir"] / ".gitkeep").exists()

    # Exactly one log + one summary file, sharing the same batch_id.
    log_files = list(ws["log_dir"].glob("batch_*.log"))
    summary_files = list(ws["summary_dir"].glob("batch_*.md"))
    assert len(log_files) == 1
    assert len(summary_files) == 1
    assert log_files[0].stem == summary_files[0].stem  # same "batch_<id>"

    log_content = log_files[0].read_text(encoding="utf-8")
    assert "FAILED bad_1.fit.gz" in log_content
    assert "SKIPPED Processed" in log_content
    assert "SKIPPED peloton_1.tcx.gz" in log_content

    summary_content = summary_files[0].read_text(encoding="utf-8")
    assert "| Succeeded (moved to processed dir) | 1 |" in summary_content
    assert "| Failed | 1 |" in summary_content
    assert "| Skipped (not a failure -- see reasons below) | 2 |" in summary_content
    assert log_files[0].name in summary_content


def test_main_treats_schema_invalid_output_as_a_failure(batch_workspace, monkeypatch):
    """
    A record that parses without raising but maps to schema-invalid JSON
    (the exact shape of both real bugs found during the first real batch --
    see the module docstring) must not be written to --output-dir or moved
    to --processed-dir. It's caught by validate_against_schema() and
    treated exactly like a normal parse failure: logged and left in
    --input-dir. This is the guard that would have caught those two real
    mismatches automatically instead of needing a manual jsonschema check.
    """
    ws = batch_workspace

    def fake_parse_fit_file(filepath):
        return {"file_id": {"manufacturer": "garmin", "garmin_product": "edge810"},
                "session": {"sport": "cycling", "start_time": "2024-01-01T00:00:00"},
                "records": [{"timestamp": datetime(2024, 1, 1), "heart_rate": 111}]}

    real_to_standard_schema = fit_parser.to_standard_schema

    def fake_to_standard_schema(raw, entry, fit_path):
        activity = real_to_standard_schema(raw, entry, fit_path)
        if "bad" in entry.name:
            # Simulate a mapping bug producing a schema-invalid field, the
            # same shape as the real elapsed_time_s mismatch found earlier.
            activity["elapsed_time_s"] = "not-a-number"
        return activity

    monkeypatch.setattr(fit_parser, "parse_fit_file", fake_parse_fit_file)
    monkeypatch.setattr(fit_parser, "to_standard_schema", fake_to_standard_schema)
    monkeypatch.setattr(sys, "argv", [
        "fit_parser.py",
        "--input-dir", str(ws["input_dir"]),
        "--output-dir", str(ws["output_dir"]),
        "--processed-dir", str(ws["processed_dir"]),
        "--tracks-dir", str(ws["tracks_dir"]),
        "--log-dir", str(ws["log_dir"]),
        "--summary-dir", str(ws["summary_dir"]),
    ])

    fit_parser.main()

    # good_1 is schema-valid: succeeds as normal.
    assert [f.name for f in ws["output_dir"].glob("*.json")] == ["good_1.json"]
    assert (ws["processed_dir"] / "good_1.fit.gz").exists()

    # bad_1 "parsed" fine but mapped to schema-invalid JSON: treated as a
    # failure, nothing written for it (JSON or track Parquet), source left
    # in place.
    assert (ws["input_dir"] / "bad_1.fit.gz").exists()
    assert not (ws["output_dir"] / "bad_1.json").exists()
    assert not (ws["tracks_dir"] / "bad_1.parquet").exists()
    assert [f.name for f in ws["tracks_dir"].glob("*.parquet")] == ["good_1.parquet"]

    log_content = next(ws["log_dir"].glob("batch_*.log")).read_text(encoding="utf-8")
    assert "FAILED bad_1.fit.gz" in log_content
    assert "not-a-number" in log_content  # jsonschema's own error message names the bad value

    summary_content = next(ws["summary_dir"].glob("batch_*.md")).read_text(encoding="utf-8")
    assert "| Succeeded (moved to processed dir) | 1 |" in summary_content
    assert "| Failed | 1 |" in summary_content


def test_batch_size_limits_entries_processed_this_run(tmp_path, monkeypatch):
    """--batch-size caps how many entries a single run touches, leaving the rest untouched for the next run."""
    input_dir = tmp_path / "activities"
    for i in range(5):
        make_fit_gz(input_dir / f"activity_{i}.fit.gz")

    monkeypatch.setattr(fit_parser, "parse_fit_file", lambda filepath: {
        "file_id": {"manufacturer": "garmin"},
        "session": {"sport": "cycling", "start_time": "2024-01-01T00:00:00"},
        "records": [],
    })
    monkeypatch.setattr(sys, "argv", [
        "fit_parser.py",
        "--input-dir", str(input_dir),
        "--output-dir", str(tmp_path / "out"),
        "--processed-dir", str(tmp_path / "processed"),
        "--tracks-dir", str(tmp_path / "tracks"),
        "--log-dir", str(tmp_path / "logs"),
        "--summary-dir", str(tmp_path / "summaries"),
        "--batch-size", "2",
    ])

    fit_parser.main()

    assert len(list((tmp_path / "processed").iterdir())) == 2
    assert len(list(input_dir.glob("*.fit.gz"))) == 3  # 5 - 2 processed


# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sport", ["cycling", "training", "fitness_equipment", "curling"])
def test_to_standard_schema_output_conforms_to_activity_schema(tmp_path, activity_schema, sport):
    """
    Automates the "spot-check output JSON against the schema" step from
    issue #1's acceptance criteria, across each metrics-routing branch
    (endurance/strength/class/none).
    """
    entry = tmp_path / "1234567.fit.gz"
    activity = fit_parser.to_standard_schema(_raw(sport=sport), entry, entry)
    jsonschema.validate(instance=activity, schema=activity_schema)


def test_to_standard_schema_output_conforms_with_fractional_seconds_and_raw_device_code(tmp_path, activity_schema):
    """
    Regression test for two real findings from the first real batch run
    against the H: drive export:
      - elapsed_time_s/moving_time_s: real Garmin session data reports
        fractional seconds (e.g. 780.532), which the schema originally
        typed as integer-only and rejected -- schema widened to "number".
      - device_name: an unrecognized garmin_product came through from
        fitparse as a raw int (3122) rather than a decoded string -- now
        stringified in to_standard_schema() so it stays schema-valid.
    """
    entry = tmp_path / "1234567.fit.gz"
    raw = _raw(sport="cycling", total_elapsed_time=780.532, total_timer_time=779.75)
    raw["file_id"]["garmin_product"] = 3122
    activity = fit_parser.to_standard_schema(raw, entry, entry)
    assert activity["elapsed_time_s"] == 780.532
    assert activity["moving_time_s"] == 779.75
    assert activity["device_name"] == "3122"
    jsonschema.validate(instance=activity, schema=activity_schema)
