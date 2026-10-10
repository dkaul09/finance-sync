"""Google failures must arrive as instructions, not tracebacks."""

from __future__ import annotations

import json

from finance_sync.errors import explain_http_error


class FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status


class FakeHttpError(Exception):
    def __init__(self, status: int, payload: dict) -> None:
        self.resp = FakeResp(status)
        self.content = json.dumps(payload).encode()


def api_disabled(host: str = "drive.googleapis.com") -> FakeHttpError:
    return FakeHttpError(403, {
        "error": {
            "message": (
                f"Google Drive API has not been used in project 1043626242023 "
                f"before or it is disabled. Enable it by visiting "
                f"https://console.developers.google.com/apis/api/{host}/overview"
            ),
            "errors": [{"reason": "accessNotConfigured"}],
        }
    })


class TestApiNotEnabled:
    def test_it_names_the_api(self):
        assert "Google Drive API" in explain_http_error(api_disabled())

    def test_it_includes_the_project_number(self):
        assert "1043626242023" in explain_http_error(api_disabled())

    def test_it_gives_a_console_link(self):
        message = explain_http_error(api_disabled())
        assert "console.cloud.google.com/apis/library/drive.googleapis.com" in message

    def test_it_recognises_the_sheets_api_too(self):
        exc = FakeHttpError(403, {"error": {
            "message": "Google Sheets API has not been used in project 123 before"
                       " or it is disabled. sheets.googleapis.com",
            "errors": [{"reason": "accessNotConfigured"}]}})
        assert "Google Sheets API" in explain_http_error(exc)


class TestOtherFailures:
    def test_storage_quota_points_at_the_service_account_trap(self):
        exc = FakeHttpError(403, {"error": {
            "message": "storageQuotaExceeded",
            "errors": [{"reason": "storageQuotaExceeded"}]}})
        assert "service-account" in explain_http_error(exc)

    def test_invalid_grant_explains_the_testing_mode_expiry(self):
        exc = FakeHttpError(401, {"error": {"message": "invalid_grant"}})
        message = explain_http_error(exc)
        assert "Testing" in message and "7 days" in message

    def test_rate_limit(self):
        exc = FakeHttpError(429, {"error": {
            "message": "rate", "errors": [{"reason": "rateLimitExceeded"}]}})
        assert "rate-limited" in explain_http_error(exc)

    def test_an_unrecognised_error_is_not_swallowed(self):
        """Unknown failures must re-raise so bugs stay visible."""
        exc = FakeHttpError(500, {"error": {"message": "kaboom"}})
        assert explain_http_error(exc) is None

    def test_a_non_http_exception_is_not_swallowed(self):
        assert explain_http_error(ValueError("nope")) is None
