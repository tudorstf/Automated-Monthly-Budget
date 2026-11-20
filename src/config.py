"""Central configuration and category taxonomy for the receipt budget automation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MAIN_CATEGORIES: tuple[str, ...] = (
    "Groceries",
    "Household",
    "Personal Care",
    "Dining & Drinks",
    "Health & Pharmacy",
    "Pets",
    "Electronics & Home",
    "Other",
)

SUBCATEGORY_TO_MAIN: dict[str, str] = {
    "Dairy & Eggs": "Groceries",
    "Meat & Fish": "Groceries",
    "Produce (Fruits & Veg)": "Groceries",
    "Bakery & Bread": "Groceries",
    "Pantry & Staples": "Groceries",
    "Snacks & Sweets": "Groceries",
    "Beverages & Coffee": "Groceries",
    "Alcohol & Tobacco": "Groceries",
    "Bottle Deposit (SGR)": "Groceries",
    "Cleaning & Laundry": "Household",
    "Kitchen & Paper Goods": "Household",
    "Toiletries & Hygiene": "Personal Care",
    "Medicine & Pharmacy": "Health & Pharmacy",
    "Restaurants & Takeout": "Dining & Drinks",
    "Pet Food & Supplies": "Pets",
    "Home & Misc": "Electronics & Home",
    "Unreconciled Adjustment": "Other",
}

SUBCATEGORIES: tuple[str, ...] = tuple(SUBCATEGORY_TO_MAIN.keys())

SUPPORTED_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".pdf",
)

SUPPORTED_MIME_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}

RECONCILIATION_TOLERANCE_RON = 0.05


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings for the pipeline."""

    base_dir: Path
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    drive_folder_name: str
    poll_interval_seconds: int
    excel_path: Path
    credentials_path: Path
    token_path: Path
    state_path: Path
    failed_dir: Path
    logs_dir: Path
    app_host: str = "127.0.0.1"
    app_port: int = 8765
    auto_start_watcher: bool = True
    gemini_timeout_seconds: int = 90

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "Settings":
        """Load settings from `.env` and environment variables."""
        root = (base_dir or PROJECT_ROOT).resolve()
        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        excel_raw = os.getenv("EXCEL_FILE_PATH", "budget_tracker.xlsx").strip() or "budget_tracker.xlsx"
        excel_path = Path(excel_raw)
        if not excel_path.is_absolute():
            excel_path = root / excel_path

        poll_raw = os.getenv("POLL_INTERVAL_SECONDS", "20").strip()
        try:
            poll_interval = max(5, int(poll_raw))
        except ValueError:
            poll_interval = 20

        fallback_raw = os.getenv(
            "GEMINI_FALLBACK_MODELS",
            "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite",
        )
        fallback_models = tuple(m.strip() for m in fallback_raw.split(",") if m.strip())

        app_host = os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
        app_port_raw = os.getenv("APP_PORT", "8765").strip()
        try:
            app_port = int(app_port_raw)
            if not (1 <= app_port <= 65535):
                app_port = 8765
        except ValueError:
            app_port = 8765

        auto_start_raw = os.getenv("AUTO_START_WATCHER", "true").strip().lower()
        auto_start_watcher = auto_start_raw not in {"0", "false", "no"}

        timeout_raw = os.getenv("GEMINI_TIMEOUT_SECONDS", "90").strip()
        try:
            gemini_timeout_seconds = max(60, int(timeout_raw))
        except ValueError:
            gemini_timeout_seconds = 90

        return cls(
            base_dir=root,
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash",
            gemini_fallback_models=fallback_models,
            drive_folder_name=os.getenv("DRIVE_FOLDER_NAME", "receipts").strip() or "receipts",
            poll_interval_seconds=poll_interval,
            excel_path=excel_path,
            credentials_path=root / "credentials.json",
            token_path=root / "token.json",
            state_path=root / "data" / "processed_state.json",
            failed_dir=root / "failed_receipts",
            logs_dir=root / "logs",
            app_host=app_host,
            app_port=app_port,
            auto_start_watcher=auto_start_watcher,
            gemini_timeout_seconds=gemini_timeout_seconds,
        )

    def ensure_directories(self) -> None:
        """Create required local directories if they do not exist."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.excel_path.parent.mkdir(parents=True, exist_ok=True)


def mask_secret(secret: str) -> str:
    """Return a masked display string for sensitive keys and secrets."""
    cleaned = (secret or "").strip()
    if not cleaned or cleaned == "your_gemini_api_key_here":
        return ""
    if len(cleaned) <= 10:
        return "***"
    return f"{cleaned[:6]}...{cleaned[-4:]}"


def update_env_file(base_dir: Path, updates: dict[str, str]) -> Settings:
    """Update `.env` atomically, sync `os.environ`, and return reloaded `Settings`."""
    root = Path(base_dir).resolve()
    env_path = root / ".env"
    example_path = root / ".env.example"

    if env_path.exists():
        raw_text = env_path.read_text(encoding="utf-8")
    elif example_path.exists():
        raw_text = example_path.read_text(encoding="utf-8")
    else:
        raw_text = ""

    sanitized: dict[str, str] = {}
    for raw_key, raw_val in updates.items():
        key = str(raw_key).strip()
        if not key:
            continue
        sanitized[key] = str(raw_val).replace("\r", "").replace("\n", "")

    lines = raw_text.splitlines() if raw_text else []
    seen_keys: set[str] = set()
    new_lines: list[str] = []
    key_pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        match = key_pattern.match(line)
        if match:
            line_key = match.group(1)
            if line_key in sanitized:
                if line_key not in seen_keys:
                    new_lines.append(f"{line_key}={sanitized[line_key]}")
                    seen_keys.add(line_key)
                continue
        new_lines.append(line)

    for key, val in sanitized.items():
        if key not in seen_keys:
            new_lines.append(f"{key}={val}")

    content = ("\n".join(new_lines) + "\n") if new_lines else ""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = env_path.with_name(f"{env_path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, env_path)

    for key, val in sanitized.items():
        os.environ[key] = val

    return Settings.from_env(root)
