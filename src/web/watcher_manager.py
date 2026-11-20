"""Background Google Drive watcher manager and upload coordinator for the Web UI."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime
import logging
from pathlib import Path
import threading
from typing import Any, Callable

from src.config import PROJECT_ROOT, Settings
from src.drive_client import DriveAuthError, GoogleDriveClient
from src.excel_writer import ExcelBudgetWriter, WorkbookLockedError, compute_sha256
from src.extractor import GeminiServiceError, ReceiptExtractionError, ReceiptExtractor
from src.notifier import DesktopNotifier
from src.pipeline import CycleStats, ReceiptPipeline

logger = logging.getLogger(__name__)

_VALID_EVENT_LEVELS = frozenset({"info", "warning", "error", "success"})


class WatcherManager:
    """Manages the background Drive polling thread, manual syncs, and web uploads."""

    def __init__(
        self,
        settings: Settings | None = None,
        pipeline_factory: Callable[[Settings], ReceiptPipeline] | None = None,
        notify: bool = True,
    ) -> None:
        self.settings = settings or Settings.from_env(PROJECT_ROOT)
        self.settings.ensure_directories()
        self.notify = notify
        self._pipeline_factory = pipeline_factory or self._default_pipeline_factory
        self.pipeline: ReceiptPipeline = self._pipeline_factory(self.settings)

        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._paused = False
        self._syncing = False
        self._thread: threading.Thread | None = None
        self._last_sync_at: str | None = None
        self._last_error: str | None = None
        self._totals: dict[str, int] = {"processed": 0, "duplicates": 0, "failed": 0}
        self._reported_locked = False

    def _default_pipeline_factory(self, settings: Settings) -> ReceiptPipeline:
        existing_pipeline = getattr(self, "pipeline", None)
        existing_writer = getattr(existing_pipeline, "excel_writer", None)
        if (
            isinstance(existing_writer, ExcelBudgetWriter)
            and existing_writer.excel_path == settings.excel_path
            and existing_writer.state_store.state_path == settings.state_path
        ):
            excel_writer = existing_writer
        else:
            excel_writer = ExcelBudgetWriter.from_settings(settings)

        return ReceiptPipeline(
            settings=settings,
            drive_client=GoogleDriveClient(settings),
            extractor=ReceiptExtractor(settings),
            excel_writer=excel_writer,
            notifier=DesktopNotifier(enabled=self.notify),
        )

    def record_event(self, level: str, message: str) -> None:
        """Append a timestamped event (newest first) to the bounded activity feed."""
        normalized_level = level if level in _VALID_EVENT_LEVELS else "info"
        event = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": normalized_level,
            "message": message,
        }
        with self._state_lock:
            self._events.appendleft(event)

    def reload_settings(self, settings: Settings | None = None) -> Settings:
        """Reload configuration from disk/env and rebuild the pipeline safely."""
        new_settings = settings or Settings.from_env(self.settings.base_dir)
        new_settings.ensure_directories()
        with self._sync_lock:
            existing_writer = getattr(self.pipeline, "excel_writer", None)
            new_pipeline = self._pipeline_factory(new_settings)
            if (
                isinstance(existing_writer, ExcelBudgetWriter)
                and isinstance(getattr(new_pipeline, "excel_writer", None), ExcelBudgetWriter)
                and existing_writer.excel_path == new_settings.excel_path
                and existing_writer.state_store.state_path == new_settings.state_path
                and new_pipeline.excel_writer is not existing_writer
            ):
                new_pipeline.excel_writer = existing_writer
            self.settings = new_settings
            self.pipeline = new_pipeline
        self.record_event("info", "Watcher settings reloaded.")
        return self.settings

    def start(self) -> dict[str, Any]:
        """Start or resume the background Drive polling thread."""
        should_spawn = False
        with self._state_lock:
            was_running = bool(self._thread and self._thread.is_alive() and not self._paused)
            self._paused = False
            if self._thread is None or not self._thread.is_alive():
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="drive-receipt-watcher",
                    daemon=True,
                )
                should_spawn = True

        if should_spawn:
            assert self._thread is not None
            self._thread.start()

        if not was_running:
            self.record_event("info", "Background Drive watcher started.")
        return self.get_status()

    def pause(self) -> dict[str, Any]:
        """Pause background Drive polling without killing the watcher thread."""
        with self._state_lock:
            self._paused = True
        self.record_event("info", "Background Drive watcher paused.")
        return self.get_status()

    def stop(self) -> None:
        """Signal the background polling thread to exit and wait up to 5 seconds."""
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        with self._state_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._state_lock:
                paused = self._paused
            if not paused:
                self._execute_sync_cycle(manual=False)

            interval = max(1, int(self.settings.poll_interval_seconds))
            elapsed = 0
            while elapsed < interval and not self._stop_event.is_set():
                if self._stop_event.wait(timeout=1.0):
                    break
                elapsed += 1
                interval = max(1, int(self.settings.poll_interval_seconds))

    def _execute_sync_cycle(self, *, manual: bool) -> CycleStats:
        with self._sync_lock:
            with self._state_lock:
                self._syncing = True
            try:
                if self.pipeline.excel_writer.is_locked():
                    if manual or not self._reported_locked:
                        self._reported_locked = True
                        self.record_event(
                            "warning",
                            f"{self.settings.excel_path.name} is open/locked; Drive sync deferred.",
                        )
                    return CycleStats()

                if self._reported_locked:
                    self._reported_locked = False
                    self.record_event(
                        "info",
                        f"{self.settings.excel_path.name} is unlocked; resuming Drive sync.",
                    )

                stats = self.pipeline.process_drive_once()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with self._state_lock:
                    self._last_sync_at = now_str
                    self._last_error = (
                        "Gemini service error during sync; pending receipts kept in Drive."
                        if stats.service_errors > 0
                        else None
                    )
                    self._totals["processed"] += stats.processed
                    self._totals["duplicates"] += stats.duplicates
                    self._totals["failed"] += stats.failed

                self._record_cycle_events(stats, manual=manual)
                return stats
            except DriveAuthError as exc:
                with self._state_lock:
                    previous_error = self._last_error
                    self._last_error = str(exc)
                if manual or previous_error != str(exc):
                    self.record_event("error", f"Google Drive authentication error: {exc}")
                if manual:
                    raise
                return CycleStats()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected error during Drive sync cycle: %s", exc)
                with self._state_lock:
                    self._last_error = str(exc)
                self.record_event("error", f"Drive sync error: {exc}")
                return CycleStats()
            finally:
                with self._state_lock:
                    self._syncing = False

    def _record_cycle_events(self, stats: CycleStats, *, manual: bool) -> None:
        if stats.processed > 0:
            extras: list[str] = []
            if stats.duplicates:
                extras.append(f"{stats.duplicates} duplicate(s)")
            if stats.failed:
                extras.append(f"{stats.failed} failed")
            suffix = f" ({', '.join(extras)})" if extras else ""
            self.record_event(
                "success",
                f"Drive sync processed {stats.processed} receipt(s){suffix}.",
            )
            return

        notes: list[str] = []
        if stats.duplicates:
            notes.append(f"{stats.duplicates} duplicate(s)")
        if stats.failed:
            notes.append(f"{stats.failed} failed")
        if stats.deferred_locked:
            notes.append(f"{stats.deferred_locked} deferred (Excel locked)")
        if stats.service_errors:
            notes.append(f"{stats.service_errors} Gemini service error(s) kept in Drive")

        if notes:
            level = "error" if (stats.failed or stats.service_errors) else "warning"
            self.record_event(level, f"Drive sync: {', '.join(notes)}.")
        elif manual:
            self.record_event("info", "Drive sync complete: no new receipts found.")

    def trigger_sync_now(self) -> dict[str, Any]:
        """Run one immediate Drive poll cycle and return stats alongside watcher status."""
        stats = self._execute_sync_cycle(manual=True)
        return {"stats": asdict(stats), "watcher": self.get_status()}

    def process_uploaded_file(self, file_path: Path, original_filename: str) -> dict[str, Any]:
        """Extract and log a manually uploaded receipt file to Excel under the sync lock."""
        with self._sync_lock:
            file_hash = compute_sha256(file_path)
            if self.pipeline.excel_writer.state_store.has_hash(file_hash):
                with self._state_lock:
                    self._totals["duplicates"] += 1
                self.record_event(
                    "info",
                    f"Skipped uploaded '{original_filename}': duplicate file already processed.",
                )
                return {
                    "status": "duplicate",
                    "filename": original_filename,
                    "receipt_id": None,
                    "store_name": "",
                    "receipt_date": "",
                    "total_amount": 0.0,
                    "currency": "RON",
                    "items_count": 0,
                    "receipt_status": "",
                    "error": None,
                }

            if self.pipeline.excel_writer.is_locked():
                self.record_event(
                    "warning",
                    f"Cannot save uploaded '{original_filename}': {self.settings.excel_path.name} is open/locked.",
                )
                raise WorkbookLockedError(
                    f"Cannot write to {self.settings.excel_path.name} while it is open in another program."
                )

            try:
                receipt = self.pipeline.extractor.extract(file_path)
            except (ReceiptExtractionError, GeminiServiceError) as exc:
                with self._state_lock:
                    self._totals["failed"] += 1
                    self._last_error = str(exc)
                self.record_event("error", f"Upload '{original_filename}' failed: {exc}")
                return {
                    "status": "failed",
                    "filename": original_filename,
                    "error": str(exc),
                }

            try:
                append_result = self.pipeline.excel_writer.append_receipt(
                    receipt,
                    source_filename=original_filename,
                    file_hash=file_hash,
                )
            except WorkbookLockedError:
                self.record_event(
                    "warning",
                    f"Cannot save uploaded '{original_filename}': {self.settings.excel_path.name} is open/locked.",
                )
                raise

            if append_result.status == "added":
                with self._state_lock:
                    self._totals["processed"] += 1
                    self._last_error = None
                level = "warning" if receipt.status == "Needs Review" else "success"
                self.record_event(
                    level,
                    f"Uploaded '{original_filename}': added {append_result.receipt_id} "
                    f"({receipt.store_name}, {receipt.total_amount:.2f} {receipt.currency}).",
                )
                try:
                    self.pipeline.notifier.notify_success(receipt, from_drive=False)
                except Exception:  # noqa: BLE001
                    pass
            else:
                with self._state_lock:
                    self._totals["duplicates"] += 1
                self.record_event(
                    "info",
                    f"Skipped uploaded '{original_filename}': {append_result.reason or 'duplicate receipt'}.",
                )

            return {
                "status": append_result.status,
                "filename": original_filename,
                "receipt_id": append_result.receipt_id,
                "store_name": receipt.store_name,
                "receipt_date": receipt.receipt_date,
                "total_amount": receipt.total_amount,
                "currency": receipt.currency,
                "items_count": len(receipt.items),
                "receipt_status": receipt.status,
                "error": None,
            }

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of watcher runtime state, cumulative totals, and events."""
        with self._state_lock:
            running = bool(self._thread and self._thread.is_alive() and not self._paused)
            return {
                "running": running,
                "paused": self._paused,
                "syncing": self._syncing,
                "poll_interval_seconds": self.settings.poll_interval_seconds,
                "drive_folder_name": self.settings.drive_folder_name,
                "last_sync_at": self._last_sync_at,
                "last_error": self._last_error,
                "totals": dict(self._totals),
                "events": list(self._events),
            }
