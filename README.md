# finance-sync

Pulls transactions from SimpleFIN daily and writes them into per-month Google
Sheets, categorized. Runs in the cloud on a schedule; your laptop does not need
to be on.

## What it does

- One spreadsheet per month, `Finances YYYY-MM`, in a Drive folder called
  `Finances`. Both are created automatically if missing, so month rollover
  needs no intervention.
- Three tabs per spreadsheet: `Transactions`, `Review`, `Overrides`.
- Transactions are filed by their **transaction date**, so a run on the 1st puts
  the previous month's stragglers in the previous month's sheet.
- Upserts on `transaction_id`. Running twice a day never duplicates; a pending
  transaction that settles updates its existing row.
- **A category you edit by hand is never overwritten.** Set `category_source`
  to `manual` (the `review` command does this for you) and later syncs touch
  only the financial fields.

## Status

| Stage | What | State |
|---|---|---|
| 1 | SimpleFIN client, models, tests | done, validated on live data |
| 2 | Sheets layer, create-if-missing, idempotent upsert | done |
| 3 | Rules categorization, `USE_JEV=false` | done |
| 4 | Jev behind the flag | not started |
| 5 | `review` command, scheduling, docs | not started |

## Setup

### 1. SimpleFIN

Sign in at [beta-bridge.simplefin.org](https://beta-bridge.simplefin.org),
connect your banks, and create a **Setup Token** for this app. Claim it once —
claiming is single-use and is the only time the Access URL is ever shown:

```bash
TOKEN='<paste setup token>'
curl -s -X POST -H 'Content-Length: 0' "$(printf '%s' "$TOKEN" | base64 -d)" \
  > ~/.simplefin-access
chmod 600 ~/.simplefin-access
head -c 8 ~/.simplefin-access   # expect: https://
```

Back the resulting URL up in your password manager. There is no page to look it
up again; losing it means generating a new setup token.

Verify:

```bash
python -m finance_sync.cli sync --days 7 --print-only
```

### 2. Google Cloud project

This is the only part that cannot be automated. Roughly 10 minutes, once.

1. **Create a project** at
   [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate).
   Name it anything, e.g. `finance-sync`.

2. **Enable two APIs.** With the project selected, visit each and click
   *Enable*:
   - [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
   - [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)

3. **Configure the OAuth consent screen**
   ([APIs & Services → OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)):
   - User type: **External**
   - App name: anything; support email: your own
   - Scopes: you can leave these blank here, the script requests them
   - Test users: add your own Google account

4. **Publish the app.** On the consent screen's summary page, click
   **Publish app** so the publishing status reads **In production**.

   > **Do not skip this.** While the app is in *Testing*, Google expires the
   > refresh token after **7 days** and the scheduled sync silently stops
   > working. You are the only user and the scopes are non-sensitive, so no
   > Google verification review is required.

5. **Create an OAuth client**
   ([APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)):
   - *Create credentials → OAuth client ID*
   - Application type: **Desktop app**
   - Download the JSON.

6. **Mint a refresh token** (once, locally — it opens a browser):

   ```bash
   pip install google-auth-oauthlib google-api-python-client
   python scripts/auth.py ~/Downloads/client_secret_*.json
   ```

   Google will warn that the app isn't verified; choose *Advanced → Go to
   <app> (unsafe)*. That warning is about your own app.

   The script prints three values. Put them in `.env` locally (it is
   gitignored) and later into GitHub Actions secrets:

   ```
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   GOOGLE_REFRESH_TOKEN=...
   ```

#### Why OAuth and not a service account

Service accounts have no Drive storage quota and cannot own files, so creating
the monthly spreadsheet in a personal `My Drive` fails with
`403 storageQuotaExceeded`. The documented alternatives are a Shared Drive
(Google Workspace only) or acting on behalf of a user. Automatic month rollover
requires file *creation*, so this uses an OAuth refresh token.

#### Scope note

The app requests `drive.file`, not full `drive`: it can only see files it
created, and never the rest of your Drive. The tradeoff is that it **cannot see
a `Finances` folder you made by hand** — let it create its own on the first run.

### 3. Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored; never commit
it.

| Variable | Default | Meaning |
|---|---|---|
| `SIMPLEFIN_ACCESS_URL_PATH` | `~/.simplefin-access` | File holding the access URL |
| `SIMPLEFIN_ACCESS_URL` | — | The URL directly; used in CI instead of the file |
| `DRIVE_FOLDER_NAME` | `Finances` | Drive folder to keep spreadsheets in |
| `USE_JEV` | `false` | Enable the Jev categorization layer (stage 4) |
| `JEV_CONFIDENCE_THRESHOLD` | `0.8` | Below this, a transaction goes to `Review` |
| `TYPESAFE_API_KEY` | — | Jev API key |
| `GOOGLE_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN` | — | From step 2 |

## Usage

```bash
# Current month, write to Sheets
python -m finance_sync.cli sync

# See what would change, write nothing
python -m finance_sync.cli sync --dry-run

# Just print transactions; skip Sheets entirely
python -m finance_sync.cli sync --print-only

# Rehearse the whole pipeline against an in-memory sheet (no Google needed)
python -m finance_sync.cli sync --fake-sheets

# Historical backfill, capped at 90 days (2 requests)
python -m finance_sync.cli backfill --days 90
```

Every run prints `fetched / inserted / updated / unchanged / routed_to_review`
plus a breakdown of which categories came from where.

## Categorization

Three layers, cheapest first, first match wins:

1. **`Overrides` tab** — `pattern`, `category`, `is_regex`. Hand-maintained,
   always wins. Source: `override`.
2. **Built-in rules** — a seed merchant dictionary. Source: `rules`.
3. **Categorizer** — `NullCategorizer` by default (resolves nothing); Jev when
   `USE_JEV=true`. Source: `jev`.

Matching normalizes punctuation, so one pattern matches both SimpleFIN's clean
`payee` (`Trader Joe's`) and the raw bank `description` (`TRADER JOE S #250`).
The **longest** matching pattern wins, which is what keeps `UBER EATS`
(restaurants) out of `UBER` (transit).

`Overrides` rows are **carried forward** into each new month's spreadsheet, so
hand-maintained rules survive rollover.

Categories: `groceries, restaurants, coffee, travel, transit, shopping,
utilities, rent, subscriptions, health, entertainment, income, transfer, fees,
other`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest
```

The whole suite runs with no network and no credentials: the Sheets API sits
behind a narrow `SheetsBackend` protocol with an in-memory implementation, and
the upsert logic is a pure function over rows.

## Rate limits

SimpleFIN allows 24 requests/day. One scheduled run uses 1; a 90-day backfill
uses 2. There are no retry or polling loops.
