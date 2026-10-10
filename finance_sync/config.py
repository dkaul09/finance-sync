"""Configuration, loaded from the environment with a .env fallback."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ACCESS_URL_PATH = "~/.simplefin-access"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Avoids a dependency and never overrides real env."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True, slots=True)
class Config:
    access_url: str
    drive_folder_name: str = "Finances"
    use_jev: bool = False
    jev_confidence_threshold: float = 0.8
    typesafe_api_key: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None

    @classmethod
    def load(cls, dotenv: Path | None = None) -> Config:
        _load_dotenv(dotenv or Path(".env"))
        return cls(
            access_url=_resolve_access_url(),
            drive_folder_name=os.environ.get("DRIVE_FOLDER_NAME", "Finances"),
            use_jev=_env_bool("USE_JEV", False),
            jev_confidence_threshold=float(
                os.environ.get("JEV_CONFIDENCE_THRESHOLD", "0.8")
            ),
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY"),
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID"),
            google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
            google_refresh_token=os.environ.get("GOOGLE_REFRESH_TOKEN"),
        )


def _resolve_access_url() -> str:
    """Read the SimpleFIN access URL, preferring the env var used in CI.

    Order: `SIMPLEFIN_ACCESS_URL` (a GitHub Actions secret), then the file at
    `SIMPLEFIN_ACCESS_URL_PATH`. The value is never logged.
    """
    from_env = os.environ.get("SIMPLEFIN_ACCESS_URL")
    if from_env and from_env.strip():
        return from_env.strip()

    path = Path(
        os.environ.get("SIMPLEFIN_ACCESS_URL_PATH", DEFAULT_ACCESS_URL_PATH)
    ).expanduser()
    if not path.is_file():
        raise ConfigError(
            f"No SimpleFIN access URL: set SIMPLEFIN_ACCESS_URL or create {path}"
        )
    value = path.read_text().strip()
    if not value:
        raise ConfigError(f"{path} is empty")
    return value
