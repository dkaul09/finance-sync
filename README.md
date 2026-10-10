# finance-sync

Pulls transactions from SimpleFIN daily and writes them into per-month Google
Sheets, categorized. Runs in the cloud on a schedule; your laptop does not need
to be on.

## What it does

- One spreadsheet per month, `Finances YYYY-MM`, in a Drive folder called
  `Finances`. Both are created automatically if missing, so month rollover
  needs no intervention.
- Four tabs per spreadsheet: `Transactions`, `Budget`, `Review`, `Overrides`.
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

3. **Configure the consent screen** — Google renamed this; there is no longer a
   single "OAuth consent screen" wizard. Go to
   [Google Auth Platform](https://console.cloud.google.com/auth/overview)
   (console menu → *APIs & Services* → *Google Auth Platform*) and click
   **Get started**, then work through the four panels:

   | Panel | What to enter |
   |---|---|
   | App Information | App name (anything, e.g. `finance-sync`); support email: your own |
   | Audience | **External** |
   | Contact Information | your own email |
   | Finish | tick *I agree to the Google API Services: User Data Policy* → **Create** |

   Afterwards the platform shows these sections in the left nav: **Overview**,
   **Branding**, **Audience**, **Data Access**, **Clients**.

   - **Branding** → only three fields are required. Fill these and leave
     everything else blank:

     | Field | Value | Required? |
     |---|---|---|
     | App name | `finance-sync` | **yes** |
     | User support email | your own address | **yes** |
     | Developer contact information → Email addresses | your own address | **yes** |
     | App logo | — | no |
     | Application home page | — | no |
     | Privacy policy link | — | no |
     | Terms of service link | — | no |
     | Authorized domains | — | no |

     The domain/policy fields are only needed for apps requesting *sensitive*
     or *restricted* scopes, or serving other people. This app requests
     neither and serves only you, so leaving them empty is correct — and means
     you never need to host a privacy policy.

   - **Audience** → under *Test users* click **Add users**, add your own Google
     account, **Save**.
   - **Data Access** → leave empty. `scripts/auth.py` requests the single scope
     it needs at authorization time.

4. **Publish the app.** Still on
   [**Audience**](https://console.cloud.google.com/auth/audience), find
   *Publishing status: Testing* and click **Publish app**, then confirm. It
   should then read **In production**.

   > **Do not skip this.** While publishing status is *Testing*, Google issues
   > refresh tokens that expire after **7 days** on a fixed clock — not an
   > inactivity timer — so a scheduled sync dies every week with
   > `invalid_grant: Token has been expired or revoked`. In production, refresh
   > tokens do not expire unless revoked or unused for ~6 months.
   >
   > Publishing does **not** require Google verification review here: you are
   > the only user, and `drive.file`/`spreadsheets` are non-sensitive scopes.
   > You may see "Verification not required".

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

The app requests exactly one scope, `drive.file`, which covers both the Drive
and the Sheets calls — the Sheets API accepts it for files the app created.

That choice matters for setup, not just privacy. Google classifies `drive.file`
as **non-sensitive**, while the obvious-looking `spreadsheets` scope is
**sensitive**: requesting it would require a privacy policy, a hosted homepage
and a verification review, for no extra capability here.

It also means the sync can only ever see files it created, never the rest of
your Drive. The tradeoff is that it **cannot see a `Finances` folder you made by
hand** — let it create its own on the first run.

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

### Jev (optional, `USE_JEV=true`)

Only transactions the first two layers could not resolve reach Jev, so the
cost is bounded by how incomplete your rules are. Each one is a single
System One call carrying three questions evaluated in parallel:

- **category** — a `Choice` over the budget lines, each with a one-line
  definition. `sk` is deliberately not offered, since it is manual-only.
- **recurring** — a `Noul`: is this a recurring subscription?
- **business** — a `Noul`: does this look like a business expense?

Both Noul probabilities are written to their own columns on `Transactions`.

**Confidence gating.** `Choice` answers carry a calibrated `confidence`.
At or above `JEV_CONFIDENCE_THRESHOLD` (default `0.8`) the category is
written straight to `Transactions`. Below it, the transaction is written to
the `Review` tab instead with the proposed category and the score, and is
**not** added to `Transactions` — an uncertain guess never silently becomes a
fact. A transaction already on `Transactions` keeps the category it has.

If the Jev API fails, the run degrades to `miscl`/`none` and continues: a
categorization outage must not stop financial data reaching the sheet.

Matching normalizes punctuation, so one pattern matches both SimpleFIN's clean
`payee` (`Trader Joe's`) and the raw bank `description` (`TRADER JOE S #250`).
The **longest** matching pattern wins, which is what keeps `UBER EATS`
(restaurants) out of `UBER` (transit).

`Overrides` rows are **carried forward** into each new month's spreadsheet, so
hand-maintained rules survive rollover.

Categories match the budget lines: `groceries, laundry, protein_shake,
rent_and_insurance, mobile, personal, pets, utilities, travel, haircut, gpt,
starbucks_coffee, coffee_pods, outside_food, miscl, sk`, plus `income` and
`transfer`, which are **not** budget lines and are excluded from budget totals.

## Budget tab

`Planned` vs `Actual` per category, for that month.

- **Planned** is yours to edit. Syncs never overwrite it, and it carries
  forward into each new month's spreadsheet.
- **Actual** is recomputed every sync from the whole `Transactions` tab, so a
  partial sync can never produce a partial total.
- Actual counts **spending only** — positive amounts are ignored, so a refund
  or deposit cannot inflate a line — and **includes pending** transactions, so
  the number reflects today rather than lagging settlement.
- `income` and `transfer` are excluded, so payroll and a $5,000 withdrawal
  don't distort the totals.

A run that changes nothing does not rewrite the Budget tab.

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
