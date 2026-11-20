"""FastAPI application and REST endpoints for the Receipt Budget Web Dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
import urllib.parse

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from src.config import (
    MAIN_CATEGORIES,
    PROJECT_ROOT,
    SUBCATEGORIES,
    SUBCATEGORY_TO_MAIN,
    Settings,
    mask_secret,
    update_env_file,
)
from src.default_oauth_client import (
    get_oauth_client_config,
    validate_and_save_custom_credentials,
)
from src.drive_client import DriveAuthError, GoogleDriveClient
from src.excel_writer import WorkbookLockedError
from src.web.watcher_manager import WatcherManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DRIVE_CACHE_TTL_SECONDS = 30.0
AVAILABLE_GEMINI_MODELS: list[str] = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]


class GeminiSettingsRequest(BaseModel):
    """Request body for saving and optionally verifying the Gemini API key and model."""

    api_key: str = ""
    model: str | None = None
    fallback_models: list[str] | None = None
    verify: bool = True


class GeneralSettingsRequest(BaseModel):
    """Request body for updating Drive folder, polling interval, or default model."""

    drive_folder_name: str | None = None
    poll_interval_seconds: int | None = None
    gemini_model: str | None = None


class ReceiptUpdateRequest(BaseModel):
    """Partial update payload for a receipt header row in the `Receipts` sheet."""

    date: str | None = None
    time: str | None = None
    store_name: str | None = None
    payment_method: str | None = None
    discounts_saved: float | None = None
    sgr_deposit: float | None = None
    total_paid: float | None = None
    currency: str | None = None
    status: str | None = None


class ItemUpdateRequest(BaseModel):
    """Partial update payload for a single line item row in the `Items` sheet."""

    clean_name: str | None = None
    raw_name: str | None = None
    main_category: str | None = None
    subcategory: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    discount_amount: float | None = None
    net_total: float | None = None
    recalculate_receipt_total: bool = True


def _is_gemini_configured(settings: Settings) -> bool:
    key = (settings.gemini_api_key or "").strip()
    return bool(key and key != "your_gemini_api_key_here")


def _normalize_oauth_host(raw_host: str) -> str:
    """Normalize loopback/wildcard hosts to `localhost` for Google OAuth redirect URIs."""
    host = raw_host.strip()
    if host in {"127.0.0.1", "0.0.0.0", "::1", "[::1]"}:
        return "localhost"
    try:
        parsed = urllib.parse.urlsplit(f"//{host}")
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return host
    if hostname in {"127.0.0.1", "0.0.0.0", "::1"}:
        return f"localhost:{port}" if port is not None else "localhost"
    return host


def _build_google_redirect_uri(request: Request, settings: Settings) -> str:
    raw_host = request.headers.get("host") or f"{settings.app_host}:{settings.app_port}"
    host = _normalize_oauth_host(raw_host)
    return f"{request.url.scheme}://{host}/api/auth/google/callback"


def _token_looks_present_locally(settings: Settings) -> bool:
    if not settings.token_path.exists():
        return False
    try:
        data = json.loads(settings.token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("token") or data.get("refresh_token"))


def _verify_gemini_api_key(api_key: str) -> None:
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=15_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    client.models.list()


def create_app(
    settings: Settings | None = None,
    watcher_manager: WatcherManager | None = None,
    start_watcher_on_startup: bool | None = None,
) -> FastAPI:
    """Create and configure the FastAPI web dashboard application."""
    resolved_settings = settings or (
        watcher_manager.settings if watcher_manager is not None else Settings.from_env(PROJECT_ROOT)
    )
    manager = watcher_manager or WatcherManager(settings=resolved_settings)

    drive_cache_lock = threading.Lock()
    drive_cache: dict[str, Any] = {"ts": 0.0, "data": None}

    def invalidate_drive_cache() -> None:
        with drive_cache_lock:
            drive_cache["ts"] = 0.0
            drive_cache["data"] = None

    def _disconnected_drive_info(current_settings: Settings, *, connected: bool = False) -> dict[str, Any]:
        _, oauth_source = get_oauth_client_config(current_settings.credentials_path)
        return {
            "connected": connected,
            "email": "",
            "display_name": "",
            "folder_name": current_settings.drive_folder_name,
            "folder_ids": [],
            "folder_url": "",
            "oauth_source": oauth_source,
        }

    def _get_drive_info(refresh_drive: bool | None) -> dict[str, Any]:
        current_settings = manager.settings
        drive_client = manager.pipeline.drive_client
        now = time.monotonic()

        with drive_cache_lock:
            cached_data = drive_cache["data"]
            cached_ts = drive_cache["ts"]

        if refresh_drive is False:
            if cached_data is not None:
                return dict(cached_data)
            if getattr(drive_client, "_service", None) is not None:
                connected = True
            elif type(drive_client).has_valid_token is not GoogleDriveClient.has_valid_token:
                connected = bool(drive_client.has_valid_token())
            else:
                connected = _token_looks_present_locally(current_settings)
            return _disconnected_drive_info(current_settings, connected=connected)

        if refresh_drive is None and cached_data is not None and (now - cached_ts) < DRIVE_CACHE_TTL_SECONDS:
            if cached_data.get("connected") or "error" in cached_data or not current_settings.token_path.exists():
                return dict(cached_data)

        info = drive_client.get_account_info()
        with drive_cache_lock:
            drive_cache["ts"] = time.monotonic()
            drive_cache["data"] = dict(info)
        return info

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        current_settings = manager.settings
        try:
            manager.pipeline.excel_writer.ensure_initialized()
        except WorkbookLockedError:
            logger.warning(
                "Workbook %s is open/locked at startup; skipping initialization.",
                current_settings.excel_path.name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not initialize workbook at startup: %s", exc)

        should_start = (
            current_settings.auto_start_watcher
            if start_watcher_on_startup is None
            else start_watcher_on_startup
        )
        if should_start and _is_gemini_configured(current_settings):
            try:
                if manager.pipeline.drive_client.has_valid_token():
                    manager.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not auto-start watcher on startup: %s", exc)

        try:
            yield
        finally:
            manager.stop()

    app = FastAPI(title="Receipt Budget Automator", lifespan=lifespan)
    app.state.watcher_manager = manager
    app.state.invalidate_drive_cache = invalidate_drive_cache

    @app.get("/api/status")
    def get_status(refresh_drive: bool | None = Query(default=None)) -> dict[str, Any]:
        current_settings = manager.settings
        gemini_configured = _is_gemini_configured(current_settings)
        drive_info = _get_drive_info(refresh_drive)
        return {
            "setup_complete": bool(gemini_configured and drive_info.get("connected")),
            "gemini": {
                "configured": gemini_configured,
                "masked_key": mask_secret(current_settings.gemini_api_key),
                "model": current_settings.gemini_model,
                "fallback_models": list(current_settings.gemini_fallback_models),
                "available_models": list(AVAILABLE_GEMINI_MODELS),
            },
            "drive": drive_info,
            "watcher": manager.get_status(),
            "excel": {
                "path": str(current_settings.excel_path),
                "filename": current_settings.excel_path.name,
                "exists": current_settings.excel_path.exists(),
                "locked": manager.pipeline.excel_writer.is_locked(),
            },
            "taxonomy": {
                "main_categories": list(MAIN_CATEGORIES),
                "subcategories": list(SUBCATEGORIES),
                "subcategory_to_main": dict(SUBCATEGORY_TO_MAIN),
            },
        }

    @app.post("/api/settings/gemini")
    def save_gemini_settings(payload: GeminiSettingsRequest) -> dict[str, Any]:
        current_settings = manager.settings
        candidate_key = payload.api_key.strip()
        effective_key = candidate_key or current_settings.gemini_api_key

        if not effective_key or effective_key == "your_gemini_api_key_here":
            raise HTTPException(
                status_code=400,
                detail="Please enter a valid Gemini API key.",
            )

        if payload.verify:
            try:
                _verify_gemini_api_key(effective_key)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=400,
                    detail=f"Gemini API key verification failed: {exc}",
                ) from exc

        updates: dict[str, str] = {"GEMINI_API_KEY": effective_key}
        if payload.model is not None and payload.model.strip():
            updates["GEMINI_MODEL"] = payload.model.strip()
        if payload.fallback_models is not None:
            cleaned = [m.strip() for m in payload.fallback_models if m and m.strip()]
            updates["GEMINI_FALLBACK_MODELS"] = ",".join(cleaned)

        update_env_file(current_settings.base_dir, updates)
        new_settings = manager.reload_settings()
        manager.record_event("success", "Gemini API settings updated.")

        if new_settings.auto_start_watcher:
            try:
                if manager.pipeline.drive_client.has_valid_token():
                    manager.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not start watcher after saving Gemini key: %s", exc)

        return {
            "ok": True,
            "masked_key": mask_secret(effective_key),
            "model": new_settings.gemini_model,
        }

    @app.post("/api/settings/general")
    def save_general_settings(payload: GeneralSettingsRequest) -> dict[str, Any]:
        current_settings = manager.settings
        updates: dict[str, str] = {}

        if payload.drive_folder_name is not None:
            folder_name = payload.drive_folder_name.strip()
            if not folder_name:
                raise HTTPException(status_code=400, detail="Drive folder name cannot be empty.")
            updates["DRIVE_FOLDER_NAME"] = folder_name

        if payload.poll_interval_seconds is not None:
            if payload.poll_interval_seconds < 5:
                raise HTTPException(
                    status_code=400,
                    detail="Poll interval must be at least 5 seconds.",
                )
            updates["POLL_INTERVAL_SECONDS"] = str(payload.poll_interval_seconds)

        if payload.gemini_model is not None:
            model = payload.gemini_model.strip()
            if not model:
                raise HTTPException(status_code=400, detail="Gemini model cannot be empty.")
            updates["GEMINI_MODEL"] = model

        if updates:
            update_env_file(current_settings.base_dir, updates)
            new_settings = manager.reload_settings()
        else:
            new_settings = current_settings

        invalidate_drive_cache()
        return {
            "ok": True,
            "drive_folder_name": new_settings.drive_folder_name,
            "poll_interval_seconds": new_settings.poll_interval_seconds,
            "gemini_model": new_settings.gemini_model,
        }

    @app.post("/api/settings/credentials-json")
    async def upload_credentials_json(file: UploadFile = File(...)) -> dict[str, Any]:
        raw_bytes = await file.read()
        try:
            saved = await run_in_threadpool(
                validate_and_save_custom_credentials,
                manager.settings.credentials_path,
                raw_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        invalidate_drive_cache()
        manager.record_event("info", "Custom Google OAuth credentials.json saved.")
        return {"ok": True, **saved}

    @app.get("/api/auth/google/start")
    def google_auth_start(request: Request) -> Response:
        current_settings = manager.settings
        redirect_uri = _build_google_redirect_uri(request, current_settings)
        try:
            auth_url, state = manager.pipeline.drive_client.create_web_authorization_url(redirect_uri)
        except DriveAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.query_params.get("format") == "json":
            return JSONResponse({"auth_url": auth_url, "state": state})
        return RedirectResponse(url=auth_url, status_code=302)

    @app.get("/api/auth/google/callback")
    def google_auth_callback(request: Request) -> RedirectResponse:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        if error or not code:
            reason = urllib.parse.quote(error or "missing_code")
            return RedirectResponse(url=f"/?auth_error={reason}", status_code=302)

        redirect_uri = _build_google_redirect_uri(request, manager.settings)
        try:
            manager.pipeline.drive_client.fetch_token_from_callback(
                redirect_uri=redirect_uri,
                code=code,
                state=state,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Google OAuth callback failed: %s", exc)
            manager.record_event("error", f"Google Drive connection failed: {exc}")
            return RedirectResponse(
                url=f"/?auth_error={urllib.parse.quote(str(exc))}",
                status_code=302,
            )

        invalidate_drive_cache()
        new_settings = manager.reload_settings()
        if new_settings.auto_start_watcher and _is_gemini_configured(new_settings):
            manager.start()
        manager.record_event("success", "Google Drive connected successfully.")
        return RedirectResponse(url="/?auth=google_connected", status_code=302)

    @app.post("/api/auth/google/disconnect")
    def google_auth_disconnect() -> dict[str, Any]:
        manager.pipeline.drive_client.disconnect()
        manager.pause()
        invalidate_drive_cache()
        manager.record_event("info", "Google Drive disconnected.")
        return {"ok": True}

    @app.post("/api/watcher/start")
    def watcher_start() -> dict[str, Any]:
        return manager.start()

    @app.post("/api/watcher/pause")
    def watcher_pause() -> dict[str, Any]:
        return manager.pause()

    @app.post("/api/watcher/sync-now")
    def watcher_sync_now() -> dict[str, Any]:
        try:
            return manager.trigger_sync_now()
        except DriveAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/dashboard")
    def get_dashboard(month: str | None = Query(default=None)) -> dict[str, Any]:
        try:
            return manager.pipeline.excel_writer.get_dashboard_payload(month_filter=month)
        except WorkbookLockedError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

    @app.post("/api/receipts/upload")
    async def upload_receipts(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        if manager.pipeline.excel_writer.is_locked():
            raise HTTPException(
                status_code=423,
                detail=f"Workbook {manager.settings.excel_path.name} is open/locked. Close it before uploading.",
            )

        results: list[dict[str, Any]] = []
        for upload in files:
            original_name = Path(upload.filename or "receipt.jpg").name or "receipt.jpg"
            suffix = Path(original_name).suffix.lower() or ".jpg"
            raw_bytes = await upload.read()

            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                    tmp_file.write(raw_bytes)
                    tmp_path = Path(tmp_file.name)

                result = await run_in_threadpool(
                    manager.process_uploaded_file,
                    tmp_path,
                    original_name,
                )
                results.append(result)
            except WorkbookLockedError as exc:
                raise HTTPException(status_code=423, detail=str(exc)) from exc
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

        return {"results": results}

    @app.patch("/api/receipts/{receipt_id}")
    def update_receipt(receipt_id: str, payload: ReceiptUpdateRequest) -> dict[str, Any]:
        updates = payload.model_dump(exclude_none=True)
        try:
            updated = manager.pipeline.excel_writer.update_receipt_meta(receipt_id, updates)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WorkbookLockedError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        manager.record_event("info", f"Updated receipt {receipt_id}.")
        return {"ok": True, "receipt": updated}

    @app.delete("/api/receipts/{receipt_id}")
    def delete_receipt(receipt_id: str) -> dict[str, Any]:
        try:
            deleted = manager.pipeline.excel_writer.delete_receipt(receipt_id)
        except WorkbookLockedError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

        if not deleted:
            raise HTTPException(status_code=404, detail=f"Receipt not found: {receipt_id}")

        manager.record_event("info", f"Deleted receipt {receipt_id}.")
        return {"ok": True, "deleted": True, "receipt_id": receipt_id}

    @app.patch("/api/receipts/{receipt_id}/items/{item_index}")
    def update_receipt_item(
        receipt_id: str,
        item_index: int,
        payload: ItemUpdateRequest,
    ) -> dict[str, Any]:
        updates = payload.model_dump(exclude_none=True)
        recalculate = bool(updates.pop("recalculate_receipt_total", True))
        try:
            updated = manager.pipeline.excel_writer.update_item(
                receipt_id,
                item_index,
                updates,
                recalculate_receipt_total=recalculate,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WorkbookLockedError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        manager.record_event("info", f"Updated item #{item_index} on receipt {receipt_id}.")
        return {"ok": True, "item": updated}

    @app.get("/api/excel/download")
    def download_excel() -> FileResponse:
        current_settings = manager.settings
        if not current_settings.excel_path.exists():
            try:
                manager.pipeline.excel_writer.ensure_initialized()
            except WorkbookLockedError as exc:
                raise HTTPException(status_code=423, detail=str(exc)) from exc

        return FileResponse(
            path=current_settings.excel_path,
            filename=current_settings.excel_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/", include_in_schema=False)
    def serve_index() -> Response:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Web UI assets not found.")
        return FileResponse(index_path)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")
    return app
