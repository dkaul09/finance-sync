#!/usr/bin/env python3
"""Mint a Google OAuth refresh token. Run once, locally.

The printed refresh token goes into a GitHub Actions secret
(GOOGLE_REFRESH_TOKEN) so the scheduled sync never needs this machine again.

Prerequisites:
  1. A Google Cloud project with the Drive and Sheets APIs enabled.
  2. An OAuth client of type "Desktop app"; download its JSON.
  3. The OAuth consent screen set to "In production" — in "Testing" the
     refresh token expires after 7 days and the cron silently breaks.

Usage:
  pip install google-auth-oauthlib
  python scripts/auth.py ~/Downloads/client_secret_xxx.json
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finance_sync.google_backend import SCOPES  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    secrets_path = Path(sys.argv[1]).expanduser()
    if not secrets_path.is_file():
        print(f"No such file: {secrets_path}")
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip install google-auth-oauthlib")
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    # access_type=offline + prompt=consent is what actually returns a refresh
    # token; without prompt=consent a repeat authorization omits it.
    credentials = flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )

    if not credentials.refresh_token:
        print("No refresh token returned. Revoke the app's access and retry.")
        return 1

    print("\nSet these three secrets (GitHub repo -> Settings -> Secrets):\n")
    print(f"GOOGLE_CLIENT_ID={credentials.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={credentials.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={credentials.refresh_token}")
    print("\nDo not commit these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
