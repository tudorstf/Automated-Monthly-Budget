"""Built-in OAuth client configuration and custom credentials.json helpers."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_OAUTH_FIELDS: tuple[str, ...] = (
    "client_id",
    "client_secret",
    "auth_uri",
    "token_uri",
)

DEFAULT_INSTALLED_CLIENT_CONFIG: dict[str, Any] = {
    "installed": {
        "client_id": "",
        "project_id": "",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "",
        "redirect_uris": ["http://localhost"],
    }
}


def has_oauth_credentials(client_config: dict[str, Any]) -> bool:
    """Return True if the OAuth client config has non-empty client_id and client_secret."""
    section = _find_oauth_section(client_config)
    if section is None:
        return False
    return bool(
        isinstance(section.get("client_id"), str)
        and section["client_id"].strip()
        and isinstance(section.get("client_secret"), str)
        and section["client_secret"].strip()
    )


def _find_oauth_section(data: Any) -> dict[str, Any] | None:
    """Return the 'installed' or 'web' client config section if valid."""
    if not isinstance(data, dict):
        return None
    for key in ("installed", "web"):
        section = data.get(key)
        if isinstance(section, dict):
            return section
    return None


def get_oauth_client_config(credentials_path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Return OAuth client config dict and source tag.

    Uses `credentials_path` when it exists and contains valid Google OAuth JSON
    (`"installed"` or `"web"`), otherwise reads `GOOGLE_OAUTH_CLIENT_ID` and
    `GOOGLE_OAUTH_CLIENT_SECRET` from environment variables if set.
    """
    if credentials_path is not None:
        path = Path(credentials_path)
        if path.exists() and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if _find_oauth_section(data) is not None:
                    return data, "custom_credentials_json"
                logger.warning(
                    "Ignoring %s because it lacks a valid 'installed' or 'web' section.",
                    path,
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning("Could not parse custom credentials file %s: %s", path, exc)

    config = copy.deepcopy(DEFAULT_INSTALLED_CLIENT_CONFIG)
    env_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    env_client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    env_project_id = os.getenv("GOOGLE_OAUTH_PROJECT_ID", "").strip()
    if env_client_id and env_client_secret:
        config["installed"]["client_id"] = env_client_id
        config["installed"]["client_secret"] = env_client_secret
        config["installed"]["project_id"] = env_project_id
    return config, "builtin_default"


def validate_and_save_custom_credentials(credentials_path: Path, raw_bytes: bytes) -> dict[str, Any]:
    """Validate uploaded OAuth client JSON bytes and write atomically with 0o600 permissions."""
    if not raw_bytes:
        raise ValueError("Uploaded credentials.json file is empty.")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("credentials.json must be UTF-8 encoded JSON.") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in credentials.json: {exc}") from exc

    section = _find_oauth_section(data)
    if section is None:
        raise ValueError(
            "Invalid credentials.json: top-level 'installed' or 'web' object is required."
        )

    missing = [
        field
        for field in _REQUIRED_OAUTH_FIELDS
        if not isinstance(section.get(field), str) or not section[field].strip()
    ]
    if missing:
        raise ValueError(
            f"Invalid credentials.json: missing required field(s): {', '.join(missing)}."
        )

    target = Path(credentials_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    formatted = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(formatted)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    project_id = section.get("project_id", "")
    return {
        "source": "custom_credentials_json",
        "client_id": section["client_id"].strip(),
        "project_id": project_id.strip() if isinstance(project_id, str) else "",
    }
