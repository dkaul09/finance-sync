#!/usr/bin/env python3
"""Mint a Google OAuth refresh token. Run once, locally.

By default the token is written straight into `.env` (mode 600) so it never
has to be copied by hand or pasted anywhere. Pass --print to show the values
instead, e.g. to paste them into GitHub Actions secrets.

Prerequisites:
  1. A Google Cloud project with the Drive and Sheets APIs enabled.
  2. An OAuth client of type "Desktop app"; download its JSON.
  3. The consent screen published to "In production" — in "Testing" the
     refresh token expires after 7 days and the schedule silently breaks.

Usage:
  python scripts/auth.py                     # auto-find client JSON in ~/Downloads
  python scripts/auth.py path/to/client.json
  python scripts/auth.py --print             # don't touch .env
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finance_sync.google_backend import SCOPES  # noqa: E402

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def find_client_json(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"No such file: {path}")
        return path

    candidates = sorted(
        glob.glob(str(Path.home() / "Downloads" / "client_secret_*.json")),
        key=lambda p: Path(p).stat().st_mtime,
    )
    if not candidates:
        raise SystemExit(
            "No client_secret_*.json found in ~/Downloads. "
            "Pass the path explicitly, or download one from the Clients page."
        )
    return Path(candidates[-1])


def write_env(updates: dict[str, str]) -> None:
    """Merge values into .env without disturbing the other keys."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out) + "\n")
    ENV_PATH.chmod(0o600)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--print"]
    print_only = "--print" in sys.argv

    client_json = find_client_json(args[0] if args else None)
    print(f"Using client: {client_json.name}")
    print(f"Requesting scope: {' '.join(SCOPES)}\n")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit("pip install google-auth-oauthlib")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_json), SCOPES)
    print("Opening a browser to authorize. Expect an 'app isn't verified'")
    print("warning — that is your own app: Advanced -> Go to finance-sync.\n")

    # access_type=offline plus prompt=consent is what actually returns a
    # refresh token; a repeat authorization omits it without prompt=consent.
    credentials = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )

    if not credentials.refresh_token:
        raise SystemExit(
            "No refresh token returned. Revoke this app at "
            "myaccount.google.com/permissions and run again."
        )

    values = {
        "GOOGLE_CLIENT_ID": credentials.client_id,
        "GOOGLE_CLIENT_SECRET": credentials.client_secret,
        "GOOGLE_REFRESH_TOKEN": credentials.refresh_token,
    }

    if print_only:
        print("\nSet these as GitHub Actions secrets:\n")
        for key, value in values.items():
            print(f"{key}={value}")
        print("\nDo not commit these.")
        return 0

    write_env(values)
    print(f"\nWrote credentials to {ENV_PATH} (mode 600, gitignored).")
    print("Nothing to copy. Re-run with --print when you need the values")
    print("again for GitHub Actions secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
