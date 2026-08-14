"""
exchange_oauth_code.py

One-off helper for issue #16: exchanges a Strava OAuth authorization code
for an access token, so the manual part of the OAuth flow is reduced to
just registering the API app and clicking through the consent screen.

Usage, after registering an app at https://www.strava.com/settings/api and
visiting (with your own client_id):

    https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost/exchange_token&approval_prompt=force&scope=activity:read_all

...clicking Authorize, and copying the `code` param from the (failed-to-load,
that's expected) localhost redirect URL:

    python exchange_oauth_code.py --client-id ... --client-secret ... --code ...

Reads --client-id/--client-secret from CLI args if given, otherwise from
STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET in the repo-root .env. Prints the
resulting access_token -- add it to .env as STRAVA_ACCESS_TOKEN for
verify_activity_id_alignment.py to use.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

from _env import load_env

TOKEN_URL = "https://www.strava.com/oauth/token"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--code", required=True, help="The `code` query-param from the OAuth redirect URL")
    args = parser.parse_args()

    env = load_env()
    client_id = args.client_id or env.get("STRAVA_CLIENT_ID")
    client_secret = args.client_secret or env.get("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing client_id/client_secret. Pass --client-id/--client-secret, "
            "or put STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET in .env first."
        )

    body = json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": args.code,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Token exchange failed: HTTP {e.code} -- {e.read().decode('utf-8', 'replace')}")

    print("Exchange succeeded. Add this to .env:\n")
    print(f"STRAVA_ACCESS_TOKEN={payload['access_token']}")
    print(f"\n(expires at {payload.get('expires_at')} -- a fresh one-off token, plenty for a single verification run)")


if __name__ == "__main__":
    main()
