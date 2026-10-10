"""The real SheetsBackend, over the Drive and Sheets REST APIs.

Auth is an OAuth refresh token, not a service account: service accounts have no
Drive storage quota and cannot own files, so creating the monthly spreadsheet in
a personal My Drive fails with 403 storageQuotaExceeded. Auto month-rollover
needs file *creation*, so this acts on behalf of the user.

Google imports are deferred to call time so the rest of the package (and the
whole test suite) runs without the client libraries installed.
"""

from __future__ import annotations

import logging
import string

from .sheets import SheetsBackend

logger = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"

#: `drive.file` alone is deliberate, and it is the only scope requested.
#:
#: It covers both the Drive calls and the Sheets calls, because the Sheets API
#: accepts drive.file for files the app itself created. Google classifies it as
#: non-sensitive, whereas `spreadsheets` is *sensitive* — requesting that would
#: drag the consent screen into needing a privacy policy, a hosted homepage and
#: a verification review, for no added capability here.
#:
#: It also grants access only to files this app created, so the sync can never
#: read the rest of the user's Drive. The consequence is that a pre-existing
#: hand-made "Finances" folder is invisible to it — let the tool create its own
#: folder once and it will find it on every later run.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def column_letter(index: int) -> str:
    """0-based column index -> spreadsheet column letters (0 -> A, 26 -> AA)."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = string.ascii_uppercase[remainder] + letters
    return letters


def build_credentials(
    client_id: str, client_secret: str, refresh_token: str
):  # pragma: no cover - requires google libs
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )


class GoogleSheetsBackend(SheetsBackend):  # pragma: no cover - network transport
    """Drive + Sheets transport. Two write calls per spreadsheet per run."""

    def __init__(self, credentials) -> None:
        from googleapiclient.discovery import build

        self._drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    # -- Drive ------------------------------------------------------------
    def find_folder(self, name: str) -> str | None:
        escaped = name.replace("'", "\\'")
        query = (
            f"name = '{escaped}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        response = (
            self._drive.files()
            .list(q=query, fields="files(id, name)", pageSize=10)
            .execute()
        )
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def ensure_folder(self, name: str) -> str:
        existing = self.find_folder(name)
        if existing is not None:
            return existing

        logger.info("Creating Drive folder %r", name)
        created = (
            self._drive.files()
            .create(body={"name": name, "mimeType": FOLDER_MIME}, fields="id")
            .execute()
        )
        return created["id"]

    def find_spreadsheet(self, folder_id: str, title: str) -> str | None:
        escaped = title.replace("'", "\\'")
        query = (
            f"name = '{escaped}' and mimeType = '{SPREADSHEET_MIME}' "
            f"and '{folder_id}' in parents and trashed = false"
        )
        response = (
            self._drive.files()
            .list(q=query, fields="files(id, name)", pageSize=10)
            .execute()
        )
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def list_spreadsheets(self, folder_id: str) -> list[tuple[str, str]]:
        query = (
            f"mimeType = '{SPREADSHEET_MIME}' and '{folder_id}' in parents "
            f"and trashed = false"
        )
        out: list[tuple[str, str]] = []
        page_token = None
        while True:
            response = (
                self._drive.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            out.extend((f["name"], f["id"]) for f in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return out

    def create_spreadsheet(
        self, folder_id: str, title: str, tabs: dict[str, list[str]]
    ) -> str:
        """Create the file in Drive (so the parent folder is right), then shape it."""
        created = (
            self._drive.files()
            .create(
                body={
                    "name": title,
                    "mimeType": SPREADSHEET_MIME,
                    "parents": [folder_id],
                },
                fields="id",
            )
            .execute()
        )
        spreadsheet_id = created["id"]

        tab_names = list(tabs)
        meta = self._sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        first_sheet = meta["sheets"][0]["properties"]

        requests: list[dict] = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": first_sheet["sheetId"], "title": tab_names[0]},
                    "fields": "title",
                }
            }
        ]
        requests += [{"addSheet": {"properties": {"title": name}}} for name in tab_names[1:]]
        # Freeze the header row on every tab.
        self._sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()

        meta = self._sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        freeze = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet["properties"]["sheetId"],
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            }
            for sheet in meta["sheets"]
        ]
        self._sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": freeze}
        ).execute()

        self._sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": f"{name}!A1", "values": [header]}
                    for name, header in tabs.items()
                ],
            },
        ).execute()

        logger.info("Created spreadsheet %r (%s)", title, spreadsheet_id)
        return spreadsheet_id

    # -- Sheets -----------------------------------------------------------
    def list_tabs(self, spreadsheet_id: str) -> list[str]:
        meta = (
            self._sheets.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def add_tab(self, spreadsheet_id: str, name: str, header: list[str]) -> None:
        self._sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": name,
                "gridProperties": {"frozenRowCount": 1},
            }}}]},
        ).execute()
        self._sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{name}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()

    def read_tab(self, spreadsheet_id: str, tab: str) -> list[list[str]]:
        response = (
            self._sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=tab)
            .execute()
        )
        return response.get("values", [])

    def update_rows(
        self, spreadsheet_id: str, tab: str, updates: list[tuple[int, list[str]]]
    ) -> None:
        if not updates:
            return
        data = []
        for row_number, values in updates:
            end = column_letter(len(values) - 1)
            data.append(
                {"range": f"{tab}!A{row_number}:{end}{row_number}", "values": [values]}
            )
        self._sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()

    def append_rows(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None:
        if not rows:
            return
        self._sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
