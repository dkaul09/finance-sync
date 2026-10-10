"""Translate Google API failures into messages that say what to do."""

from __future__ import annotations

import json
import re


class GoogleSetupError(RuntimeError):
    """A Google API failure the user can fix, with instructions attached."""


_API_NAMES = {
    "drive.googleapis.com": "Google Drive API",
    "sheets.googleapis.com": "Google Sheets API",
}


def explain_http_error(exc: Exception) -> str | None:
    """Return an actionable message for a googleapiclient HttpError, or None.

    Raw HttpError tracebacks bury the one sentence that matters, and the most
    common failures here all have a specific fix.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")

    reason = ""
    message = ""
    try:
        payload = json.loads(content)
        error = payload.get("error", {})
        message = error.get("message", "")
        details = error.get("errors") or []
        if details:
            reason = details[0].get("reason", "")
    except (ValueError, AttributeError):
        message = content[:300]

    if reason == "accessNotConfigured" or "has not been used in project" in message:
        api_host = next((h for h in _API_NAMES if h in message), None)
        api_name = _API_NAMES.get(api_host or "", "the required API")
        project = ""
        found = re.search(r"project (\d+)", message)
        if found:
            project = found.group(1)
        url = (
            f"https://console.cloud.google.com/apis/library/{api_host}"
            f"?project={project}" if api_host else
            "https://console.cloud.google.com/apis/library"
        )
        return (
            f"{api_name} is not enabled for this Google Cloud project"
            f"{f' ({project})' if project else ''}.\n"
            f"  Enable it here, then retry (it can take a minute to propagate):\n"
            f"  {url}"
        )

    if status == 403 and "storageQuotaExceeded" in content:
        return (
            "Drive refused to create the file: storage quota exceeded.\n"
            "  This is the service-account failure mode. Confirm the credentials "
            "in .env are an OAuth refresh token, not a service account key."
        )

    if status == 403 and reason in {"insufficientPermissions", "forbidden"}:
        return (
            "Google rejected the request as unauthorized for this scope.\n"
            "  Re-run `python scripts/auth.py` to re-consent with the current scope."
        )

    if status == 401 or "invalid_grant" in content:
        return (
            "The Google refresh token is invalid or expired.\n"
            "  The usual cause is an OAuth consent screen still in 'Testing', "
            "which expires refresh tokens after 7 days.\n"
            "  Publish the app at https://console.cloud.google.com/auth/audience, "
            "then re-run `python scripts/auth.py`."
        )

    if status == 429 or reason == "rateLimitExceeded":
        return "Google rate-limited the request. Wait a minute and retry."

    return None
