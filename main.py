"""CLI entrypoint for the Google Drive receipt-to-Excel budget tracker."""

from __future__ import annotations

import argparse
import getpass
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import threading
from pathlib import Path

from src.config import PROJECT_ROOT, Settings
from src.drive_client import DriveAuthError, GoogleDriveClient
from src.excel_writer import ExcelBudgetWriter, WorkbookLockedError
from src.extractor import ReceiptExtractor
from src.notifier import DesktopNotifier
from src.pipeline import ReceiptPipeline

logger = logging.getLogger("receipt_watcher")


def configure_logging(settings: Settings) -> None:
    """Configure console + rotating file logging."""
    settings.ensure_directories()
    log_file = settings.logs_dir / "receipt_watcher.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def _ensure_env_file(settings: Settings) -> Settings:
    """Ensure `.env` exists and prompt interactively for `GEMINI_API_KEY` if unset."""
    env_path = settings.base_dir / ".env"
    example_path = settings.base_dir / ".env.example"

    if not env_path.exists() and example_path.exists():
        env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")

    current_key = settings.gemini_api_key
    if not current_key or current_key == "your_gemini_api_key_here":
        print("\n[1/3] Gemini API Key Setup")
        print("Get a free key from Google AI Studio: https://aistudio.google.com/apikey")
        entered = getpass.getpass("Paste your GEMINI_API_KEY (input hidden, Enter to skip): ").strip()
        if entered:
            lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
            updated = False
            new_lines: list[str] = []
            for line in lines:
                if line.startswith("GEMINI_API_KEY="):
                    new_lines.append(f"GEMINI_API_KEY={entered}")
                    updated = True
                else:
                    new_lines.append(line)
            if not updated:
                new_lines.insert(0, f"GEMINI_API_KEY={entered}")
            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            print(f"Saved GEMINI_API_KEY to {env_path}")
            # Reload settings with updated key
            import os

            os.environ["GEMINI_API_KEY"] = entered
            settings = Settings.from_env(settings.base_dir)
        else:
            print(f"Skipped. Make sure to set GEMINI_API_KEY in {env_path} before running.")
    else:
        print(f"[1/3] GEMINI_API_KEY is already configured in {env_path}.")
    return settings


def run_setup(settings: Settings) -> int:
    """Run interactive setup for Gemini API key, Google Drive OAuth, and Excel initialization."""
    settings.ensure_directories()
    settings = _ensure_env_file(settings)

    print("\n[2/3] Google Drive OAuth Setup")
    if not settings.credentials_path.exists():
        print(
            f"No custom {settings.credentials_path.name} found; "
            "using built-in Google Drive OAuth client..."
        )

    drive_client = GoogleDriveClient(settings)
    try:
        drive_client.authenticate(interactive=True)
        folder_id = drive_client.ensure_receipts_folder()
    except DriveAuthError as exc:
        print(f"Google Drive authentication failed: {exc}")
        return 1

    print(
        f"Authenticated! Watching Google Drive folder '{settings.drive_folder_name}' "
        f"(id={folder_id})."
    )

    print("\n[3/3] Excel Workbook Initialization")
    excel_writer = ExcelBudgetWriter.from_settings(settings)
    excel_writer.ensure_initialized()
    print(f"Workbook ready at: {settings.excel_path}")
    print("\nSetup complete! Next steps:")
    print("  - Test once:          .venv/bin/python main.py --once")
    print("  - Start auto-watcher: ./scripts/install_service.sh")
    return 0


def build_pipeline(settings: Settings, *, notify: bool) -> ReceiptPipeline:
    """Assemble pipeline components from settings."""
    drive_client = GoogleDriveClient(settings)
    extractor = ReceiptExtractor(settings)
    excel_writer = ExcelBudgetWriter.from_settings(settings)
    notifier = DesktopNotifier(enabled=notify)
    return ReceiptPipeline(
        settings=settings,
        drive_client=drive_client,
        extractor=extractor,
        excel_writer=excel_writer,
        notifier=notifier,
    )


def run_once(settings: Settings, *, notify: bool) -> int:
    """Poll Google Drive once, process pending files, and exit."""
    pipeline = build_pipeline(settings, notify=notify)
    try:
        pipeline.excel_writer.ensure_initialized()
    except WorkbookLockedError as exc:
        logger.warning("%s", exc)

    try:
        stats = pipeline.process_drive_once()
    except DriveAuthError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Cycle complete: added=%d, duplicates=%d, failed=%d, locked=%d, service_errors=%d",
        stats.processed,
        stats.duplicates,
        stats.failed,
        stats.deferred_locked,
        stats.service_errors,
    )
    return 0 if stats.service_errors == 0 else 1


def run_watch(settings: Settings, *, notify: bool) -> int:
    """Run continuous polling daemon until SIGINT or SIGTERM."""
    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("Received signal %d; shutting down gracefully...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    pipeline = build_pipeline(settings, notify=notify)
    try:
        pipeline.excel_writer.ensure_initialized()
    except WorkbookLockedError:
        pass

    logger.info(
        "Starting receipt watcher: polling Drive folder '%s' every %ds -> %s",
        settings.drive_folder_name,
        settings.poll_interval_seconds,
        settings.excel_path,
    )

    while not stop_event.is_set():
        try:
            pipeline.process_drive_once()
        except DriveAuthError as exc:
            logger.error("Authentication error: %s", exc)
            pipeline.notifier.send(
                "Receipt Watcher Auth Required",
                "Run `python main.py --setup` to reconnect Google Drive.",
                urgency="critical",
            )
            return 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during Drive polling cycle: %s", exc)

        stop_event.wait(timeout=settings.poll_interval_seconds)

    logger.info("Receipt watcher stopped.")
    return 0


def run_local(settings: Settings, local_path: Path, *, notify: bool) -> int:
    """Process a single local image or PDF receipt file."""
    pipeline = build_pipeline(settings, notify=notify)
    pipeline.excel_writer.ensure_initialized()
    try:
        outcome = pipeline.process_local_file(local_path)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    logger.info("Local file outcome for '%s': %s", local_path.name, outcome)
    return 0 if outcome in {"added", "duplicate"} else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automated Google Drive receipt-to-Excel budget tracker."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--setup",
        action="store_true",
        help="Configure Gemini API key, Google Drive OAuth login, and Excel workbook.",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Check Google Drive receipts once, process pending files, and exit.",
    )
    mode.add_argument(
        "--watch",
        action="store_true",
        help="Continuously watch Google Drive receipts on the configured interval.",
    )
    mode.add_argument(
        "--local",
        type=Path,
        metavar="FILE",
        help="Process a single local receipt photo or PDF file.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable Linux desktop notifications.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.from_env(PROJECT_ROOT)
    configure_logging(settings)
    notify = not args.no_notify

    if args.setup:
        return run_setup(settings)
    if args.once:
        return run_once(settings, notify=notify)
    if args.watch:
        return run_watch(settings, notify=notify)
    if args.local is not None:
        return run_local(settings, args.local, notify=notify)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
