"""
verify_activity_id_alignment.py

One-off check for issue #16: are the `id` values already on
data/processed/*.json (populated from the bulk export) the same as the
`id` values the live Strava API returns for the same activities?

If they're not, the reconciliation planned for #23 (list what Strava has ->
diff against local -> fetch only the delta) will match nothing on the first
sync and treat all 2,720 existing records as brand-new.

Usage, once STRAVA_ACCESS_TOKEN is in the repo-root .env (see
exchange_oauth_code.py for how to get one):

    python verify_activity_id_alignment.py

Fetches one page of GET /api/v3/athlete/activities, saves the raw response
to ingestion/strava/_id_verification/ (gitignored -- personal activity
data, and this is a throwaway diagnostic, not a pipeline artifact), and
cross-checks each returned id against data/processed/<id>.json.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from _env import load_env

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
OUTPUT_DIR = Path(__file__).resolve().parent / "_id_verification"
API_URL = "https://www.strava.com/api/v3/athlete/activities?per_page=30"

MIN_MATCHES_REQUIRED = 3  # per the issue's Acceptance Criteria


def fetch_page(token: str) -> list[dict]:
    req = urllib.request.Request(API_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 401:
            raise SystemExit(f"HTTP 401 -- token missing/expired/wrong scope. Response: {detail}")
        raise SystemExit(f"HTTP {e.code} fetching activities: {detail}")


def main() -> None:
    token = load_env().get("STRAVA_ACCESS_TOKEN")
    if not token:
        raise SystemExit(
            "STRAVA_ACCESS_TOKEN not found in .env. See exchange_oauth_code.py "
            "(or the issue #16 comment) for how to get one."
        )

    activities = fetch_page(token)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "api_page_1.json"
    out_path.write_text(json.dumps(activities, indent=2), encoding="utf-8")
    print(f"Saved {len(activities)} activities to {out_path}\n")

    if not activities:
        raise SystemExit("API returned zero activities -- nothing to check. Is this the right account?")

    matched, unmatched = [], []
    for a in activities:
        api_id = str(a["id"])
        local_path = PROCESSED_DIR / f"{api_id}.json"
        entry = {"id": api_id, "name": a.get("name"), "start_date": a.get("start_date")}
        (matched if local_path.exists() else unmatched).append(entry)

    print(f"Checked {len(activities)} activities from the API's most recent page.")
    print(f"  Matched a local data/processed/<id>.json file:     {len(matched)}")
    print(f"  No local file with that id:                        {len(unmatched)}\n")

    if matched:
        print("Matches (id, name, date):")
        for m in matched[:10]:
            print(f"  {m['id']}  {m['start_date']}  {m['name']}")
        print()

    if unmatched:
        print("No local match (expected for activities newer than the bulk export; a red flag only if ALL are unmatched):")
        for u in unmatched[:10]:
            print(f"  {u['id']}  {u['start_date']}  {u['name']}")
        print()

    verdict = "IDs match" if len(matched) >= MIN_MATCHES_REQUIRED else "IDs do not match"
    print(f"VERDICT: {verdict}")
    if len(matched) < MIN_MATCHES_REQUIRED:
        print(
            f"(Fewer than {MIN_MATCHES_REQUIRED} matches found. If data/processed/ genuinely has "
            "overlapping-date activities that still didn't match by id, that's the mismatch signal "
            "the issue is checking for -- file the follow-up issue per the AC. If instead all "
            "fetched activities are simply newer than the bulk export's cutoff, re-run after "
            "confirming the API page includes an older, already-ingested activity."
        )


if __name__ == "__main__":
    main()
