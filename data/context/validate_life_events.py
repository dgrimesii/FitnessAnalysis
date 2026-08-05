"""
validate_life_events.py

Validates data/context/life_events.json against data/schema/life_event.schema.json,
plus a few checks a JSON Schema alone can't express:
  - no duplicate `id` values
  - every `related_event_ids` entry points at a real event in this same file
  - `date_end` (if present) is not before `date`

Run this after hand-editing life_events.json -- it's meant to be a quick
sanity check before committing a new or updated entry, not a full test
suite (this file is small and hand-maintained, unlike the FIT/TCX
pipeline's automated test suite in ingestion/strava/tests/).

Usage:
    python data/context/validate_life_events.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "life_event.schema.json"
EVENTS_PATH = Path(__file__).resolve().parent / "life_events.json"


def load(path: Path) -> object:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate() -> list[str]:
    """Returns a list of human-readable error messages; empty means everything's valid."""
    schema = load(SCHEMA_PATH)
    events = load(EVENTS_PATH)
    errors: list[str] = []

    if not isinstance(events, list):
        return [f"{EVENTS_PATH} must contain a JSON array of events, got {type(events).__name__}"]

    ids_seen: dict[str, int] = {}
    for i, event in enumerate(events):
        label = event.get("id", f"entry #{i}") if isinstance(event, dict) else f"entry #{i}"
        try:
            jsonschema.validate(instance=event, schema=schema)
        except jsonschema.exceptions.ValidationError as e:
            errors.append(f"{label}: schema violation -- {e.message}")
            continue  # further checks assume a schema-valid shape

        if event["id"] in ids_seen:
            errors.append(f"{label}: duplicate id (also used by entry #{ids_seen[event['id']]})")
        else:
            ids_seen[event["id"]] = i

        date_end = event.get("date_end")
        if date_end and date.fromisoformat(date_end) < date.fromisoformat(event["date"]):
            errors.append(f"{label}: date_end ({date_end}) is before date ({event['date']})")

    all_ids = set(ids_seen)
    for event in events:
        if not isinstance(event, dict) or "id" not in event:
            continue
        for related_id in event.get("related_event_ids", []):
            if related_id not in all_ids:
                errors.append(f"{event['id']}: related_event_ids references unknown id '{related_id}'")

    return errors


def main():
    errors = validate()
    if errors:
        print(f"FAILED -- {len(errors)} issue(s) in {EVENTS_PATH}:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    events = load(EVENTS_PATH)
    print(f"OK -- {len(events)} event(s) in {EVENTS_PATH} all valid.")


if __name__ == "__main__":
    main()
