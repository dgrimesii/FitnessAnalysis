"""
validate_weight.py

Validates data/weight/weight.json against data/schema/weight.schema.json,
plus a few checks a JSON Schema alone can't express:
  - no duplicate `id` values
  - weight_lb is consistent with weight_kg (catches a stale/hand-edited row)
  - weight_kg falls within a plausible human range (catches unit mistakes,
    e.g. accidentally storing pounds in weight_kg)

Run this after re-running ingestion/google_fit/extract_weight.py or
hand-editing the file.

Usage:
    python data/weight/validate_weight.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "weight.schema.json"
WEIGHT_PATH = Path(__file__).resolve().parent / "weight.json"

KG_TO_LB = 2.2046226218
PLAUSIBLE_KG_RANGE = (30, 250)


def load(path: Path) -> object:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate() -> list[str]:
    """Returns a list of human-readable error messages; empty means everything's valid."""
    schema = load(SCHEMA_PATH)
    readings = load(WEIGHT_PATH)
    errors: list[str] = []

    if not isinstance(readings, list):
        return [f"{WEIGHT_PATH} must contain a JSON array of readings, got {type(readings).__name__}"]

    ids_seen: dict[str, int] = {}
    for i, reading in enumerate(readings):
        label = reading.get("id", f"entry #{i}") if isinstance(reading, dict) else f"entry #{i}"
        try:
            jsonschema.validate(instance=reading, schema=schema)
        except jsonschema.exceptions.ValidationError as e:
            errors.append(f"{label}: schema violation -- {e.message}")
            continue  # further checks assume a schema-valid shape

        if reading["id"] in ids_seen:
            errors.append(f"{label}: duplicate id (also used by entry #{ids_seen[reading['id']]})")
        else:
            ids_seen[reading["id"]] = i

        kg = reading["weight_kg"]
        if not (PLAUSIBLE_KG_RANGE[0] <= kg <= PLAUSIBLE_KG_RANGE[1]):
            errors.append(f"{label}: weight_kg={kg} is outside the plausible range {PLAUSIBLE_KG_RANGE} -- check units")

        expected_lb = round(kg * KG_TO_LB, 1)
        if "weight_lb" in reading and abs(reading["weight_lb"] - expected_lb) > 0.2:
            errors.append(f"{label}: weight_lb={reading['weight_lb']} doesn't match weight_kg={kg} (expected ~{expected_lb})")

    return errors


def main():
    errors = validate()
    if errors:
        print(f"FAILED -- {len(errors)} issue(s) in {WEIGHT_PATH}:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    readings = load(WEIGHT_PATH)
    print(f"OK -- {len(readings)} reading(s) in {WEIGHT_PATH} all valid.")


if __name__ == "__main__":
    main()
