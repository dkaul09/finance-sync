"""Transport-shape tests that need no credentials."""

from finance_sync.google_backend import SCOPES, column_letter


def test_column_letters():
    assert column_letter(0) == "A"
    assert column_letter(12) == "M"   # the 13th Transactions column
    assert column_letter(25) == "Z"
    assert column_letter(26) == "AA"


def test_scopes_are_least_privilege():
    """drive.file keeps the sync out of the rest of the user's Drive."""
    assert "https://www.googleapis.com/auth/drive.file" in SCOPES
    assert "https://www.googleapis.com/auth/drive" not in SCOPES


def test_only_non_sensitive_scopes_are_requested():
    """`spreadsheets` is a *sensitive* scope and would force a verification
    review plus a privacy policy; drive.file already covers the Sheets calls."""
    assert SCOPES == ["https://www.googleapis.com/auth/drive.file"]
