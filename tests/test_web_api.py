"""API and integration tests for the FastAPI web dashboard in `src/web/api.py`."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from openpyxl import load_workbook
from PIL import Image
import pytest

import src.drive_client as drive_client_module
from src.config import MAIN_CATEGORIES, SUBCATEGORIES, SUBCATEGORY_TO_MAIN, Settings
from src.drive_client import DRIVE_SCOPES, DriveAuthError, DriveFile
from src.excel_writer import ExcelBudgetWriter
from src.models import ExtractedLineItem, ExtractedReceipt
from src.notifier import DesktopNotifier
from src.pipeline import ReceiptPipeline
from src.web.api import create_app
from src.web.watcher_manager import WatcherManager


_ENV_KEYS = (
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_FALLBACK_MODELS",
    "DRIVE_FOLDER_NAME",
    "POLL_INTERVAL_SECONDS",
    "EXCEL_FILE_PATH",
    "APP_HOST",
    "APP_PORT",
    "AUTO_START_WATCHER",
)


@pytest.fixture(autouse=True)
def _isolated_env_and_oauth_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with drive_client_module._OAUTH_STATES_LOCK:
        drive_client_module._PENDING_OAUTH_STATES.clear()
    yield
    with drive_client_module._OAUTH_STATES_LOCK:
        drive_client_module._PENDING_OAUTH_STATES.clear()


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "base_dir": tmp_path,
        "gemini_api_key": "",
        "gemini_model": "gemini-3.8-flash",
        "gemini_fallback_models": ("gemini-3.7-flash",),
        "drive_folder_name": "receipts",
        "poll_interval_seconds": 20,
        "excel_path": tmp_path / "budget_tracker.xlsx",
        "credentials_path": tmp_path / "credentials.json",
        "token_path": tmp_path / "token.json",
        "state_path": tmp_path / "data" / "processed_state.json",
        "failed_dir": tmp_path / "failed_receipts",
        "logs_dir": tmp_path / "logs",
        "app_host": "127.0.0.1",
        "app_port": 8765,
        "auto_start_watcher": False,
    }
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.ensure_directories()
    return settings


def _png_bytes(color: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 48), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _sample_receipt(
    *,
    store_name: str,
    receipt_date: str,
    total_amount: float,
    clean_name: str,
    main_category: str = "Groceries",
    subcategory: str = "Dairy & Eggs",
    discounts: float = 0.0,
    sgr: float = 0.0,
) -> ExtractedReceipt:
    items = [
        ExtractedLineItem(
            raw_name=clean_name.upper(),
            clean_name=clean_name,
            main_category=main_category,  # type: ignore[arg-type]
            subcategory=subcategory,  # type: ignore[arg-type]
            quantity=1.0,
            unit="buc",
            unit_price=round(total_amount - sgr + discounts, 2),
            discount_amount=discounts,
            net_total=round(total_amount - sgr, 2),
        )
    ]
    if sgr > 0:
        items.append(
            ExtractedLineItem(
                raw_name="GARANTIE SGR",
                clean_name="Garanție SGR",
                main_category="Groceries",
                subcategory="Bottle Deposit (SGR)",
                quantity=1.0,
                unit="buc",
                unit_price=sgr,
                discount_amount=0.0,
                net_total=sgr,
                is_sgr_deposit=True,
            )
        )
    receipt = ExtractedReceipt(
        is_valid_receipt=True,
        store_name=store_name,
        receipt_date=receipt_date,
        receipt_time="12:15",
        currency="RON",
        payment_method="Card",
        subtotal_discounts=discounts,
        sgr_deposit_total=sgr,
        total_amount=total_amount,
        items=items,
    )
    receipt.reconcile_totals()
    return receipt


def test_static_assets_and_index_served(tmp_path: Path) -> None:
    app = create_app(settings=_make_settings(tmp_path), start_watcher_on_startup=False)
    with TestClient(app) as client:
        index_resp = client.get("/")
        assert index_resp.status_code == 200
        assert "text/html" in index_resp.headers["content-type"]
        assert "Receipt Budget Automator" in index_resp.text

        css_resp = client.get("/static/styles.css")
        assert css_resp.status_code == 200
        assert "--bg-canvas" in css_resp.text

        js_resp = client.get("/static/app.js")
        assert js_resp.status_code == 200
        assert "/api/dashboard" in js_resp.text


def test_status_incomplete_configured_and_locked(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings=settings, start_watcher_on_startup=False)
    with TestClient(app) as client:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()

        assert data["setup_complete"] is False
        assert data["gemini"]["configured"] is False
        assert data["gemini"]["masked_key"] == ""
        assert data["drive"]["connected"] is False
        assert data["drive"]["oauth_source"] == "builtin_default"
        assert data["excel"]["locked"] is False
        assert data["excel"]["exists"] is True
        assert data["taxonomy"]["main_categories"] == list(MAIN_CATEGORIES)
        assert data["taxonomy"]["subcategories"] == list(SUBCATEGORIES)
        assert data["taxonomy"]["subcategory_to_main"] == dict(SUBCATEGORY_TO_MAIN)

    # Configured key + custom credentials + lock file present
    locked_dir = tmp_path / "locked_case"
    locked_settings = _make_settings(locked_dir, gemini_api_key="AIzaSyConfiguredKey9876")
    locked_settings.credentials_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "custom-id",
                    "client_secret": "custom-sec",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )
    (locked_dir / ".~lock.budget_tracker.xlsx#").write_text("lock", encoding="utf-8")

    locked_app = create_app(settings=locked_settings, start_watcher_on_startup=False)
    with TestClient(locked_app) as client:
        resp = client.get("/api/status?refresh_drive=false")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gemini"]["configured"] is True
        assert data["gemini"]["masked_key"] == "AIzaSy...9876"
        assert data["drive"]["oauth_source"] == "custom_credentials_json"
        assert data["excel"]["locked"] is True


def test_settings_gemini_and_general_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings=settings, start_watcher_on_startup=False)

    with TestClient(app) as client:
        # Empty and placeholder keys rejected with 400
        empty_resp = client.post(
            "/api/settings/gemini",
            json={"api_key": "   ", "verify": False},
        )
        assert empty_resp.status_code == 400

        placeholder_resp = client.post(
            "/api/settings/gemini",
            json={"api_key": "your_gemini_api_key_here", "verify": False},
        )
        assert placeholder_resp.status_code == 400

        # Save without live verification
        save_resp = client.post(
            "/api/settings/gemini",
            json={
                "api_key": "AIzaSyWebDashboard1234",
                "model": "gemini-3.7-flash",
                "verify": False,
            },
        )
        assert save_resp.status_code == 200
        assert save_resp.json() == {
            "ok": True,
            "masked_key": "AIzaSy...1234",
            "model": "gemini-3.7-flash",
        }
        assert "GEMINI_API_KEY=AIzaSyWebDashboard1234" in (tmp_path / ".env").read_text(encoding="utf-8")

        status = client.get("/api/status?refresh_drive=false").json()
        assert status["gemini"]["configured"] is True
        assert status["gemini"]["masked_key"] == "AIzaSy...1234"
        assert status["gemini"]["model"] == "gemini-3.7-flash"

        # Verify=True calls _verify_gemini_api_key
        monkeypatch.setattr(
            "src.web.api._verify_gemini_api_key",
            lambda _key: (_ for _ in ()).throw(RuntimeError("bad api key")),
        )
        bad_verify = client.post(
            "/api/settings/gemini",
            json={"api_key": "AIzaSyBadKey0000", "verify": True},
        )
        assert bad_verify.status_code == 400
        assert "bad api key" in bad_verify.json()["detail"]

        # Update general settings
        gen_resp = client.post(
            "/api/settings/general",
            json={
                "drive_folder_name": "bonuri_familie",
                "poll_interval_seconds": 45,
                "gemini_model": "gemini-3.5-flash",
            },
        )
        assert gen_resp.status_code == 200
        assert gen_resp.json() == {
            "ok": True,
            "drive_folder_name": "bonuri_familie",
            "poll_interval_seconds": 45,
            "gemini_model": "gemini-3.5-flash",
        }
        env_content = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "DRIVE_FOLDER_NAME=bonuri_familie" in env_content
        assert "POLL_INTERVAL_SECONDS=45" in env_content
        assert "GEMINI_MODEL=gemini-3.5-flash" in env_content

        assert (
            client.post("/api/settings/general", json={"drive_folder_name": "   "}).status_code
            == 400
        )
        assert (
            client.post("/api/settings/general", json={"poll_interval_seconds": 3}).status_code
            == 400
        )


def test_upload_custom_credentials_json(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings=settings, start_watcher_on_startup=False)

    valid_oauth_json = json.dumps(
        {
            "installed": {
                "client_id": "123456-custom.apps.googleusercontent.com",
                "project_id": "receipt-proj-1",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_secret": "test-client-secret",
                "redirect_uris": ["http://localhost"],
            }
        }
    ).encode("utf-8")

    with TestClient(app) as client:
        bad_json_resp = client.post(
            "/api/settings/credentials-json",
            files={"file": ("credentials.json", b"{not-json", "application/json")},
        )
        assert bad_json_resp.status_code == 400

        missing_fields_resp = client.post(
            "/api/settings/credentials-json",
            files={
                "file": (
                    "credentials.json",
                    b'{"installed": {"client_id": "only-id"}}',
                    "application/json",
                )
            },
        )
        assert missing_fields_resp.status_code == 400

        ok_resp = client.post(
            "/api/settings/credentials-json",
            files={"file": ("credentials.json", valid_oauth_json, "application/json")},
        )
        assert ok_resp.status_code == 200
        body = ok_resp.json()
        assert body["ok"] is True
        assert body["source"] == "custom_credentials_json"
        assert body["client_id"] == "123456-custom.apps.googleusercontent.com"
        assert body["project_id"] == "receipt-proj-1"

        assert settings.credentials_path.exists()
        assert stat.S_IMODE(settings.credentials_path.stat().st_mode) == 0o600

        status = client.get("/api/status").json()
        assert status["drive"]["oauth_source"] == "custom_credentials_json"


def test_google_oauth_flow_callback_and_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-oauth-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-oauth-client-secret")
    settings = _make_settings(tmp_path, gemini_api_key="AIzaSyConfiguredKey1111")
    captured: dict[str, Any] = {}

    class FakeFlow:
        def __init__(self, redirect_uri: str, state: str | None = None) -> None:
            self.redirect_uri = redirect_uri
            self.state = state
            self.code_verifier = "pkce-verifier-xyz"
            self.credentials: Credentials | None = None

        def authorization_url(self, **_kwargs: Any) -> tuple[str, str]:
            return "https://accounts.google.com/o/oauth2/auth?state=state-123", "state-123"

        def fetch_token(self, *, code: str) -> None:
            captured["code"] = code
            captured["redirect_uri"] = self.redirect_uri
            captured["state"] = self.state
            captured["code_verifier"] = self.code_verifier
            self.credentials = Credentials(
                token="fake-access-token",
                refresh_token="fake-refresh-token",
                token_uri="https://oauth2.googleapis.com/token",
                client_id="fake-client-id",
                client_secret="fake-client-secret",
                scopes=DRIVE_SCOPES,
            )

    def _fake_from_client_config(
        _client_config: dict[str, Any],
        _scopes: list[str],
        *,
        redirect_uri: str,
        state: str | None = None,
    ) -> FakeFlow:
        return FakeFlow(redirect_uri=redirect_uri, state=state)

    monkeypatch.setattr(
        drive_client_module.InstalledAppFlow,
        "from_client_config",
        staticmethod(_fake_from_client_config),
    )

    app = create_app(settings=settings, start_watcher_on_startup=False)
    with TestClient(app) as client:
        start_json = client.get("/api/auth/google/start?format=json")
        assert start_json.status_code == 200
        assert start_json.json() == {
            "auth_url": "https://accounts.google.com/o/oauth2/auth?state=state-123",
            "state": "state-123",
        }

        start_redirect = client.get("/api/auth/google/start", follow_redirects=False)
        assert start_redirect.status_code == 302
        assert start_redirect.headers["location"].startswith("https://accounts.google.com/")

        # Denied consent redirects with auth_error
        denied = client.get(
            "/api/auth/google/callback?error=access_denied",
            follow_redirects=False,
        )
        assert denied.status_code == 302
        assert denied.headers["location"] == "/?auth_error=access_denied"

        # Missing code redirects with auth_error
        missing_code = client.get("/api/auth/google/callback", follow_redirects=False)
        assert missing_code.status_code == 302
        assert missing_code.headers["location"] == "/?auth_error=missing_code"

        # Missing state or invalid/forged state must be rejected with auth_error
        missing_state = client.get(
            "/api/auth/google/callback?code=oauth-code-99",
            follow_redirects=False,
        )
        assert missing_state.status_code == 302
        assert missing_state.headers["location"].startswith("/?auth_error=")
        assert not settings.token_path.exists()

        bad_state = client.get(
            "/api/auth/google/callback?code=oauth-code-99&state=forged-state",
            follow_redirects=False,
        )
        assert bad_state.status_code == 302
        assert bad_state.headers["location"].startswith("/?auth_error=")
        assert not settings.token_path.exists()

        monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)

        # Valid callback exchanges code, sets scope relaxation, and persists token.json
        callback_resp = client.get(
            "/api/auth/google/callback?code=oauth-code-99&state=state-123",
            follow_redirects=False,
        )
        assert callback_resp.status_code == 302
        assert callback_resp.headers["location"] == "/?auth=google_connected"
        assert captured["code"] == "oauth-code-99"
        assert captured["state"] == "state-123"
        assert captured["code_verifier"] == "pkce-verifier-xyz"
        import os

        assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"

        # Replaying a consumed OAuth state must fail
        replay_resp = client.get(
            "/api/auth/google/callback?code=oauth-code-99&state=state-123",
            follow_redirects=False,
        )
        assert replay_resp.status_code == 302
        assert replay_resp.headers["location"].startswith("/?auth_error=")

        assert settings.token_path.exists()
        assert stat.S_IMODE(settings.token_path.stat().st_mode) == 0o600
        token_json = json.loads(settings.token_path.read_text(encoding="utf-8"))
        assert token_json["refresh_token"] == "fake-refresh-token"

        # Disconnect deletes token.json and pauses watcher
        disc_resp = client.post("/api/auth/google/disconnect")
        assert disc_resp.status_code == 200
        assert disc_resp.json() == {"ok": True}
        assert not settings.token_path.exists()

        status = client.get("/api/status?refresh_drive=false").json()
        assert status["drive"]["connected"] is False
        assert status["watcher"]["paused"] is True


def test_watcher_start_pause_and_sync_now(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, gemini_api_key="AIzaSyWatcherKey1234", poll_interval_seconds=60)
    png_data = _png_bytes((20, 40, 60))

    class StubDriveClient:
        def __init__(self) -> None:
            self.raise_auth_error = False
            self.files: dict[str, tuple[DriveFile, bytes]] = {
                "drv-100": (
                    DriveFile(
                        id="drv-100",
                        name="drive_bon.png",
                        mime_type="image/png",
                        size=len(png_data),
                        created_time="2026-11-18T10:00:00Z",
                    ),
                    png_data,
                )
            }
            self.deleted_ids: list[str] = []

        def has_valid_token(self) -> bool:
            return not self.raise_auth_error

        def get_account_info(self) -> dict[str, Any]:
            return {
                "connected": True,
                "email": "user@example.com",
                "display_name": "Test User",
                "folder_name": settings.drive_folder_name,
                "folder_ids": ["folder-1"],
                "folder_url": "https://drive.google.com/drive/folders/folder-1",
                "oauth_source": "builtin_default",
            }

        def list_pending_files(self) -> list[DriveFile]:
            if self.raise_auth_error:
                raise DriveAuthError("Token expired")
            return [meta for meta, _ in self.files.values()]

        def download_file(self, _file_id: str, dest_path: Path) -> Path:
            dest_path.write_bytes(png_data)
            return dest_path

        def delete_file_permanently(self, file_id: str) -> None:
            self.deleted_ids.append(file_id)
            self.files.pop(file_id, None)

    stub_drive = StubDriveClient()
    stub_extractor = SimpleNamespace(
        extract=lambda _path: _sample_receipt(
            store_name="Kaufland",
            receipt_date="2026-11-18",
            total_amount=18.50,
            clean_name="Lapte Zuzu",
        )
    )

    def _pipeline_factory(cfg: Settings) -> ReceiptPipeline:
        return ReceiptPipeline(
            settings=cfg,
            drive_client=stub_drive,  # type: ignore[arg-type]
            extractor=stub_extractor,  # type: ignore[arg-type]
            excel_writer=ExcelBudgetWriter.from_settings(cfg),
            notifier=DesktopNotifier(enabled=False),
        )

    manager = WatcherManager(settings=settings, pipeline_factory=_pipeline_factory, notify=False)
    app = create_app(settings=settings, watcher_manager=manager, start_watcher_on_startup=False)

    with TestClient(app) as client:
        pause_resp = client.post("/api/watcher/pause")
        assert pause_resp.status_code == 200
        assert pause_resp.json()["paused"] is True
        assert pause_resp.json()["running"] is False

        sync_resp = client.post("/api/watcher/sync-now")
        assert sync_resp.status_code == 200
        sync_data = sync_resp.json()
        assert sync_data["stats"]["processed"] == 1
        assert sync_data["watcher"]["totals"]["processed"] == 1
        assert stub_drive.deleted_ids == ["drv-100"]

        dash = client.get("/api/dashboard").json()
        assert dash["kpis"]["receipt_count"] == 1
        assert dash["receipts"][0]["store_name"] == "Kaufland"

        start_resp = client.post("/api/watcher/start")
        assert start_resp.status_code == 200
        assert start_resp.json()["running"] is True
        assert start_resp.json()["paused"] is False

        pause_again = client.post("/api/watcher/pause")
        assert pause_again.status_code == 200
        assert pause_again.json()["running"] is False

        stub_drive.raise_auth_error = True
        auth_err_resp = client.post("/api/watcher/sync-now")
        assert auth_err_resp.status_code == 401


def test_upload_edit_delete_locked_and_excel_download(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, gemini_api_key="AIzaSyUploadKey5555")
    receipts_queue = [
        _sample_receipt(
            store_name="Lidl",
            receipt_date="2026-10-10",
            total_amount=22.50,
            clean_name="Iaurt grecesc",
            discounts=2.00,
            sgr=0.50,
        ),
        _sample_receipt(
            store_name="Mega Image",
            receipt_date="2026-11-12",
            total_amount=15.00,
            clean_name="Pâine integrală",
            subcategory="Bakery & Bread",
        ),
    ]

    class StubExtractor:
        def extract(self, _path: Path) -> ExtractedReceipt:
            return receipts_queue.pop(0)

    def _pipeline_factory(cfg: Settings) -> ReceiptPipeline:
        return ReceiptPipeline(
            settings=cfg,
            drive_client=None,
            extractor=StubExtractor(),  # type: ignore[arg-type]
            excel_writer=ExcelBudgetWriter.from_settings(cfg),
            notifier=DesktopNotifier(enabled=False),
        )

    manager = WatcherManager(settings=settings, pipeline_factory=_pipeline_factory, notify=False)
    app = create_app(settings=settings, watcher_manager=manager, start_watcher_on_startup=False)

    img1 = _png_bytes((255, 0, 0))
    img2 = _png_bytes((0, 0, 255))

    with TestClient(app) as client:
        upload_resp = client.post(
            "/api/receipts/upload",
            files=[
                ("files", ("lidl_oct.png", img1, "image/png")),
                ("files", ("mega_nov.png", img2, "image/png")),
            ],
        )
        assert upload_resp.status_code == 200
        results = upload_resp.json()["results"]
        assert len(results) == 2
        assert [r["status"] for r in results] == ["added", "added"]
        assert [r["receipt_id"] for r in results] == ["RCP-0001", "RCP-0002"]

        # Duplicate upload detected
        dup_resp = client.post(
            "/api/receipts/upload",
            files=[("files", ("lidl_oct_again.png", img1, "image/png"))],
        )
        assert dup_resp.status_code == 200
        assert dup_resp.json()["results"][0]["status"] == "duplicate"

        # Full dashboard and month filter
        dash = client.get("/api/dashboard").json()
        assert dash["months"] == ["2026-10", "2026-11"]
        assert dash["kpis"]["total_spent"] == 37.50
        assert dash["kpis"]["total_discounts"] == 2.00
        assert dash["kpis"]["total_sgr"] == 0.50
        assert dash["kpis"]["receipt_count"] == 2
        assert dash["kpis"]["items_count"] == 3

        oct_dash = client.get("/api/dashboard?month=2026-10").json()
        assert oct_dash["kpis"]["receipt_count"] == 1
        assert oct_dash["kpis"]["total_spent"] == 22.50

        # PATCH receipt metadata
        patch_rcp = client.patch(
            "/api/receipts/RCP-0001",
            json={
                "store_name": "Lidl Updated",
                "date": "2026-10-11",
                "payment_method": "Cash",
                "status": "Needs Review",
            },
        )
        assert patch_rcp.status_code == 200
        assert patch_rcp.json()["receipt"]["store_name"] == "Lidl Updated"
        assert patch_rcp.json()["receipt"]["status"] == "Needs Review"

        # PATCH line item + category validation + parent recalculation
        bad_item = client.patch(
            "/api/receipts/RCP-0001/items/0",
            json={"main_category": "BogusCategory"},
        )
        assert bad_item.status_code == 400

        missing_item = client.patch(
            "/api/receipts/RCP-0001/items/99",
            json={"clean_name": "Nope"},
        )
        assert missing_item.status_code == 404

        patch_item = client.patch(
            "/api/receipts/RCP-0001/items/0",
            json={
                "clean_name": "Iaurt grecesc Olympus",
                "main_category": "Household",
                "subcategory": "Cleaning & Laundry",
                "quantity": 2.0,
                "unit_price": 15.00,
                "discount_amount": 4.00,
                "net_total": 26.00,
            },
        )
        assert patch_item.status_code == 200
        assert patch_item.json()["item"]["clean_name"] == "Iaurt grecesc Olympus"

        updated_dash = client.get("/api/dashboard").json()
        rcp1 = next(r for r in updated_dash["receipts"] if r["receipt_id"] == "RCP-0001")
        assert rcp1["total_paid"] == 26.50
        assert rcp1["discounts_saved"] == 4.00
        assert rcp1["sgr_deposit"] == 0.50

        # Lock workbook and verify 423 responses on all write endpoints
        lock_file = tmp_path / ".~lock.budget_tracker.xlsx#"
        lock_file.write_text("tudor,host", encoding="utf-8")
        try:
            assert (
                client.post(
                    "/api/receipts/upload",
                    files=[("files", ("new.png", _png_bytes((0, 255, 0)), "image/png"))],
                ).status_code
                == 423
            )
            assert (
                client.patch("/api/receipts/RCP-0001", json={"store_name": "Blocked"}).status_code
                == 423
            )
            assert (
                client.patch("/api/receipts/RCP-0001/items/0", json={"clean_name": "Blocked"}).status_code
                == 423
            )
            assert client.delete("/api/receipts/RCP-0001").status_code == 423
            # Dashboard read remains available while locked
            assert client.get("/api/dashboard").status_code == 200
        finally:
            lock_file.unlink(missing_ok=True)

        # Delete receipt after unlocking
        del_resp = client.delete("/api/receipts/RCP-0001")
        assert del_resp.status_code == 200
        assert del_resp.json() == {"ok": True, "deleted": True, "receipt_id": "RCP-0001"}
        assert client.delete("/api/receipts/RCP-0001").status_code == 404

        # Download valid Excel workbook bytes
        dl_resp = client.get("/api/excel/download")
        assert dl_resp.status_code == 200
        assert (
            dl_resp.headers["content-type"]
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        wb = load_workbook(io.BytesIO(dl_resp.content))
        assert wb.sheetnames == ["Monthly Dashboard", "Receipts", "Items"]
        assert wb["Receipts"].max_row == 2
        assert wb["Receipts"].cell(row=2, column=1).value == "RCP-0002"


def test_app_cli_parse_args_and_create_app() -> None:
    import app as web_app

    args = web_app.parse_args(["--host", "0.0.0.0", "--port", "18765", "--open-browser", "--no-watcher"])
    assert args.host == "0.0.0.0"
    assert args.port == 18765
    assert args.open_browser is True
    assert args.no_watcher is True


def test_oauth_redirect_normalizes_loopback_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path)
    seen_redirects: list[str] = []

    def _fake_create_url(_self: Any, redirect_uri: str) -> tuple[str, str]:
        seen_redirects.append(redirect_uri)
        return f"https://accounts.google.com/o/oauth2/auth?redirect_uri={redirect_uri}", "st-1"

    monkeypatch.setattr(
        drive_client_module.GoogleDriveClient,
        "create_web_authorization_url",
        _fake_create_url,
    )

    app = create_app(settings=settings, start_watcher_on_startup=False)
    with TestClient(app) as client:
        for host_header in ("127.0.0.1:8765", "0.0.0.0:8765", "[::1]:9000"):
            resp = client.get("/api/auth/google/start?format=json", headers={"host": host_header})
            assert resp.status_code == 200

        # Non-loopback host stays intact
        lan_resp = client.get("/api/auth/google/start?format=json", headers={"host": "192.168.1.50:8765"})
        assert lan_resp.status_code == 200

    assert seen_redirects == [
        "http://localhost:8765/api/auth/google/callback",
        "http://localhost:8765/api/auth/google/callback",
        "http://localhost:9000/api/auth/google/callback",
        "http://192.168.1.50:8765/api/auth/google/callback",
    ]


def test_drive_cache_preserves_auth_error_across_non_refresh_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path, gemini_api_key="AIzaSyConfiguredKey2222")
    # Write a token file on disk that looks locally present, while live Drive check returns an auth error
    settings.token_path.write_text(
        json.dumps({"token": "expired", "refresh_token": "revoked"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        drive_client_module.GoogleDriveClient,
        "get_account_info",
        lambda _self: {
            "connected": False,
            "email": "",
            "display_name": "",
            "folder_name": settings.drive_folder_name,
            "folder_ids": [],
            "folder_url": "",
            "oauth_source": "builtin_default",
            "error": "invalid_grant: Token has been expired or revoked.",
        },
    )

    app = create_app(settings=settings, start_watcher_on_startup=False)
    with TestClient(app) as client:
        live_status = client.get("/api/status?refresh_drive=true").json()
        assert live_status["drive"]["connected"] is False
        assert "revoked" in live_status["drive"]["error"]

        # Subsequent non-refresh poll must keep the cached auth error even past 30s TTL
        base_mono = 1000.0
        monkeypatch.setattr("src.web.api.time.monotonic", lambda: base_mono + 120.0)
        polled_status = client.get("/api/status?refresh_drive=false").json()
        assert polled_status["drive"]["connected"] is False
        assert "revoked" in polled_status["drive"]["error"]
        assert polled_status["setup_complete"] is False


def test_delete_receipt_allows_reupload_via_api(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, gemini_api_key="AIzaSyReuploadKey3333")

    class StubExtractor:
        def extract(self, _path: Path) -> ExtractedReceipt:
            return _sample_receipt(
                store_name="Profi",
                receipt_date="2026-11-16",
                total_amount=14.00,
                clean_name="Apă plată 2L",
            )

    def _pipeline_factory(cfg: Settings) -> ReceiptPipeline:
        return ReceiptPipeline(
            settings=cfg,
            drive_client=None,
            extractor=StubExtractor(),  # type: ignore[arg-type]
            excel_writer=ExcelBudgetWriter.from_settings(cfg),
            notifier=DesktopNotifier(enabled=False),
        )

    manager = WatcherManager(settings=settings, pipeline_factory=_pipeline_factory, notify=False)
    app = create_app(settings=settings, watcher_manager=manager, start_watcher_on_startup=False)
    img_bytes = _png_bytes((12, 34, 56))

    with TestClient(app) as client:
        first_up = client.post(
            "/api/receipts/upload",
            files=[("files", ("profi.png", img_bytes, "image/png"))],
        )
        assert first_up.status_code == 200
        assert first_up.json()["results"][0]["status"] == "added"
        rid = first_up.json()["results"][0]["receipt_id"]

        # Deleting via API must purge processed_state.json so re-uploading succeeds
        del_resp = client.delete(f"/api/receipts/{rid}")
        assert del_resp.status_code == 200

        second_up = client.post(
            "/api/receipts/upload",
            files=[("files", ("profi.png", img_bytes, "image/png"))],
        )
        assert second_up.status_code == 200
        assert second_up.json()["results"][0]["status"] == "added"


def test_watcher_manager_reload_settings_preserves_excel_writer(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    manager = WatcherManager(settings=settings, notify=False)
    orig_writer = manager.pipeline.excel_writer

    updated_settings = _make_settings(
        tmp_path,
        drive_folder_name="updated_folder",
        poll_interval_seconds=40,
    )
    manager.reload_settings(updated_settings)
    assert manager.pipeline.excel_writer is orig_writer
    assert manager.pipeline.excel_writer._lock is orig_writer._lock

    # Different excel_path creates a distinct writer
    moved_settings = _make_settings(tmp_path, excel_path=tmp_path / "other.xlsx")
    manager.reload_settings(moved_settings)
    assert manager.pipeline.excel_writer is not orig_writer


def test_app_main_exits_zero_when_port_already_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket
    import app as web_app

    opened_urls: list[str] = []
    uvicorn_called: list[bool] = []

    monkeypatch.setattr(web_app, "configure_logging", lambda _s: None)
    monkeypatch.setattr(
        web_app.Settings,
        "from_env",
        classmethod(lambda cls, _base=None: _make_settings(tmp_path)),
    )
    monkeypatch.setattr(web_app.webbrowser, "open", lambda url: opened_urls.append(url) or True)
    monkeypatch.setattr(web_app.uvicorn, "run", lambda *_args, **_kwargs: uvicorn_called.append(True))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        rc = web_app.main(["--host", "127.0.0.1", "--port", str(port), "--open-browser"])

    assert rc == 0
    assert uvicorn_called == []
    assert opened_urls == [f"http://localhost:{port}"]
