"""Linux desktop notification helper (`notify-send`)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable

from src.models import ExtractedReceipt

logger = logging.getLogger(__name__)


class DesktopNotifier:
    """Sends non-blocking Linux desktop notifications via `notify-send`."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        app_name: str = "Budget Receipt Watcher",
        runner: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.app_name = app_name
        self._notify_bin = shutil.which("notify-send")
        self._runner = runner

    def send(self, title: str, body: str, *, urgency: str = "normal") -> None:
        """Display a desktop notification without raising on headless environments."""
        if not self.enabled:
            return
        if self._runner is not None:
            self._runner([title, body, urgency])
            return
        if not self._notify_bin:
            return

        cmd = [
            self._notify_bin,
            "--app-name",
            self.app_name,
            "--urgency",
            urgency,
            title,
            body,
        ]
        try:
            subprocess.run(cmd, check=False, timeout=5, capture_output=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Desktop notification failed: %s", exc)

    def notify_success(self, receipt: ExtractedReceipt, *, from_drive: bool = True) -> None:
        """Notify user when a receipt is successfully logged to Excel."""
        source_note = " & deleted from Drive" if from_drive else ""
        if receipt.status == "Needs Review":
            adj_sum = sum(
                item.net_total
                for item in receipt.items
                if item.raw_name == "RECONCILIATION_ADJUSTMENT"
            )
            title = f"Receipt Added (Needs Review): {receipt.store_name}"
            body = (
                f"{receipt.total_amount:.2f} {receipt.currency} — "
                f"{adj_sum:.2f} {receipt.currency} reconciliation adjustment{source_note}"
            )
            self.send(title, body, urgency="normal")
            return

        title = f"Receipt Added: {receipt.store_name} ({receipt.total_amount:.2f} {receipt.currency})"
        body = f"{len(receipt.items)} items logged to budget_tracker.xlsx{source_note}"
        self.send(title, body, urgency="normal")

    def notify_failure(self, filename: str, *, removed_from_drive: bool = True) -> None:
        """Notify user when a receipt could not be parsed and was moved to `failed_receipts/`."""
        drive_note = " & removed from Drive" if removed_from_drive else ""
        title = f"Receipt Failed: {filename}"
        body = f"Saved to local failed_receipts/{drive_note}"
        self.send(title, body, urgency="critical")
