# Privacy Policy — finance-sync

_Last updated: 9 October 2026_

## What this application is

`finance-sync` is a personal, single-user tool that copies the operator's own
bank transaction data into the operator's own Google Sheets. It is not a
product, it is not offered to other people, and it has no users other than the
person who installed and configured it.

## What data it accesses

With the operator's authorization, the application uses one Google OAuth scope:

- `https://www.googleapis.com/auth/drive.file` — per-file access, limited to
  files this application itself creates.

Under this scope the application can only see the spreadsheets and the folder
it created. It cannot read, modify, or list any other file in the operator's
Google Drive.

It also reads transaction data (dates, amounts, merchant descriptions, and
account names) from the operator's own financial institutions through
[SimpleFIN](https://beta-bridge.simplefin.org), using credentials the operator
supplied.

## What it does with that data

Transaction records are written into monthly Google Sheets spreadsheets inside
a folder in the operator's own Google Drive. Each transaction is assigned a
spending category.

## What it does not do

- It does not sell, share, or transmit data to any third party.
- It does not use data for advertising or profiling.
- It does not transfer data to anyone other than the operator's own Google
  account and, when explicitly enabled, the categorization service described
  below.
- It has no analytics, no telemetry, and no backend server that retains data.

## Third-party processing

If the optional categorization feature is enabled, a transaction's merchant
description, amount, account name, and date are sent to
[TypeSafe AI](https://typesafe.ai) solely to obtain a spending category. No
account numbers or credentials are sent. This feature is disabled by default.

## Storage and retention

Transaction data is stored only in the operator's own Google Sheets and is
retained for as long as the operator keeps those spreadsheets. Credentials
(the SimpleFIN access URL and the Google OAuth refresh token) are stored only
in the operator's own environment and secret store. No copy of either the data
or the credentials is held anywhere else.

## Revoking access

The operator can revoke this application's access at any time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).
Doing so immediately and permanently ends its ability to access Google Drive or
Google Sheets.

## Contact

Questions about this policy should be directed to the repository owner through
the issue tracker of the repository hosting this file.
