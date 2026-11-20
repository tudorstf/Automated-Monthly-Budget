"""End-to-end receipt processing pipeline coordinating Drive, Gemini, Excel, and notifications."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.config import Settings
from src.drive_client import DriveFile, GoogleDriveClient
from src.excel_writer import (
    ExcelBudgetWriter,
    WorkbookLockedError,
    compute_sha256,
)
from src.extractor import GeminiServiceError, ReceiptExtractionError, ReceiptExtractor
from src.notifier import DesktopNotifier

logger = logging.getLogger(__name__)


@dataclass
class CycleStats:
    """Summary counts from a single Drive polling cycle."""

    processed: int = 0
    duplicates: int = 0
    failed: int = 0
    deferred_locked: int = 0
    service_errors: int = 0


def _unique_destination(directory: Path, filename: str) -> Path:
    """Return a non-colliding file path inside `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name or "receipt.bin"
    candidate = directory / safe_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    numbered = directory / f"{stem}_{timestamp}{suffix}"
    counter = 1
    while numbered.exists():
        numbered = directory / f"{stem}_{timestamp}_{counter}{suffix}"
        counter += 1
    return numbered


class ReceiptPipeline:
    """Coordinates receipt ingestion from Google Drive or local paths into Excel."""

    def __init__(
        self,
        *,
        settings: Settings,
        drive_client: GoogleDriveClient | None,
        extractor: ReceiptExtractor,
        excel_writer: ExcelBudgetWriter,
        notifier: DesktopNotifier,
    ) -> None:
        self.settings = settings
        self.drive_client = drive_client
        self.extractor = extractor
        self.excel_writer = excel_writer
        self.notifier = notifier
        self._was_locked = False

    def process_drive_once(self) -> CycleStats:
        """Poll Google Drive `receipts` once and process all pending files."""
        if self.drive_client is None:
            raise RuntimeError("Drive client is not configured.")

        stats = CycleStats()
        if self.excel_writer.is_locked():
            self._was_locked = True
            logger.warning(
                "Workbook %s is open/locked; deferring Drive processing until closed.",
                self.settings.excel_path.name,
            )
            return stats

        if self._was_locked:
            self._was_locked = False
            logger.info(
                "Workbook %s is now unlocked; resuming Drive monitoring.",
                self.settings.excel_path.name,
            )

        pending_files = self.drive_client.list_pending_files()
        if not pending_files:
            return stats

        logger.info("Found %d pending file(s) in Google Drive '%s'.", len(pending_files), self.settings.drive_folder_name)

        for drive_file in pending_files:
            outcome = self._process_single_drive_file(drive_file)
            if outcome == "added":
                stats.processed += 1
            elif outcome == "duplicate":
                stats.duplicates += 1
            elif outcome == "failed":
                stats.failed += 1
            elif outcome == "locked":
                stats.deferred_locked += 1
                break
            elif outcome == "service_error":
                stats.service_errors += 1
                break

        return stats

    def _process_single_drive_file(self, drive_file: DriveFile) -> str:
        assert self.drive_client is not None
        safe_name = Path(drive_file.name).name or f"{drive_file.id}{drive_file.extension}"

        with tempfile.TemporaryDirectory(prefix="receipt_") as tmp_dir:
            temp_path = Path(tmp_dir) / safe_name
            self.drive_client.download_file(drive_file.id, temp_path)
            file_hash = compute_sha256(temp_path)

            if self.excel_writer.state_store.has_hash(file_hash):
                logger.info("Duplicate file hash for '%s'; deleting from Google Drive.", safe_name)
                self.drive_client.delete_file_permanently(drive_file.id)
                return "duplicate"

            try:
                receipt = self.extractor.extract(temp_path)
            except ReceiptExtractionError as exc:
                self._archive_failed_file(
                    temp_path,
                    safe_name,
                    exc,
                    removed_from_drive=True,
                )
                self.drive_client.delete_file_permanently(drive_file.id)
                return "failed"
            except GeminiServiceError as exc:
                logger.error(
                    "Gemini service error while processing '%s' (kept in Drive for retry): %s",
                    safe_name,
                    exc,
                )
                return "service_error"

            try:
                append_result = self.excel_writer.append_receipt(
                    receipt,
                    source_filename=safe_name,
                    file_hash=file_hash,
                )
            except WorkbookLockedError:
                logger.warning(
                    "Workbook became locked before saving '%s'; keeping in Drive for retry.",
                    safe_name,
                )
                return "locked"

            self.drive_client.delete_file_permanently(drive_file.id)
            if append_result.status == "added":
                logger.info(
                    "Logged %s (%s, %.2f %s) -> %s & permanently deleted '%s' from Drive.",
                    append_result.receipt_id,
                    receipt.store_name,
                    receipt.total_amount,
                    receipt.currency,
                    self.settings.excel_path.name,
                    safe_name,
                )
                self.notifier.notify_success(receipt, from_drive=True)
                return "added"

            logger.info(
                "Duplicate receipt detected for '%s' (%s); permanently deleted from Drive.",
                safe_name,
                append_result.reason,
            )
            return "duplicate"

    def process_local_file(self, local_path: Path) -> str:
        """Process a single local receipt image or PDF without touching Google Drive."""
        resolved = local_path.resolve()
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"Local receipt file not found: {resolved}")

        file_hash = compute_sha256(resolved)
        if self.excel_writer.state_store.has_hash(file_hash):
            logger.info("Duplicate file hash for local file '%s'; skipping.", resolved.name)
            return "duplicate"

        try:
            receipt = self.extractor.extract(resolved)
        except ReceiptExtractionError as exc:
            self._archive_failed_file(
                resolved,
                resolved.name,
                exc,
                removed_from_drive=False,
            )
            return "failed"

        append_result = self.excel_writer.append_receipt(
            receipt,
            source_filename=resolved.name,
            file_hash=file_hash,
        )
        if append_result.status == "added":
            logger.info(
                "Logged local receipt %s (%s, %.2f %s) -> %s.",
                append_result.receipt_id,
                receipt.store_name,
                receipt.total_amount,
                receipt.currency,
                self.settings.excel_path.name,
            )
            self.notifier.notify_success(receipt, from_drive=False)
            return "added"

        logger.info("Duplicate local receipt '%s': %s", resolved.name, append_result.reason)
        return "duplicate"

    def _archive_failed_file(
        self,
        source_path: Path,
        display_name: str,
        error: Exception,
        *,
        removed_from_drive: bool,
    ) -> Path:
        """Copy an unreadable receipt into `failed_receipts/` with an error log sidecar."""
        logger.error("Failed to parse '%s': %s", display_name, error)
        failed_path = _unique_destination(self.settings.failed_dir, display_name)
        shutil.copy2(source_path, failed_path)
        failed_path.with_name(f"{failed_path.name}.error.txt").write_text(
            f"Source: {display_name}\nTime: {datetime.now().isoformat()}\nError: {error}\n",
            encoding="utf-8",
        )
        self.notifier.notify_failure(display_name, removed_from_drive=removed_from_drive)
        return failed_path
