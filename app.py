"""CLI entrypoint for the Receipt Budget Automator Web App Dashboard & Watcher."""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import webbrowser

import uvicorn

from main import configure_logging
from src.config import PROJECT_ROOT, Settings
from src.web.api import create_app

logger = logging.getLogger("receipt_web_app")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the web dashboard server."""
    parser = argparse.ArgumentParser(
        description="Receipt Budget Automator — Local Web Dashboard & Google Drive Watcher."
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for the HTTP server (default: APP_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port for the HTTP server (default: APP_PORT or 8765).",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Automatically open the dashboard URL in your default web browser.",
    )
    parser.add_argument(
        "--no-watcher",
        action="store_true",
        help="Start the web dashboard without auto-starting the background Drive watcher.",
    )
    return parser.parse_args(argv)


def _print_startup_banner(url: str, host: str, port: int, settings: Settings) -> None:
    print("=" * 64)
    print("  Receipt Budget Automator — Web Dashboard & Drive Watcher")
    print(f"  Dashboard URL : {url}")
    if host not in {"127.0.0.1", "localhost"}:
        print(f"  Listening on  : http://{host}:{port}")
    print(f"  Excel File    : {settings.excel_path}")
    print(f"  Drive Folder  : {settings.drive_folder_name}")
    print("=" * 64)


def _is_port_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if a TCP server is already accepting connections on host:port."""
    cleaned = host.strip().strip("[]")
    probe_host = "localhost" if cleaned in {"0.0.0.0", "::", ""} else cleaned
    try:
        with socket.create_connection((probe_host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    """Configure logging, create the FastAPI app, and run uvicorn."""
    args = parse_args(argv)
    settings = Settings.from_env(PROJECT_ROOT)
    configure_logging(settings)

    host = args.host or settings.app_host
    port = args.port if args.port is not None else settings.app_port
    browser_host = "localhost" if host in {"127.0.0.1", "0.0.0.0", "::1", "[::1]", "::"} else host
    dashboard_url = f"http://{browser_host}:{port}"

    if _is_port_listening(host, port):
        logger.info("Web dashboard is already running at %s", dashboard_url)
        print(f"Receipt Budget Automator is already running at {dashboard_url}")
        if args.open_browser:
            webbrowser.open(dashboard_url)
        return 0

    start_watcher_on_startup: bool | None = False if args.no_watcher else None
    app = create_app(
        settings=settings,
        start_watcher_on_startup=start_watcher_on_startup,
    )

    _print_startup_banner(dashboard_url, host, port, settings)

    if args.open_browser:
        timer = threading.Timer(1.0, webbrowser.open, args=[dashboard_url])
        timer.daemon = True
        timer.start()

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
