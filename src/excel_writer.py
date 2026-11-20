"""Excel workbook engine (`budget_tracker.xlsx`) and deduplication state manager."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.config import MAIN_CATEGORIES, SUBCATEGORIES, Settings
from src.models import ExtractedReceipt

RECEIPTS_HEADERS: tuple[str, ...] = (
    "Receipt ID",
    "Date",
    "Time",
    "Month (YYYY-MM)",
    "Store Name",
    "Payment Method",
    "Items Count",
    "Discounts Saved (RON)",
    "SGR Deposit (RON)",
    "Total Paid (RON)",
    "Currency",
    "Status",
    "Source File",
    "Processed At",
)

ITEMS_HEADERS: tuple[str, ...] = (
    "Receipt ID",
    "Date",
    "Month (YYYY-MM)",
    "Store Name",
    "Clean Product Name",
    "Raw Receipt Name",
    "Main Category",
    "Subcategory",
    "Quantity",
    "Unit",
    "Unit Price (RON)",
    "Discount (RON)",
    "Net Total (RON)",
)

# Styling palette
_HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
_SUBHEADER_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
_KPI_LABEL_FILL = PatternFill(start_color="EDF2F8", end_color="EDF2F8", fill_type="solid")
_TOTAL_FILL = PatternFill(start_color="E9EDF4", end_color="E9EDF4", fill_type="solid")
_AMBER_FILL = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
_ZEBRA_FILL = PatternFill(start_color="F9FAFC", end_color="F9FAFC", fill_type="solid")

_HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
_TITLE_FONT = Font(name="Calibri", size=15, bold=True, color="1F4E78")
_SECTION_FONT = Font(name="Calibri", size=12, bold=True, color="1F4E78")
_BOLD_FONT = Font(name="Calibri", size=11, bold=True, color="111111")
_REGULAR_FONT = Font(name="Calibri", size=11, bold=False, color="111111")
_KPI_VALUE_FONT = Font(name="Calibri", size=13, bold=True, color="1F4E78")

_THIN_BORDER_SIDE = Side(border_style="thin", color="D0D7DE")
_CELL_BORDER = Border(
    left=_THIN_BORDER_SIDE,
    right=_THIN_BORDER_SIDE,
    top=_THIN_BORDER_SIDE,
    bottom=_THIN_BORDER_SIDE,
)
_TOTAL_BORDER = Border(
    top=Side(border_style="thin", color="1F4E78"),
    bottom=Side(border_style="double", color="1F4E78"),
    left=_THIN_BORDER_SIDE,
    right=_THIN_BORDER_SIDE,
)


class WorkbookLockedError(RuntimeError):
    """Raised when the Excel workbook is currently open/locked by another program."""


@dataclass(frozen=True)
class AppendResult:
    """Outcome of attempting to append a receipt to the workbook."""

    status: str  # "added" | "duplicate"
    receipt_id: str | None
    reason: str = ""


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest for a local file."""
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_store_name(store_name: str) -> str:
    """Normalize merchant names for deduplication fingerprinting."""
    cleaned = re.sub(r"\b(s\.?r\.?l\.?|s\.?a\.?|romania|ro)\b", "", store_name.lower())
    cleaned = re.sub(r"[^a-z0-9]+", "", cleaned)
    return cleaned or "unknown"


def compute_receipt_fingerprint(receipt: ExtractedReceipt) -> str:
    """Build deterministic semantic fingerprint to catch re-photographed duplicates."""
    base_items_count = sum(
        1 for item in receipt.items if item.raw_name != "RECONCILIATION_ADJUSTMENT"
    )
    parts = (
        receipt.receipt_date.strip(),
        normalize_store_name(receipt.store_name),
        f"{receipt.total_amount:.2f}",
        str(base_items_count),
    )
    return "|".join(parts)


class ProcessedStateStore:
    """Persists processed file hashes and receipt fingerprints in JSON."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"hashes": {}, "fingerprints": {}}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("hashes", {})
                raw.setdefault("fingerprints", {})
                return raw
        except (json.JSONDecodeError, OSError):
            pass
        return {"hashes": {}, "fingerprints": {}}

    def has_hash(self, file_hash: str) -> bool:
        with self._lock:
            return file_hash in self._data["hashes"]

    def has_fingerprint(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._data["fingerprints"]

    def record(
        self,
        *,
        file_hash: str,
        fingerprint: str | None = None,
        receipt_id: str | None = None,
        source_name: str = "",
    ) -> None:
        with self._lock:
            now_iso = datetime.now().isoformat(timespec="seconds")
            effective_receipt_id = receipt_id
            if effective_receipt_id is None and fingerprint:
                effective_receipt_id = self._data["fingerprints"].get(fingerprint, {}).get(
                    "receipt_id"
                )
            if effective_receipt_id is None:
                effective_receipt_id = self._data["hashes"].get(file_hash, {}).get("receipt_id")

            self._data["hashes"][file_hash] = {
                "receipt_id": effective_receipt_id,
                "source_name": source_name,
                "recorded_at": now_iso,
            }
            if fingerprint:
                existing_fp = self._data["fingerprints"].get(fingerprint)
                if existing_fp and receipt_id is None:
                    # Preserve existing fingerprint link to the original receipt
                    if existing_fp.get("receipt_id") is None and effective_receipt_id is not None:
                        existing_fp["receipt_id"] = effective_receipt_id
                else:
                    self._data["fingerprints"][fingerprint] = {
                        "receipt_id": effective_receipt_id,
                        "source_name": source_name,
                        "recorded_at": now_iso,
                    }
            self._save()

    def remove_receipt(
        self,
        receipt_id: str,
        *,
        fingerprints: set[str] | None = None,
        source_names: set[str] | None = None,
    ) -> None:
        """Remove persisted hash and fingerprint entries associated with a deleted receipt."""
        with self._lock:
            fp_targets = fingerprints or set()
            src_targets = {s for s in (source_names or set()) if s}
            removed = False

            hashes = self._data.get("hashes", {})
            for h_key in list(hashes.keys()):
                meta = hashes.get(h_key) or {}
                meta_rid = meta.get("receipt_id")
                if meta_rid == receipt_id or (
                    meta_rid is None and src_targets and meta.get("source_name") in src_targets
                ):
                    del hashes[h_key]
                    removed = True

            fps = self._data.get("fingerprints", {})
            for fp_key in list(fps.keys()):
                meta = fps.get(fp_key) or {}
                meta_rid = meta.get("receipt_id")
                if (
                    fp_key in fp_targets
                    or meta_rid == receipt_id
                    or (
                        meta_rid is None
                        and src_targets
                        and meta.get("source_name") in src_targets
                    )
                ):
                    del fps[fp_key]
                    removed = True

            if removed:
                self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self.state_path)


class ExcelBudgetWriter:
    """Manages reading, updating, styling, and dashboard rebuilding of `budget_tracker.xlsx`."""

    def __init__(self, excel_path: Path, state_store: ProcessedStateStore) -> None:
        self.excel_path = excel_path
        self.state_store = state_store
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExcelBudgetWriter":
        settings.ensure_directories()
        return cls(settings.excel_path, ProcessedStateStore(settings.state_path))

    def is_locked(self) -> bool:
        """Return True if LibreOffice or Excel holds a lock file next to the workbook."""
        parent = self.excel_path.parent
        name = self.excel_path.name
        lock_candidates = (
            parent / f".~lock.{name}#",
            parent / f"~${name}",
        )
        return any(candidate.exists() for candidate in lock_candidates)

    def ensure_initialized(self) -> None:
        """Create a styled empty workbook if `budget_tracker.xlsx` does not exist."""
        with self._lock:
            if self.excel_path.exists():
                return
            if self.is_locked():
                raise WorkbookLockedError(f"Workbook is currently open/locked: {self.excel_path}")
            wb = Workbook()
            try:
                # Rename default active sheet to Monthly Dashboard
                dash_ws = wb.active
                dash_ws.title = "Monthly Dashboard"
                receipts_ws = wb.create_sheet("Receipts")
                items_ws = wb.create_sheet("Items")
                self._write_headers(receipts_ws, RECEIPTS_HEADERS)
                self._write_headers(items_ws, ITEMS_HEADERS)
                self._rebuild_dashboard(wb)
                self._style_all_data_sheets(receipts_ws, items_ws)
                self._save_atomic(wb)
            finally:
                wb.close()

    def append_receipt(
        self,
        receipt: ExtractedReceipt,
        *,
        source_filename: str,
        file_hash: str,
    ) -> AppendResult:
        """Append a reconciled receipt and its items to `budget_tracker.xlsx`."""
        with self._lock:
            fingerprint = compute_receipt_fingerprint(receipt)

            if self.state_store.has_hash(file_hash):
                return AppendResult(status="duplicate", receipt_id=None, reason="Duplicate file hash")
            if self.state_store.has_fingerprint(fingerprint):
                self.state_store.record(
                    file_hash=file_hash,
                    fingerprint=fingerprint,
                    receipt_id=None,
                    source_name=source_filename,
                )
                return AppendResult(
                    status="duplicate",
                    receipt_id=None,
                    reason="Duplicate receipt fingerprint (date/store/total/items)",
                )

            if self.is_locked():
                raise WorkbookLockedError(
                    f"Cannot write to {self.excel_path.name} while it is open in another program."
                )

            wb = self._load_or_create_workbook()
            try:
                receipts_ws = wb["Receipts"]
                items_ws = wb["Items"]

                receipt_id = self._next_receipt_id(receipts_ws)
                processed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                receipt_row = [
                    receipt_id,
                    receipt.receipt_date,
                    receipt.receipt_time,
                    receipt.month_key,
                    receipt.store_name,
                    receipt.payment_method,
                    len(receipt.items),
                    round(receipt.subtotal_discounts, 2),
                    round(receipt.sgr_deposit_total, 2),
                    round(receipt.total_amount, 2),
                    receipt.currency,
                    receipt.status,
                    source_filename,
                    processed_at,
                ]
                receipts_ws.append(receipt_row)

                for item in receipt.items:
                    items_ws.append(
                        [
                            receipt_id,
                            receipt.receipt_date,
                            receipt.month_key,
                            receipt.store_name,
                            item.clean_name,
                            item.raw_name,
                            item.main_category,
                            item.subcategory,
                            round(item.quantity, 3),
                            item.unit,
                            round(item.unit_price, 2),
                            round(item.discount_amount, 2),
                            round(item.net_total, 2),
                        ]
                    )

                self._style_all_data_sheets(receipts_ws, items_ws)
                self._rebuild_dashboard(wb)
                self._save_atomic(wb)
            finally:
                wb.close()

            self.state_store.record(
                file_hash=file_hash,
                fingerprint=fingerprint,
                receipt_id=receipt_id,
                source_name=source_filename,
            )
            return AppendResult(status="added", receipt_id=receipt_id)

    def get_dashboard_payload(self, month_filter: str | None = None) -> dict[str, Any]:
        """Read workbook data and return aggregated KPIs, breakdowns, receipts, and items."""
        with self._lock:
            if not self.excel_path.exists() and not self.is_locked():
                self.ensure_initialized()

            all_receipts: list[dict[str, Any]] = []
            all_items: list[dict[str, Any]] = []

            if self.excel_path.exists():
                wb = load_workbook(self.excel_path, data_only=True)
                try:
                    if "Receipts" in wb.sheetnames:
                        receipts_ws = wb["Receipts"]
                        for row in receipts_ws.iter_rows(min_row=2, values_only=True):
                            if not any(row):
                                continue
                            all_receipts.append(self._row_to_receipt_dict(row))

                    if "Items" in wb.sheetnames:
                        items_ws = wb["Items"]
                        item_counters: dict[str, int] = defaultdict(int)
                        for row in items_ws.iter_rows(min_row=2, values_only=True):
                            if not any(row):
                                continue
                            receipt_id = str(row[0] or "").strip()
                            idx = item_counters[receipt_id]
                            item_counters[receipt_id] += 1
                            all_items.append(self._row_to_item_dict(row, idx))
                finally:
                    wb.close()

            months_set = {r["month"] for r in all_receipts if r["month"]} | {
                i["month"] for i in all_items if i["month"]
            }
            months = sorted(months_set)

            month_receipt_agg: dict[str, dict[str, Any]] = {
                m: {"spent": 0.0, "discounts": 0.0, "count": 0} for m in months
            }
            for receipt in all_receipts:
                m = receipt["month"]
                if m:
                    agg = month_receipt_agg[m]
                    agg["spent"] += receipt["total_paid"]
                    agg["discounts"] += receipt["discounts_saved"]
                    agg["count"] += 1

            by_month = [
                {
                    "month": m,
                    "total_spent": round(month_receipt_agg[m]["spent"], 2),
                    "discounts": round(month_receipt_agg[m]["discounts"], 2),
                    "receipt_count": month_receipt_agg[m]["count"],
                }
                for m in months
            ]

            cleaned_filter = (month_filter or "").strip()
            active_month_filter = (
                None if (not cleaned_filter or cleaned_filter.lower() == "all") else cleaned_filter
            )

            if active_month_filter is not None:
                filtered_receipts = [r for r in all_receipts if r["month"] == active_month_filter]
                filtered_items = [i for i in all_items if i["month"] == active_month_filter]
            else:
                filtered_receipts = list(all_receipts)
                filtered_items = list(all_items)

            total_spent = round(sum(r["total_paid"] for r in filtered_receipts), 2)
            total_discounts = round(sum(r["discounts_saved"] for r in filtered_receipts), 2)
            total_sgr = round(sum(r["sgr_deposit"] for r in filtered_receipts), 2)
            receipt_count = len(filtered_receipts)
            needs_review_count = sum(1 for r in filtered_receipts if r["status"] == "Needs Review")
            items_count = len(filtered_items)

            if active_month_filter is not None:
                current_month = active_month_filter
                current_month_spent = total_spent
            else:
                calendar_month = datetime.now().strftime("%Y-%m")
                cal_spent = round(month_receipt_agg.get(calendar_month, {}).get("spent", 0.0), 2)
                if cal_spent != 0.0:
                    current_month = calendar_month
                    current_month_spent = cal_spent
                else:
                    months_with_spent = [
                        m for m in months if round(month_receipt_agg[m]["spent"], 2) != 0.0
                    ]
                    if months_with_spent:
                        current_month = months_with_spent[-1]
                        current_month_spent = round(month_receipt_agg[current_month]["spent"], 2)
                    elif months:
                        current_month = months[-1]
                        current_month_spent = 0.0
                    else:
                        current_month = calendar_month
                        current_month_spent = 0.0

            kpis = {
                "total_spent": total_spent,
                "current_month": current_month,
                "current_month_spent": current_month_spent,
                "total_discounts": total_discounts,
                "total_sgr": total_sgr,
                "receipt_count": receipt_count,
                "needs_review_count": needs_review_count,
                "items_count": items_count,
            }

            main_totals: dict[str, float] = defaultdict(float)
            main_by_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            for item in filtered_items:
                mc = item["main_category"]
                main_totals[mc] += item["net_total"]
                if item["month"]:
                    main_by_month[mc][item["month"]] += item["net_total"]

            grand_items_total = sum(main_totals.values())
            by_main_category = [
                {
                    "main_category": mc,
                    "total": round(tot, 2),
                    "share": round(tot / grand_items_total, 4) if grand_items_total > 0 else 0.0,
                    "by_month": {
                        m: round(val, 2) for m, val in sorted(main_by_month[mc].items())
                    },
                }
                for mc, tot in main_totals.items()
            ]
            by_main_category.sort(key=lambda x: (-x["total"], x["main_category"]))

            sub_totals: dict[tuple[str, str], float] = defaultdict(float)
            sub_by_month: dict[tuple[str, str], dict[str, float]] = defaultdict(
                lambda: defaultdict(float)
            )
            for item in filtered_items:
                pair = (item["main_category"], item["subcategory"])
                sub_totals[pair] += item["net_total"]
                if item["month"]:
                    sub_by_month[pair][item["month"]] += item["net_total"]

            by_subcategory = [
                {
                    "main_category": mc,
                    "subcategory": sc,
                    "total": round(tot, 2),
                    "by_month": {
                        m: round(val, 2) for m, val in sorted(sub_by_month[(mc, sc)].items())
                    },
                }
                for (mc, sc), tot in sub_totals.items()
            ]
            by_subcategory.sort(key=lambda x: (-x["total"], x["main_category"], x["subcategory"]))

            store_agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "spent": 0.0})
            for receipt in filtered_receipts:
                store = receipt["store_name"]
                store_agg[store]["count"] += 1
                store_agg[store]["spent"] += receipt["total_paid"]

            by_store = []
            for store, agg in store_agg.items():
                cnt = agg["count"]
                tot = round(agg["spent"], 2)
                by_store.append(
                    {
                        "store_name": store,
                        "receipt_count": cnt,
                        "total_spent": tot,
                        "average_trip": round(tot / cnt, 2) if cnt > 0 else 0.0,
                    }
                )
            by_store.sort(key=lambda x: (-x["total_spent"], x["store_name"]))

            receipt_sort_meta = {
                r["receipt_id"]: (
                    r["date"],
                    r["time"],
                    self._receipt_id_num(r["receipt_id"]),
                    r["receipt_id"],
                )
                for r in filtered_receipts
            }
            sorted_receipts = sorted(
                filtered_receipts,
                key=lambda r: (
                    r["date"],
                    r["time"],
                    self._receipt_id_num(r["receipt_id"]),
                    r["receipt_id"],
                ),
                reverse=True,
            )
            sorted_items = sorted(
                filtered_items,
                key=lambda i: (
                    i["date"],
                    receipt_sort_meta.get(i["receipt_id"], ("", "", 0, ""))[1],
                    self._receipt_id_num(i["receipt_id"]),
                    i["receipt_id"],
                    -i["item_index"],
                ),
                reverse=True,
            )

            return {
                "months": months,
                "by_month": by_month,
                "kpis": kpis,
                "by_main_category": by_main_category,
                "by_subcategory": by_subcategory,
                "by_store": by_store,
                "receipts": sorted_receipts,
                "items": sorted_items,
            }

    def update_receipt_meta(self, receipt_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update receipt-level metadata and propagate date/store changes to child items."""
        with self._lock:
            if self.is_locked():
                raise WorkbookLockedError(
                    f"Cannot write to {self.excel_path.name} while it is open in another program."
                )
            if not self.excel_path.exists():
                raise KeyError(f"Receipt not found: {receipt_id}")

            wb = self._load_or_create_workbook()
            try:
                receipts_ws = wb["Receipts"]
                items_ws = wb["Items"]

                target_row_idx: int | None = None
                for row_idx in range(2, receipts_ws.max_row + 1):
                    val = str(receipts_ws.cell(row=row_idx, column=1).value or "").strip()
                    if val == receipt_id:
                        target_row_idx = row_idx
                        break

                if target_row_idx is None:
                    raise KeyError(f"Receipt not found: {receipt_id}")

                old_date = self._norm_date(receipts_ws.cell(row=target_row_idx, column=2).value)
                old_store = str(receipts_ws.cell(row=target_row_idx, column=5).value or "").strip()

                if "date" in updates and updates["date"] is not None:
                    date_val = str(updates["date"]).strip()
                    try:
                        parsed = datetime.strptime(date_val, "%Y-%m-%d").date()
                    except ValueError as exc:
                        raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {date_val!r}") from exc
                    normalized_date = parsed.isoformat()
                    receipts_ws.cell(row=target_row_idx, column=2, value=normalized_date)
                    receipts_ws.cell(row=target_row_idx, column=4, value=normalized_date[:7])

                if "time" in updates and updates["time"] is not None:
                    receipts_ws.cell(row=target_row_idx, column=3, value=str(updates["time"]).strip())

                if "store_name" in updates and updates["store_name"] is not None:
                    store_val = str(updates["store_name"]).strip()
                    if not store_val:
                        raise ValueError("store_name cannot be empty")
                    receipts_ws.cell(row=target_row_idx, column=5, value=store_val)

                if "payment_method" in updates and updates["payment_method"] is not None:
                    pm_val = str(updates["payment_method"]).strip()
                    if not pm_val:
                        raise ValueError("payment_method cannot be empty")
                    receipts_ws.cell(row=target_row_idx, column=6, value=pm_val)

                if "discounts_saved" in updates and updates["discounts_saved"] is not None:
                    disc_val = round(self._parse_finite_float(updates["discounts_saved"], "discounts_saved"), 2)
                    receipts_ws.cell(row=target_row_idx, column=8, value=disc_val)

                if "sgr_deposit" in updates and updates["sgr_deposit"] is not None:
                    sgr_val = round(self._parse_finite_float(updates["sgr_deposit"], "sgr_deposit"), 2)
                    receipts_ws.cell(row=target_row_idx, column=9, value=sgr_val)

                if "total_paid" in updates and updates["total_paid"] is not None:
                    total_val = round(self._parse_finite_float(updates["total_paid"], "total_paid"), 2)
                    receipts_ws.cell(row=target_row_idx, column=10, value=total_val)

                if "currency" in updates and updates["currency"] is not None:
                    curr_val = str(updates["currency"]).strip().upper()[:5]
                    if not curr_val:
                        raise ValueError("currency cannot be empty")
                    receipts_ws.cell(row=target_row_idx, column=11, value=curr_val)

                if "status" in updates and updates["status"] is not None:
                    status_val = str(updates["status"]).strip()
                    if status_val not in {"OK", "Needs Review"}:
                        raise ValueError("status must be 'OK' or 'Needs Review'")
                    receipts_ws.cell(row=target_row_idx, column=12, value=status_val)

                new_date = self._norm_date(receipts_ws.cell(row=target_row_idx, column=2).value)
                new_month = str(receipts_ws.cell(row=target_row_idx, column=4).value or "").strip()
                new_store = str(receipts_ws.cell(row=target_row_idx, column=5).value or "").strip()

                if new_date != old_date or new_store != old_store:
                    for item_row_idx in range(2, items_ws.max_row + 1):
                        item_rid = str(items_ws.cell(row=item_row_idx, column=1).value or "").strip()
                        if item_rid == receipt_id:
                            items_ws.cell(row=item_row_idx, column=2, value=new_date)
                            items_ws.cell(row=item_row_idx, column=3, value=new_month)
                            items_ws.cell(row=item_row_idx, column=4, value=new_store)

                self._style_all_data_sheets(receipts_ws, items_ws)
                self._rebuild_dashboard(wb)
                self._save_atomic(wb)

                updated_row = tuple(
                    receipts_ws.cell(row=target_row_idx, column=col).value
                    for col in range(1, len(RECEIPTS_HEADERS) + 1)
                )
                return self._row_to_receipt_dict(updated_row)
            finally:
                wb.close()

    def update_item(
        self,
        receipt_id: str,
        item_index: int,
        updates: dict[str, Any],
        *,
        recalculate_receipt_total: bool = True,
    ) -> dict[str, Any]:
        """Update a single line item and recalculate its parent receipt summary."""
        with self._lock:
            if self.is_locked():
                raise WorkbookLockedError(
                    f"Cannot write to {self.excel_path.name} while it is open in another program."
                )
            if not self.excel_path.exists() or item_index < 0:
                raise KeyError(f"Item {item_index} not found for receipt {receipt_id}")

            wb = self._load_or_create_workbook()
            try:
                receipts_ws = wb["Receipts"]
                items_ws = wb["Items"]

                matching_item_rows: list[int] = []
                for row_idx in range(2, items_ws.max_row + 1):
                    val = str(items_ws.cell(row=row_idx, column=1).value or "").strip()
                    if val == receipt_id:
                        matching_item_rows.append(row_idx)

                if item_index >= len(matching_item_rows):
                    raise KeyError(f"Item {item_index} not found for receipt {receipt_id}")

                target_row_idx = matching_item_rows[item_index]

                if "main_category" in updates and updates["main_category"] is not None:
                    main_cat = str(updates["main_category"]).strip()
                    if main_cat not in MAIN_CATEGORIES:
                        raise ValueError(f"Invalid main_category: {main_cat!r}")
                    items_ws.cell(row=target_row_idx, column=7, value=main_cat)

                if "subcategory" in updates and updates["subcategory"] is not None:
                    sub_cat = str(updates["subcategory"]).strip()
                    if sub_cat not in SUBCATEGORIES:
                        raise ValueError(f"Invalid subcategory: {sub_cat!r}")
                    items_ws.cell(row=target_row_idx, column=8, value=sub_cat)

                if "clean_name" in updates and updates["clean_name"] is not None:
                    clean_name = str(updates["clean_name"]).strip()
                    if not clean_name:
                        raise ValueError("clean_name cannot be empty")
                    items_ws.cell(row=target_row_idx, column=5, value=clean_name)

                if "raw_name" in updates and updates["raw_name"] is not None:
                    items_ws.cell(row=target_row_idx, column=6, value=str(updates["raw_name"]).strip())

                if "quantity" in updates and updates["quantity"] is not None:
                    qty = round(self._parse_finite_float(updates["quantity"], "quantity"), 3)
                    if qty < 0:
                        raise ValueError("quantity cannot be negative")
                    items_ws.cell(row=target_row_idx, column=9, value=qty)

                if "unit" in updates and updates["unit"] is not None:
                    unit = str(updates["unit"]).strip() or "buc"
                    items_ws.cell(row=target_row_idx, column=10, value=unit)

                if "unit_price" in updates and updates["unit_price"] is not None:
                    unit_price = round(self._parse_finite_float(updates["unit_price"], "unit_price"), 2)
                    items_ws.cell(row=target_row_idx, column=11, value=unit_price)

                if "discount_amount" in updates and updates["discount_amount"] is not None:
                    discount_amount = round(
                        self._parse_finite_float(updates["discount_amount"], "discount_amount"), 2
                    )
                    if discount_amount < 0:
                        raise ValueError("discount_amount cannot be negative")
                    items_ws.cell(row=target_row_idx, column=12, value=discount_amount)

                if "net_total" in updates and updates["net_total"] is not None:
                    net_total = round(self._parse_finite_float(updates["net_total"], "net_total"), 2)
                    items_ws.cell(row=target_row_idx, column=13, value=net_total)

                # Recalculate parent receipt aggregates
                parent_row_idx: int | None = None
                for row_idx in range(2, receipts_ws.max_row + 1):
                    if str(receipts_ws.cell(row=row_idx, column=1).value or "").strip() == receipt_id:
                        parent_row_idx = row_idx
                        break

                if parent_row_idx is not None:
                    items_count = len(matching_item_rows)
                    discounts_sum = round(
                        sum(
                            self._to_float(items_ws.cell(row=r_idx, column=12).value)
                            for r_idx in matching_item_rows
                        ),
                        2,
                    )
                    sgr_sum = round(
                        sum(
                            self._to_float(items_ws.cell(row=r_idx, column=13).value)
                            for r_idx in matching_item_rows
                            if str(items_ws.cell(row=r_idx, column=8).value or "").strip()
                            == "Bottle Deposit (SGR)"
                        ),
                        2,
                    )
                    receipts_ws.cell(row=parent_row_idx, column=7, value=items_count)
                    receipts_ws.cell(row=parent_row_idx, column=8, value=discounts_sum)
                    receipts_ws.cell(row=parent_row_idx, column=9, value=sgr_sum)
                    if recalculate_receipt_total:
                        net_sum = round(
                            sum(
                                self._to_float(items_ws.cell(row=r_idx, column=13).value)
                                for r_idx in matching_item_rows
                            ),
                            2,
                        )
                        receipts_ws.cell(row=parent_row_idx, column=10, value=net_sum)

                self._style_all_data_sheets(receipts_ws, items_ws)
                self._rebuild_dashboard(wb)
                self._save_atomic(wb)

                updated_row = tuple(
                    items_ws.cell(row=target_row_idx, column=col).value
                    for col in range(1, len(ITEMS_HEADERS) + 1)
                )
                return self._row_to_item_dict(updated_row, item_index)
            finally:
                wb.close()

    def delete_receipt(self, receipt_id: str) -> bool:
        """Delete a receipt and all of its line items from the workbook."""
        with self._lock:
            if self.is_locked():
                raise WorkbookLockedError(
                    f"Cannot write to {self.excel_path.name} while it is open in another program."
                )
            if not self.excel_path.exists():
                return False

            wb = self._load_or_create_workbook()
            try:
                receipts_ws = wb["Receipts"]
                items_ws = wb["Items"]

                receipt_rows = [
                    row_idx
                    for row_idx in range(2, receipts_ws.max_row + 1)
                    if str(receipts_ws.cell(row=row_idx, column=1).value or "").strip() == receipt_id
                ]
                if not receipt_rows:
                    return False

                item_rows = [
                    row_idx
                    for row_idx in range(2, items_ws.max_row + 1)
                    if str(items_ws.cell(row=row_idx, column=1).value or "").strip() == receipt_id
                ]

                base_items_count = sum(
                    1
                    for r_idx in item_rows
                    if str(items_ws.cell(row=r_idx, column=6).value or "").strip()
                    != "RECONCILIATION_ADJUSTMENT"
                )
                fingerprints_to_remove: set[str] = set()
                source_names_to_remove: set[str] = set()
                for r_idx in receipt_rows:
                    row_vals = tuple(
                        receipts_ws.cell(row=r_idx, column=col).value
                        for col in range(1, len(RECEIPTS_HEADERS) + 1)
                    )
                    rec = self._row_to_receipt_dict(row_vals)
                    if rec["source_file"]:
                        source_names_to_remove.add(rec["source_file"])
                    fp = "|".join(
                        (
                            rec["date"].strip(),
                            normalize_store_name(rec["store_name"]),
                            f"{rec['total_paid']:.2f}",
                            str(base_items_count),
                        )
                    )
                    fingerprints_to_remove.add(fp)

                for row_idx in reversed(receipt_rows):
                    receipts_ws.delete_rows(row_idx, 1)

                for row_idx in reversed(item_rows):
                    items_ws.delete_rows(row_idx, 1)

                self._style_all_data_sheets(receipts_ws, items_ws)
                self._rebuild_dashboard(wb)
                self._save_atomic(wb)
            finally:
                wb.close()

            self.state_store.remove_receipt(
                receipt_id,
                fingerprints=fingerprints_to_remove,
                source_names=source_names_to_remove,
            )
            return True

    def _style_all_data_sheets(self, receipts_ws: Worksheet, items_ws: Worksheet) -> None:
        self._style_data_sheet(
            receipts_ws,
            RECEIPTS_HEADERS,
            money_cols={8, 9, 10},
            int_cols={7},
            highlight_col=12,
            highlight_value="Needs Review",
        )
        self._style_data_sheet(
            items_ws,
            ITEMS_HEADERS,
            money_cols={11, 12, 13},
            qty_cols={9},
            highlight_col=8,
            highlight_value="Unreconciled Adjustment",
        )

    @staticmethod
    def _norm_date(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")
        if hasattr(value, "isoformat"):
            return str(value.isoformat())[:10]
        return str(value).strip()

    @staticmethod
    def _norm_month(value: Any, date_str: str = "") -> str:
        if value is not None:
            if isinstance(value, datetime):
                return value.strftime("%Y-%m")
            if hasattr(value, "strftime") and not isinstance(value, str):
                return value.strftime("%Y-%m")
            text = str(value).strip()
            if text:
                if re.match(r"^\d{4}-\d{2}", text):
                    return text[:7]
                return text
        if len(date_str) >= 7:
            return date_str[:7]
        return ""

    @staticmethod
    def _norm_time(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "strftime") and not isinstance(value, str):
            return value.strftime("%H:%M")
        return str(value).strip()

    @staticmethod
    def _norm_datetime_str(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value).strip()

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return default
            if "," in cleaned and "." not in cleaned:
                parts = cleaned.split(",")
                if len(parts) == 2 and len(parts[1]) != 3:
                    cleaned = cleaned.replace(",", ".")
                else:
                    cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", "")
            value = cleaned
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if not value:
                return default
        try:
            parsed = float(value)
            return int(parsed) if math.isfinite(parsed) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_finite_float(value: Any, field_name: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric value for {field_name}: {value!r}") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"Non-finite numeric value for {field_name}: {value!r}")
        return parsed

    @staticmethod
    def _receipt_id_num(receipt_id: str) -> int:
        match = re.search(r"(\d+)$", receipt_id)
        return int(match.group(1)) if match else 0

    def _row_to_receipt_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        cells = list(row) + [None] * max(0, len(RECEIPTS_HEADERS) - len(row))
        date_str = self._norm_date(cells[1])
        month_str = self._norm_month(cells[3], date_str)
        return {
            "receipt_id": str(cells[0] or "").strip(),
            "date": date_str,
            "time": self._norm_time(cells[2]),
            "month": month_str,
            "store_name": str(cells[4] or "Unknown Store").strip() or "Unknown Store",
            "payment_method": str(cells[5] or "Unknown").strip() or "Unknown",
            "items_count": self._to_int(cells[6], 0),
            "discounts_saved": round(self._to_float(cells[7], 0.0), 2),
            "sgr_deposit": round(self._to_float(cells[8], 0.0), 2),
            "total_paid": round(self._to_float(cells[9], 0.0), 2),
            "currency": str(cells[10] or "RON").strip() or "RON",
            "status": str(cells[11] or "OK").strip() or "OK",
            "source_file": str(cells[12] or "").strip(),
            "processed_at": self._norm_datetime_str(cells[13]),
        }

    def _row_to_item_dict(self, row: tuple[Any, ...], item_index: int) -> dict[str, Any]:
        cells = list(row) + [None] * max(0, len(ITEMS_HEADERS) - len(row))
        date_str = self._norm_date(cells[1])
        month_str = self._norm_month(cells[2], date_str)
        return {
            "receipt_id": str(cells[0] or "").strip(),
            "item_index": item_index,
            "date": date_str,
            "month": month_str,
            "store_name": str(cells[3] or "Unknown Store").strip() or "Unknown Store",
            "clean_name": str(cells[4] or "").strip(),
            "raw_name": str(cells[5] or "").strip(),
            "main_category": str(cells[6] or "Other").strip() or "Other",
            "subcategory": str(cells[7] or "Home & Misc").strip() or "Home & Misc",
            "quantity": round(self._to_float(cells[8], 1.0), 3),
            "unit": str(cells[9] or "buc").strip() or "buc",
            "unit_price": round(self._to_float(cells[10], 0.0), 2),
            "discount_amount": round(self._to_float(cells[11], 0.0), 2),
            "net_total": round(self._to_float(cells[12], 0.0), 2),
        }

    def _load_or_create_workbook(self) -> Workbook:
        if not self.excel_path.exists():
            wb = Workbook()
            wb.active.title = "Monthly Dashboard"
            receipts_ws = wb.create_sheet("Receipts")
            items_ws = wb.create_sheet("Items")
            self._write_headers(receipts_ws, RECEIPTS_HEADERS)
            self._write_headers(items_ws, ITEMS_HEADERS)
            return wb

        wb = load_workbook(self.excel_path)
        for sheet_name, headers in (("Receipts", RECEIPTS_HEADERS), ("Items", ITEMS_HEADERS)):
            if sheet_name not in wb.sheetnames:
                ws = wb.create_sheet(sheet_name)
                self._write_headers(ws, headers)
        if "Monthly Dashboard" not in wb.sheetnames:
            wb.create_sheet("Monthly Dashboard", index=0)
        return wb

    @staticmethod
    def _write_headers(ws: Worksheet, headers: tuple[str, ...]) -> None:
        ws.append(list(headers))

    @staticmethod
    def _next_receipt_id(receipts_ws: Worksheet) -> str:
        max_num = 0
        for row in receipts_ws.iter_rows(min_row=2, max_col=1, values_only=True):
            val = row[0]
            if isinstance(val, str):
                match = re.search(r"(\d+)$", val.strip())
                if match:
                    max_num = max(max_num, int(match.group(1)))
        return f"RCP-{max_num + 1:04d}"

    def _style_data_sheet(
        self,
        ws: Worksheet,
        headers: tuple[str, ...],
        *,
        money_cols: set[int] | None = None,
        int_cols: set[int] | None = None,
        qty_cols: set[int] | None = None,
        highlight_col: int | None = None,
        highlight_value: str | None = None,
    ) -> None:
        money_cols = money_cols or set()
        int_cols = int_cols or set()
        qty_cols = qty_cols or set()

        num_cols = len(headers)
        for col_idx in range(1, num_cols + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.value = headers[col_idx - 1]
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = _CELL_BORDER
        ws.row_dimensions[1].height = 26
        ws.freeze_panes = "A2"

        max_row = ws.max_row
        if max_row >= 1:
            ws.auto_filter.ref = f"A1:{get_column_letter(num_cols)}{max_row}"

        for row_idx in range(2, max_row + 1):
            is_highlighted = False
            if highlight_col and highlight_value:
                flag_val = ws.cell(row=row_idx, column=highlight_col).value
                is_highlighted = flag_val == highlight_value

            row_fill = (
                _AMBER_FILL
                if is_highlighted
                else (_ZEBRA_FILL if row_idx % 2 == 1 else PatternFill(fill_type=None))
            )

            for col_idx in range(1, num_cols + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.font = _REGULAR_FONT
                cell.border = _CELL_BORDER
                if row_fill.fill_type:
                    cell.fill = row_fill
                else:
                    cell.fill = PatternFill(fill_type=None)

                if col_idx in money_cols:
                    cell.number_format = "#,##0.00"
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif col_idx in int_cols:
                    cell.number_format = "#,##0"
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif col_idx in qty_cols:
                    cell.number_format = "#,##0.###"
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="center")

        self._autofit_columns(ws, max_cols=num_cols)

    def _rebuild_dashboard(self, wb: Workbook) -> None:
        """Rebuild `Monthly Dashboard` from live rows in `Receipts` and `Items`."""
        dash_ws = wb["Monthly Dashboard"]
        # Ensure Monthly Dashboard is always the first tab
        wb.move_sheet(dash_ws, offset=-wb.sheetnames.index("Monthly Dashboard"))
        dash_ws.delete_rows(1, dash_ws.max_row + 1)
        dash_ws.views.sheetView[0].showGridLines = True

        receipts_ws = wb["Receipts"]
        items_ws = wb["Items"]

        receipt_rows = list(receipts_ws.iter_rows(min_row=2, values_only=True))
        item_rows = list(items_ws.iter_rows(min_row=2, values_only=True))

        # Aggregate receipt KPIs and store breakdown
        total_spent = 0.0
        total_discounts = 0.0
        total_sgr = 0.0
        receipt_count = 0
        month_totals_from_receipts: dict[str, float] = defaultdict(float)
        store_stats: dict[str, list[float | int]] = defaultdict(lambda: [0, 0.0])

        for row in receipt_rows:
            if not any(row):
                continue
            rec = self._row_to_receipt_dict(row)
            receipt_count += 1
            month = rec["month"]
            store = rec["store_name"]
            discounts = rec["discounts_saved"]
            sgr = rec["sgr_deposit"]
            paid = rec["total_paid"]

            total_spent += paid
            total_discounts += discounts
            total_sgr += sgr
            if month:
                month_totals_from_receipts[month] += paid
            store_stats[store][0] = int(store_stats[store][0]) + 1
            store_stats[store][1] = float(store_stats[store][1]) + paid

        # Aggregate item spending by (main_category, subcategory) and month
        months_set: set[str] = set(month_totals_from_receipts.keys())
        main_cat_month: dict[tuple[str, str], float] = defaultdict(float)
        sub_cat_month: dict[tuple[str, str, str], float] = defaultdict(float)
        observed_subcats: dict[tuple[str, str], None] = {}

        for idx, row in enumerate(item_rows):
            if not any(row):
                continue
            item = self._row_to_item_dict(row, idx)
            month = item["month"]
            main_cat = item["main_category"]
            sub_cat = item["subcategory"]
            net_total = item["net_total"]
            if month:
                months_set.add(month)
            main_cat_month[(main_cat, month)] += net_total
            sub_cat_month[(main_cat, sub_cat, month)] += net_total
            observed_subcats[(main_cat, sub_cat)] = None

        months = sorted(months_set)
        current_month = datetime.now().strftime("%Y-%m")
        current_month_spent = round(month_totals_from_receipts.get(current_month, 0.0), 2)

        # Title
        dash_ws["A1"] = "Monthly Budget Tracker — Dashboard"
        dash_ws["A1"].font = _TITLE_FONT
        dash_ws["A2"] = f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        dash_ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="57606A")

        # KPI Summary Section
        row_cursor = 4
        dash_ws.cell(row=row_cursor, column=1, value="Key Financial Metrics").font = _SECTION_FONT
        row_cursor += 1
        kpis = [
            ("Total All-Time Spent (RON)", round(total_spent, 2), "#,##0.00"),
            (f"Current Month Spent ({current_month})", current_month_spent, "#,##0.00"),
            ("Total Discounts Saved (RON)", round(total_discounts, 2), "#,##0.00"),
            ("Total SGR Bottle Deposits (RON)", round(total_sgr, 2), "#,##0.00"),
            ("Total Receipts Processed", receipt_count, "#,##0"),
        ]
        for label, value, num_fmt in kpis:
            lbl_cell = dash_ws.cell(row=row_cursor, column=1, value=label)
            lbl_cell.font = _BOLD_FONT
            lbl_cell.fill = _KPI_LABEL_FILL
            lbl_cell.border = _CELL_BORDER
            lbl_cell.alignment = Alignment(horizontal="left", vertical="center")

            val_cell = dash_ws.cell(row=row_cursor, column=2, value=value)
            val_cell.font = _KPI_VALUE_FONT
            val_cell.border = _CELL_BORDER
            val_cell.number_format = num_fmt
            val_cell.alignment = Alignment(horizontal="right", vertical="center")
            row_cursor += 1

        # Main Category x Month Table
        row_cursor += 1
        dash_ws.cell(row=row_cursor, column=1, value="Spending by Main Category & Month (RON)").font = _SECTION_FONT
        row_cursor += 1

        main_headers = ["Main Category", *months, "Total (RON)", "% Share"]
        self._write_dashboard_header_row(dash_ws, row_cursor, main_headers)
        row_cursor += 1

        ordered_mains = list(MAIN_CATEGORIES) + sorted(
            {mc for mc, _ in main_cat_month.keys() if mc not in MAIN_CATEGORIES}
        )
        grand_total_items = sum(main_cat_month.values())

        for main_cat in ordered_mains:
            row_vals = [round(main_cat_month.get((main_cat, m), 0.0), 2) for m in months]
            cat_total = round(sum(row_vals), 2)
            if cat_total == 0.0 and not any(row_vals):
                # Keep core categories visible or only show non-zero + Groceries/Household
                if main_cat not in ("Groceries", "Household", "Personal Care", "Dining & Drinks"):
                    continue
            share = (cat_total / grand_total_items) if grand_total_items > 0 else 0.0
            dash_ws.cell(row=row_cursor, column=1, value=main_cat).font = _BOLD_FONT
            dash_ws.cell(row=row_cursor, column=1).border = _CELL_BORDER
            for idx, val in enumerate(row_vals, start=2):
                c = dash_ws.cell(row=row_cursor, column=idx, value=val)
                c.font = _REGULAR_FONT
                c.number_format = "#,##0.00"
                c.border = _CELL_BORDER
                c.alignment = Alignment(horizontal="right", vertical="center")
            tot_c = dash_ws.cell(row=row_cursor, column=2 + len(months), value=cat_total)
            tot_c.font = _BOLD_FONT
            tot_c.number_format = "#,##0.00"
            tot_c.border = _CELL_BORDER
            tot_c.alignment = Alignment(horizontal="right", vertical="center")

            pct_c = dash_ws.cell(row=row_cursor, column=3 + len(months), value=share)
            pct_c.font = _REGULAR_FONT
            pct_c.number_format = "0.0%"
            pct_c.border = _CELL_BORDER
            pct_c.alignment = Alignment(horizontal="right", vertical="center")
            row_cursor += 1

        # Grand Total row for Main Category table
        dash_ws.cell(row=row_cursor, column=1, value="GRAND TOTAL").font = _BOLD_FONT
        dash_ws.cell(row=row_cursor, column=1).fill = _TOTAL_FILL
        dash_ws.cell(row=row_cursor, column=1).border = _TOTAL_BORDER
        for idx, m in enumerate(months, start=2):
            m_total = round(sum(main_cat_month.get((mc, m), 0.0) for mc in ordered_mains), 2)
            c = dash_ws.cell(row=row_cursor, column=idx, value=m_total)
            c.font = _BOLD_FONT
            c.fill = _TOTAL_FILL
            c.border = _TOTAL_BORDER
            c.number_format = "#,##0.00"
            c.alignment = Alignment(horizontal="right", vertical="center")
        gt_cell = dash_ws.cell(row=row_cursor, column=2 + len(months), value=round(grand_total_items, 2))
        gt_cell.font = _BOLD_FONT
        gt_cell.fill = _TOTAL_FILL
        gt_cell.border = _TOTAL_BORDER
        gt_cell.number_format = "#,##0.00"
        gt_cell.alignment = Alignment(horizontal="right", vertical="center")

        pct_tot_cell = dash_ws.cell(
            row=row_cursor, column=3 + len(months), value=1.0 if grand_total_items > 0 else 0.0
        )
        pct_tot_cell.font = _BOLD_FONT
        pct_tot_cell.fill = _TOTAL_FILL
        pct_tot_cell.border = _TOTAL_BORDER
        pct_tot_cell.number_format = "0.0%"
        pct_tot_cell.alignment = Alignment(horizontal="right", vertical="center")
        row_cursor += 2

        # Detailed Subcategory Breakdown Table
        dash_ws.cell(
            row=row_cursor, column=1, value="Detailed Breakdown by Subcategory & Month (RON)"
        ).font = _SECTION_FONT
        row_cursor += 1

        sub_headers = ["Main Category", "Subcategory", *months, "Total (RON)"]
        self._write_dashboard_header_row(dash_ws, row_cursor, sub_headers)
        row_cursor += 1

        sub_order_index = {name: i for i, name in enumerate(SUBCATEGORIES)}
        main_order_index = {name: i for i, name in enumerate(MAIN_CATEGORIES)}
        sorted_pairs = sorted(
            observed_subcats.keys(),
            key=lambda pair: (
                main_order_index.get(pair[0], 999),
                sub_order_index.get(pair[1], 999),
                pair[0],
                pair[1],
            ),
        )

        for main_cat, sub_cat in sorted_pairs:
            row_vals = [round(sub_cat_month.get((main_cat, sub_cat, m), 0.0), 2) for m in months]
            sub_total = round(sum(row_vals), 2)
            c1 = dash_ws.cell(row=row_cursor, column=1, value=main_cat)
            c2 = dash_ws.cell(row=row_cursor, column=2, value=sub_cat)
            c1.font = _REGULAR_FONT
            c2.font = _REGULAR_FONT
            c1.border = _CELL_BORDER
            c2.border = _CELL_BORDER
            if sub_cat == "Unreconciled Adjustment":
                c1.fill = _AMBER_FILL
                c2.fill = _AMBER_FILL

            for idx, val in enumerate(row_vals, start=3):
                c = dash_ws.cell(row=row_cursor, column=idx, value=val)
                c.font = _REGULAR_FONT
                c.number_format = "#,##0.00"
                c.border = _CELL_BORDER
                c.alignment = Alignment(horizontal="right", vertical="center")
                if sub_cat == "Unreconciled Adjustment":
                    c.fill = _AMBER_FILL

            tot_c = dash_ws.cell(row=row_cursor, column=3 + len(months), value=sub_total)
            tot_c.font = _BOLD_FONT
            tot_c.number_format = "#,##0.00"
            tot_c.border = _CELL_BORDER
            tot_c.alignment = Alignment(horizontal="right", vertical="center")
            if sub_cat == "Unreconciled Adjustment":
                tot_c.fill = _AMBER_FILL
            row_cursor += 1

        if not sorted_pairs:
            empty_cell = dash_ws.cell(
                row=row_cursor, column=1, value="No items logged yet. Drop a receipt in Google Drive /receipts!"
            )
            empty_cell.font = Font(name="Calibri", size=11, italic=True, color="57606A")
            row_cursor += 1

        # Store Breakdown Summary
        row_cursor += 1
        dash_ws.cell(row=row_cursor, column=1, value="Spending by Store").font = _SECTION_FONT
        row_cursor += 1
        self._write_dashboard_header_row(
            dash_ws, row_cursor, ["Store Name", "Receipts", "Total Spent (RON)", "Average Trip (RON)"]
        )
        row_cursor += 1

        for store, (trips, spent) in sorted(
            store_stats.items(), key=lambda kv: float(kv[1][1]), reverse=True
        ):
            trips_int = int(trips)
            spent_val = round(float(spent), 2)
            avg_val = round(spent_val / trips_int, 2) if trips_int > 0 else 0.0
            vals = [store, trips_int, spent_val, avg_val]
            for idx, val in enumerate(vals, start=1):
                c = dash_ws.cell(row=row_cursor, column=idx, value=val)
                c.font = _REGULAR_FONT
                c.border = _CELL_BORDER
                if idx == 1:
                    c.alignment = Alignment(horizontal="left", vertical="center")
                elif idx == 2:
                    c.number_format = "#,##0"
                    c.alignment = Alignment(horizontal="right", vertical="center")
                else:
                    c.number_format = "#,##0.00"
                    c.alignment = Alignment(horizontal="right", vertical="center")
            row_cursor += 1

        self._autofit_columns(dash_ws, max_cols=max(4, len(months) + 3))

    @staticmethod
    def _write_dashboard_header_row(ws: Worksheet, row_idx: int, headers: list[str]) -> None:
        for col_idx, text in enumerate(headers, start=1):
            c = ws.cell(row=row_idx, column=col_idx, value=text)
            c.font = _HEADER_FONT
            c.fill = _HEADER_FILL
            c.border = _CELL_BORDER
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[row_idx].height = 24

    @staticmethod
    def _autofit_columns(ws: Worksheet, *, max_cols: int) -> None:
        for col_idx in range(1, max_cols + 1):
            col_letter = get_column_letter(col_idx)
            max_len = 10
            for row in ws.iter_rows(min_col=col_idx, max_col=col_idx, values_only=True):
                val = row[0]
                if val is not None:
                    text = f"{val:,.2f}" if isinstance(val, float) else str(val)
                    max_len = max(max_len, len(text))
            ws.column_dimensions[col_letter].width = min(max_len + 4, 48)

    def _save_atomic(self, wb: Workbook) -> None:
        self.excel_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.excel_path.with_name(f"{self.excel_path.name}.tmp")
        with tmp_path.open("wb") as fh:
            wb.save(fh)
        os.replace(tmp_path, self.excel_path)
