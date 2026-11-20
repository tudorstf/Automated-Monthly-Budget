"""Google Drive API v3 client for watching and cleaning up the `receipts` folder."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import google_auth_httplib2
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
import httplib2

from src.config import SUPPORTED_EXTENSIONS, SUPPORTED_MIME_TYPES, Settings
from src.default_oauth_client import get_oauth_client_config, has_oauth_credentials

logger = logging.getLogger(__name__)

# Allow Google OAuth to return additional previously granted scopes (e.g., incremental consent)
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Full Drive scope is required so files uploaded by mobile/web can be permanently deleted
DRIVE_SCOPES: list[str] = ["https://www.googleapis.com/auth/drive"]

# In-memory store for PKCE code_verifier and redirect_uri keyed by OAuth state during web login
_PENDING_OAUTH_STATES: dict[str, dict[str, Any]] = {}
_OAUTH_STATES_LOCK = threading.Lock()


class DriveAuthError(RuntimeError):
    """Raised when Google Drive OAuth credentials or tokens are missing/invalid."""


@dataclass(frozen=True)
class DriveFile:
    """Metadata for a pending receipt file in Google Drive."""

    id: str
    name: str
    mime_type: str
    size: int
    created_time: str
    md5_checksum: str = ""

    @property
    def extension(self) -> str:
        ext = Path(self.name).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            return ext
        return SUPPORTED_MIME_TYPES.get(self.mime_type.lower(), ".jpg")


class GoogleDriveClient:
    """Wrapper around Google Drive API v3 for polling, downloading, and deleting receipts."""

    def __init__(self, settings: Settings, service: Any | None = None) -> None:
        self.settings = settings
        self._service = service
        self._folder_id: str | None = None

    def authenticate(self, *, interactive: bool = False) -> Any:
        """Load, refresh, or interactively obtain OAuth2 credentials and build Drive service."""
        if self._service is not None:
            return self._service

        creds: Credentials | None = None
        token_path = self.settings.token_path
        credentials_path = self.settings.credentials_path

        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load existing token.json: %s", exc)
                creds = None

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(token_path, creds)
            except RefreshError as exc:
                if not interactive:
                    raise DriveAuthError(
                        "Google Drive OAuth token expired and could not refresh. "
                        "Run `python main.py --setup` to re-authenticate."
                    ) from exc
                creds = None

        if not creds or not creds.valid:
            if not interactive:
                raise DriveAuthError(
                    "Google Drive authentication required. Run `python main.py --setup` first."
                )
            client_config, _ = get_oauth_client_config(credentials_path)
            if not has_oauth_credentials(client_config):
                raise DriveAuthError(
                    "Google Drive OAuth client is not configured. Save credentials.json or set "
                    "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env."
                )
            flow = InstalledAppFlow.from_client_config(client_config, DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
            self._save_token(token_path, creds)

        authorized_http = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(timeout=30)
        )
        self._service = build("drive", "v3", http=authorized_http, cache_discovery=False)
        return self._service

    def has_valid_token(self) -> bool:
        """Return True if a Drive service is active or a valid/refreshable OAuth token exists."""
        if self._service is not None:
            return True
        token_path = self.settings.token_path
        if not token_path.exists():
            return False
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self._save_token(token_path, creds)
            return bool(creds and creds.valid)
        except Exception:  # noqa: BLE001
            return False

    def create_web_authorization_url(self, redirect_uri: str) -> tuple[str, str]:
        """Generate a Google OAuth consent URL for web login and cache PKCE state."""
        client_config, _ = get_oauth_client_config(self.settings.credentials_path)
        if not has_oauth_credentials(client_config):
            raise DriveAuthError(
                "Google Drive OAuth client is not configured. Upload credentials.json under "
                "Advanced in Step 2, or set GOOGLE_OAUTH_CLIENT_ID and "
                "GOOGLE_OAUTH_CLIENT_SECRET in .env."
            )
        flow = InstalledAppFlow.from_client_config(
            client_config,
            DRIVE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        with _OAUTH_STATES_LOCK:
            _PENDING_OAUTH_STATES[state] = {
                "redirect_uri": redirect_uri,
                "code_verifier": getattr(flow, "code_verifier", None),
            }
            while len(_PENDING_OAUTH_STATES) > 50:
                oldest_state = next(iter(_PENDING_OAUTH_STATES))
                _PENDING_OAUTH_STATES.pop(oldest_state, None)
        return auth_url, state

    def fetch_token_from_callback(
        self,
        *,
        redirect_uri: str,
        code: str,
        state: str | None = None,
    ) -> None:
        """Exchange the OAuth callback code for tokens and persist `token.json`."""
        if not state or not state.strip():
            raise DriveAuthError("Missing OAuth state parameter in callback.")

        with _OAUTH_STATES_LOCK:
            saved = _PENDING_OAUTH_STATES.pop(state, None)

        if not saved:
            raise DriveAuthError(
                "Invalid or expired OAuth state. Please start Google Drive sign-in again."
            )

        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        effective_redirect = saved.get("redirect_uri") or redirect_uri
        client_config, _ = get_oauth_client_config(self.settings.credentials_path)
        flow = InstalledAppFlow.from_client_config(
            client_config,
            DRIVE_SCOPES,
            redirect_uri=effective_redirect,
            state=state,
        )
        if saved.get("code_verifier"):
            flow.code_verifier = saved["code_verifier"]
        flow.fetch_token(code=code)
        self._save_token(self.settings.token_path, flow.credentials)
        self._service = None
        self._folder_id = None

    def get_account_info(self) -> dict[str, Any]:
        """Fetch connected Google account metadata and watched Drive folder details."""
        _, oauth_source = get_oauth_client_config(self.settings.credentials_path)
        disconnected: dict[str, Any] = {
            "connected": False,
            "email": "",
            "display_name": "",
            "folder_name": self.settings.drive_folder_name,
            "folder_ids": [],
            "folder_url": "",
            "oauth_source": oauth_source,
        }
        if self._service is None and not self.has_valid_token():
            return disconnected

        try:
            service = self._get_service()
            about = service.about().get(fields="user(displayName,emailAddress)").execute()
            user = about.get("user", {})
            folder_ids = self.ensure_receipts_folders()
            return {
                "connected": True,
                "email": user.get("emailAddress", ""),
                "display_name": user.get("displayName", ""),
                "folder_name": self.settings.drive_folder_name,
                "folder_ids": folder_ids,
                "folder_url": (
                    f"https://drive.google.com/drive/folders/{folder_ids[0]}"
                    if folder_ids
                    else ""
                ),
                "oauth_source": oauth_source,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to retrieve Google Drive account info: %s", exc)
            return {**disconnected, "error": str(exc)}

    def disconnect(self) -> None:
        """Remove saved OAuth token and reset cached Drive service state."""
        self.settings.token_path.unlink(missing_ok=True)
        self._service = None
        self._folder_id = None

    @staticmethod
    def _save_token(token_path: Path, creds: Credentials) -> None:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass

    def _get_service(self) -> Any:
        if self._service is None:
            return self.authenticate(interactive=False)
        return self._service

    def ensure_receipts_folder(self) -> str:
        """Locate the primary `receipts` folder in Drive, creating it if absent."""
        folder_ids = self.ensure_receipts_folders()
        return folder_ids[0]

    def ensure_receipts_folders(self) -> list[str]:
        """Locate all non-trashed folders matching `drive_folder_name`, creating one if none exist."""
        service = self._get_service()
        folder_name = self.settings.drive_folder_name
        escaped_name = folder_name.replace("'", "\\'")
        query = (
            f"name = '{escaped_name}' "
            "and mimeType = 'application/vnd.google-apps.folder' "
            "and trashed = false"
        )
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name, parents)",
                pageSize=50,
            )
            .execute()
        )
        folders = response.get("files", [])
        if folders:
            folder_ids = [f["id"] for f in folders if f.get("id")]
            if self._folder_id not in folder_ids:
                self._folder_id = folder_ids[0]
                logger.info(
                    "Using Google Drive folder(s) '%s': %s",
                    folder_name,
                    ", ".join(folder_ids),
                )
            return folder_ids

        metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        created = service.files().create(body=metadata, fields="id, name").execute()
        self._folder_id = created["id"]
        logger.info("Created Google Drive folder '%s' (id=%s)", folder_name, self._folder_id)
        return [self._folder_id]

    def list_pending_files(self) -> list[DriveFile]:
        """List non-trashed receipt images and PDFs across all matching Drive folders."""
        folder_ids = self.ensure_receipts_folders()
        service = self._get_service()

        parent_clause = " or ".join(f"'{fid}' in parents" for fid in folder_ids)
        query = (
            f"({parent_clause}) "
            "and trashed = false "
            "and mimeType != 'application/vnd.google-apps.folder'"
        )
        pending: list[DriveFile] = []
        seen_ids: set[str] = set()
        page_token: str | None = None

        while True:
            try:
                response = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        orderBy="createdTime asc",
                        fields="nextPageToken, files(id, name, mimeType, size, createdTime, md5Checksum)",
                        pageSize=100,
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as exc:
                if getattr(exc, "resp", None) and exc.resp.status == 404:
                    self._folder_id = None
                raise

            for item in response.get("files", []):
                file_id = item.get("id", "")
                if not file_id or file_id in seen_ids:
                    continue
                name = item.get("name", "")
                mime_type = item.get("mimeType", "")
                ext = Path(name).suffix.lower()
                if mime_type.lower() in SUPPORTED_MIME_TYPES or ext in SUPPORTED_EXTENSIONS:
                    seen_ids.add(file_id)
                    try:
                        size = int(item.get("size", 0) or 0)
                    except (TypeError, ValueError):
                        size = 0
                    pending.append(
                        DriveFile(
                            id=file_id,
                            name=name,
                            mime_type=mime_type,
                            size=size,
                            created_time=item.get("createdTime", ""),
                            md5_checksum=item.get("md5Checksum", ""),
                        )
                    )

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return pending

    def download_file(self, file_id: str, dest_path: Path) -> Path:
        """Download a file from Google Drive to `dest_path`."""
        service = self._get_service()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        request = service.files().get_media(fileId=file_id)

        with dest_path.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return dest_path

    def delete_file_permanently(self, file_id: str) -> None:
        """Permanently delete a file from Google Drive (bypassing Trash)."""
        service = self._get_service()
        try:
            service.files().delete(fileId=file_id).execute()
        except HttpError as exc:
            if getattr(exc, "resp", None) and exc.resp.status == 404:
                logger.warning("Drive file %s was already deleted.", file_id)
                return
            raise
