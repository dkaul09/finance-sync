"""In-memory SheetsBackend for tests and --dry-run rehearsals.

Behaves like the real thing for the operations the sync uses, including
append-after-last-row semantics, so idempotency tests mean something.
"""

from __future__ import annotations

from .sheets import SheetsBackend


class FakeSheetsBackend(SheetsBackend):
    def __init__(self) -> None:
        self.folders: dict[str, str] = {}
        # spreadsheet_id -> {tab -> rows}
        self.spreadsheets: dict[str, dict[str, list[list[str]]]] = {}
        self.titles: dict[str, str] = {}
        self.parents: dict[str, str] = {}
        self.created_spreadsheets: list[str] = []
        self.call_log: list[str] = []

    def ensure_folder(self, name: str) -> str:
        self.call_log.append(f"ensure_folder:{name}")
        if name not in self.folders:
            self.folders[name] = f"folder-{len(self.folders) + 1}"
        return self.folders[name]

    def find_spreadsheet(self, folder_id: str, title: str) -> str | None:
        self.call_log.append(f"find:{title}")
        for sid, stitle in self.titles.items():
            if stitle == title and self.parents.get(sid) == folder_id:
                return sid
        return None

    def create_spreadsheet(self, folder_id: str, title: str, tabs: dict[str, list[str]]) -> str:
        self.call_log.append(f"create:{title}")
        sid = f"sheet-{len(self.spreadsheets) + 1}"
        self.spreadsheets[sid] = {tab: [list(header)] for tab, header in tabs.items()}
        self.titles[sid] = title
        self.parents[sid] = folder_id
        self.created_spreadsheets.append(title)
        return sid

    def list_spreadsheets(self, folder_id: str) -> list[tuple[str, str]]:
        self.call_log.append(f"list:{folder_id}")
        return [
            (title, sid)
            for sid, title in self.titles.items()
            if self.parents.get(sid) == folder_id
        ]

    def read_tab(self, spreadsheet_id: str, tab: str) -> list[list[str]]:
        self.call_log.append(f"read:{spreadsheet_id}:{tab}")
        return [list(r) for r in self.spreadsheets[spreadsheet_id].get(tab, [])]

    def update_rows(self, spreadsheet_id: str, tab: str, updates: list[tuple[int, list[str]]]) -> None:
        self.call_log.append(f"update:{spreadsheet_id}:{tab}:{len(updates)}")
        rows = self.spreadsheets[spreadsheet_id][tab]
        for row_number, values in updates:
            index = row_number - 1
            while len(rows) <= index:
                rows.append([])
            rows[index] = list(values)

    def append_rows(self, spreadsheet_id: str, tab: str, rows: list[list[str]]) -> None:
        self.call_log.append(f"append:{spreadsheet_id}:{tab}:{len(rows)}")
        self.spreadsheets[spreadsheet_id].setdefault(tab, []).extend(
            [list(r) for r in rows]
        )

    # -- test helpers ----------------------------------------------------
    def tab(self, title: str, tab: str) -> list[list[str]]:
        sid = next(s for s, t in self.titles.items() if t == title)
        return self.spreadsheets[sid][tab]

    def data_rows(self, title: str, tab: str = "Transactions") -> list[list[str]]:
        return self.tab(title, tab)[1:]

    def write_calls(self) -> list[str]:
        return [c for c in self.call_log if c.startswith(("update:", "append:"))]
