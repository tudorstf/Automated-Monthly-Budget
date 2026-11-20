"""Tests for `ExcelBudgetWriter`, dashboard aggregation, deduplication, and lock safety."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from src.excel_writer import (
    ExcelBudgetWriter,
    ProcessedStateStore,
    WorkbookLockedError,
)
from src.models import ExtractedLineItem, ExtractedReceipt


def _sample_receipt(
    *,
    store_name: str,
    receipt_date: str,
    total_amount: float,
    items: list[ExtractedLineItem],
    discounts: float = 0.0,
    sgr: float = 0.0,
) -> ExtractedReceipt:
    receipt = ExtractedReceipt(
        is_valid_receipt=True,
        store_name=store_name,
        receipt_date=receipt_date,
        receipt_time="14:30",
        currency="RON",
        payment_method="Card",
        subtotal_discounts=discounts,
        sgr_deposit_total=sgr,
        total_amount=total_amount,
        items=items,
    )
    receipt.reconcile_totals()
    return receipt


def test_multi_month_workbook_and_dashboard_totals(tmp_path: Path) -> None:
    excel_path = tmp_path / "budget_tracker.xlsx"
    state_path = tmp_path / "data" / "processed_state.json"
    writer = ExcelBudgetWriter(excel_path, ProcessedStateStore(state_path))
    writer.ensure_initialized()

    oct_receipt = _sample_receipt(
        store_name="Kaufland",
        receipt_date="2026-10-15",
        total_amount=40.50,
        discounts=3.00,
        sgr=0.50,
        items=[
            ExtractedLineItem(
                raw_name="PIEPT PUI",
                clean_name="Piept de pui dezosat",
                main_category="Groceries",
                subcategory="Meat & Fish",
                quantity=1.0,
                unit="kg",
                unit_price=33.00,
                discount_amount=3.00,
                net_total=30.00,
            ),
            ExtractedLineItem(
                raw_name="DETERGENT",
                clean_name="Detergent vase",
                main_category="Household",
                subcategory="Cleaning & Laundry",
                quantity=1.0,
                unit="buc",
                unit_price=10.00,
                net_total=10.00,
            ),
            ExtractedLineItem(
                raw_name="GARANTIE SGR",
                clean_name="Garanție SGR",
                main_category="Groceries",
                subcategory="Bottle Deposit (SGR)",
                quantity=1.0,
                unit="buc",
                unit_price=0.50,
                net_total=0.50,
                is_sgr_deposit=True,
            ),
        ],
    )

    nov_receipt = _sample_receipt(
        store_name="Lidl",
        receipt_date="2026-11-05",
        total_amount=25.00,
        items=[
            ExtractedLineItem(
                raw_name="BANANE",
                clean_name="Banane",
                main_category="Groceries",
                subcategory="Produce (Fruits & Veg)",
                quantity=2.0,
                unit="kg",
                unit_price=7.50,
                net_total=15.00,
            ),
            # Intentionally short by 10.00 RON to trigger Needs Review amber highlight
        ],
    )

    res1 = writer.append_receipt(oct_receipt, source_filename="oct.jpg", file_hash="hash_oct")
    res2 = writer.append_receipt(nov_receipt, source_filename="nov.jpg", file_hash="hash_nov")

    assert res1.status == "added" and res1.receipt_id == "RCP-0001"
    assert res2.status == "added" and res2.receipt_id == "RCP-0002"

    wb = load_workbook(excel_path)
    assert wb.sheetnames == ["Monthly Dashboard", "Receipts", "Items"]

    receipts_ws = wb["Receipts"]
    assert receipts_ws.max_row == 3
    assert receipts_ws.cell(row=2, column=12).value == "OK"
    assert receipts_ws.cell(row=3, column=12).value == "Needs Review"
    # Needs Review row highlighted in soft amber (#FFF3CD)
    assert receipts_ws.cell(row=3, column=1).fill.start_color.rgb.endswith("FFF3CD")

    items_ws = wb["Items"]
    # 3 items from Oct + 1 item + 1 Unreconciled Adjustment from Nov = 5 item rows (+1 header = 6)
    assert items_ws.max_row == 6
    assert items_ws.auto_filter.ref == "A1:M6"

    dash_ws = wb["Monthly Dashboard"]
    # Verify KPI values written on dashboard
    kpi_values = {
        dash_ws.cell(row=r, column=1).value: dash_ws.cell(row=r, column=2).value
        for r in range(5, 11)
    }
    assert kpi_values["Total All-Time Spent (RON)"] == 65.50
    assert kpi_values["Total Discounts Saved (RON)"] == 3.00
    assert kpi_values["Total SGR Bottle Deposits (RON)"] == 0.50
    assert kpi_values["Total Receipts Processed"] == 2


def test_duplicate_hash_and_fingerprint_rejected(tmp_path: Path) -> None:
    excel_path = tmp_path / "budget_tracker.xlsx"
    state_path = tmp_path / "data" / "processed_state.json"
    writer = ExcelBudgetWriter(excel_path, ProcessedStateStore(state_path))

    receipt = _sample_receipt(
        store_name="Mega Image SRL",
        receipt_date="2026-11-10",
        total_amount=12.00,
        items=[
            ExtractedLineItem(
                raw_name="PAINE",
                clean_name="Pâine albă",
                main_category="Groceries",
                subcategory="Bakery & Bread",
                unit_price=12.00,
                net_total=12.00,
            )
        ],
    )

    first = writer.append_receipt(receipt, source_filename="bon1.jpg", file_hash="hash_1")
    dup_hash = writer.append_receipt(receipt, source_filename="bon1_copy.jpg", file_hash="hash_1")

    # Different photo/hash of the exact same receipt (Mega Image vs Mega Image SRL)
    rephotographed = _sample_receipt(
        store_name="Mega Image",
        receipt_date="2026-11-10",
        total_amount=12.00,
        items=[
            ExtractedLineItem(
                raw_name="PAINE",
                clean_name="Pâine albă",
                main_category="Groceries",
                subcategory="Bakery & Bread",
                unit_price=12.00,
                net_total=12.00,
            )
        ],
    )
    dup_fp = writer.append_receipt(
        rephotographed, source_filename="bon1_rephoto.jpg", file_hash="hash_2"
    )

    assert first.status == "added"
    assert dup_hash.status == "duplicate"
    assert dup_fp.status == "duplicate"

    wb = load_workbook(excel_path)
    assert wb["Receipts"].max_row == 2  # Header + 1 receipt only


def test_libreoffice_lock_prevents_write(tmp_path: Path) -> None:
    excel_path = tmp_path / "budget_tracker.xlsx"
    state_path = tmp_path / "data" / "processed_state.json"
    writer = ExcelBudgetWriter(excel_path, ProcessedStateStore(state_path))
    writer.ensure_initialized()

    lock_file = tmp_path / ".~lock.budget_tracker.xlsx#"
    lock_file.write_text("tudor,host,2026-11-17", encoding="utf-8")

    receipt = _sample_receipt(
        store_name="Profi",
        receipt_date="2026-11-15",
        total_amount=5.00,
        items=[
            ExtractedLineItem(
                raw_name="APA",
                clean_name="Apă plată",
                main_category="Groceries",
                subcategory="Beverages & Coffee",
                unit_price=5.00,
                net_total=5.00,
            )
        ],
    )

    with pytest.raises(WorkbookLockedError):
        writer.append_receipt(receipt, source_filename="profi.jpg", file_hash="hash_profi")

    # State must remain unrecorded while locked
    assert not writer.state_store.has_hash("hash_profi")


def _seed_two_month_writer(tmp_path: Path) -> ExcelBudgetWriter:
    excel_path = tmp_path / "budget_tracker.xlsx"
    state_path = tmp_path / "data" / "processed_state.json"
    writer = ExcelBudgetWriter(excel_path, ProcessedStateStore(state_path))
    writer.ensure_initialized()

    oct_receipt = _sample_receipt(
        store_name="Kaufland",
        receipt_date="2026-10-15",
        total_amount=40.50,
        discounts=3.00,
        sgr=0.50,
        items=[
            ExtractedLineItem(
                raw_name="PIEPT PUI",
                clean_name="Piept de pui dezosat",
                main_category="Groceries",
                subcategory="Meat & Fish",
                quantity=1.0,
                unit="kg",
                unit_price=33.00,
                discount_amount=3.00,
                net_total=30.00,
            ),
            ExtractedLineItem(
                raw_name="DETERGENT",
                clean_name="Detergent vase",
                main_category="Household",
                subcategory="Cleaning & Laundry",
                quantity=1.0,
                unit="buc",
                unit_price=10.00,
                net_total=10.00,
            ),
            ExtractedLineItem(
                raw_name="GARANTIE SGR",
                clean_name="Garanție SGR",
                main_category="Groceries",
                subcategory="Bottle Deposit (SGR)",
                quantity=1.0,
                unit="buc",
                unit_price=0.50,
                net_total=0.50,
                is_sgr_deposit=True,
            ),
        ],
    )
    nov_receipt = _sample_receipt(
        store_name="Lidl",
        receipt_date="2026-11-05",
        total_amount=25.00,
        items=[
            ExtractedLineItem(
                raw_name="BANANE",
                clean_name="Banane",
                main_category="Groceries",
                subcategory="Produce (Fruits & Veg)",
                quantity=2.0,
                unit="kg",
                unit_price=7.50,
                net_total=15.00,
            ),
        ],
    )
    writer.append_receipt(oct_receipt, source_filename="oct.jpg", file_hash="hash_oct")
    writer.append_receipt(nov_receipt, source_filename="nov.jpg", file_hash="hash_nov")
    return writer


def test_get_dashboard_payload_aggregations_and_month_filter(tmp_path: Path) -> None:
    empty_writer = ExcelBudgetWriter(
        tmp_path / "empty.xlsx",
        ProcessedStateStore(tmp_path / "empty_state.json"),
    )
    empty_payload = empty_writer.get_dashboard_payload()
    assert empty_payload["months"] == []
    assert empty_payload["receipts"] == []
    assert empty_payload["items"] == []
    assert empty_payload["kpis"]["total_spent"] == 0.0

    writer = _seed_two_month_writer(tmp_path)

    all_payload = writer.get_dashboard_payload()
    assert all_payload["months"] == ["2026-10", "2026-11"]
    assert all_payload["by_month"] == [
        {"month": "2026-10", "total_spent": 40.50, "discounts": 3.00, "receipt_count": 1},
        {"month": "2026-11", "total_spent": 25.00, "discounts": 0.00, "receipt_count": 1},
    ]

    kpis = all_payload["kpis"]
    assert kpis["total_spent"] == 65.50
    assert kpis["total_discounts"] == 3.00
    assert kpis["total_sgr"] == 0.50
    assert kpis["receipt_count"] == 2
    assert kpis["needs_review_count"] == 1
    assert kpis["items_count"] == 5

    # Main category shares are 0.0-1.0 ratios summing to ~1.0
    by_main = all_payload["by_main_category"]
    assert [entry["main_category"] for entry in by_main] == ["Groceries", "Household", "Other"]
    for entry in by_main:
        assert 0.0 <= entry["share"] <= 1.0
    assert sum(entry["share"] for entry in by_main) == pytest.approx(1.0, abs=1e-3)
    assert by_main[0]["total"] == 45.50
    assert by_main[0]["by_month"] == {"2026-10": 30.50, "2026-11": 15.00}

    by_sub = all_payload["by_subcategory"]
    assert len(by_sub) == 5
    assert by_sub[0]["subcategory"] == "Meat & Fish"
    assert by_sub[0]["total"] == 30.00

    by_store = all_payload["by_store"]
    assert by_store == [
        {"store_name": "Kaufland", "receipt_count": 1, "total_spent": 40.50, "average_trip": 40.50},
        {"store_name": "Lidl", "receipt_count": 1, "total_spent": 25.00, "average_trip": 25.00},
    ]

    # Receipts and items sorted newest first, preserving 0-based item_index per receipt
    assert [r["receipt_id"] for r in all_payload["receipts"]] == ["RCP-0002", "RCP-0001"]
    rcp1_items = [it for it in all_payload["items"] if it["receipt_id"] == "RCP-0001"]
    rcp2_items = [it for it in all_payload["items"] if it["receipt_id"] == "RCP-0002"]
    assert [it["item_index"] for it in rcp1_items] == [0, 1, 2]
    assert [it["item_index"] for it in rcp2_items] == [0, 1]

    # Filtered by month keeps full months/by_month list while filtering KPIs and tables
    oct_payload = writer.get_dashboard_payload("2026-10")
    assert oct_payload["months"] == ["2026-10", "2026-11"]
    assert len(oct_payload["by_month"]) == 2
    assert oct_payload["kpis"]["current_month"] == "2026-10"
    assert oct_payload["kpis"]["current_month_spent"] == 40.50
    assert oct_payload["kpis"]["total_spent"] == 40.50
    assert oct_payload["kpis"]["receipt_count"] == 1
    assert oct_payload["kpis"]["needs_review_count"] == 0
    assert oct_payload["kpis"]["items_count"] == 3
    assert [r["receipt_id"] for r in oct_payload["receipts"]] == ["RCP-0001"]
    assert all(it["month"] == "2026-10" for it in oct_payload["items"])


def test_update_receipt_meta_propagates_to_items_and_dashboard(tmp_path: Path) -> None:
    writer = _seed_two_month_writer(tmp_path)

    updated = writer.update_receipt_meta(
        "RCP-0002",
        {
            "store_name": "Lidl Plus Market",
            "date": "2026-12-03",
            "time": "19:15",
            "payment_method": "Cash",
            "status": "OK",
        },
    )

    assert updated["store_name"] == "Lidl Plus Market"
    assert updated["date"] == "2026-12-03"
    assert updated["month"] == "2026-12"
    assert updated["time"] == "19:15"
    assert updated["payment_method"] == "Cash"
    assert updated["status"] == "OK"

    payload = writer.get_dashboard_payload()
    assert payload["months"] == ["2026-10", "2026-12"]
    assert payload["kpis"]["needs_review_count"] == 0

    rcp2_items = [it for it in payload["items"] if it["receipt_id"] == "RCP-0002"]
    assert len(rcp2_items) == 2
    assert all(it["store_name"] == "Lidl Plus Market" for it in rcp2_items)
    assert all(it["date"] == "2026-12-03" for it in rcp2_items)
    assert all(it["month"] == "2026-12" for it in rcp2_items)

    # Unrelated receipt items stay unchanged
    rcp1_items = [it for it in payload["items"] if it["receipt_id"] == "RCP-0001"]
    assert all(it["store_name"] == "Kaufland" and it["month"] == "2026-10" for it in rcp1_items)

    # Monthly Dashboard sheet rebuilt with updated store & month
    wb = load_workbook(writer.excel_path)
    dash_text = [
        wb["Monthly Dashboard"].cell(row=r, column=1).value
        for r in range(1, wb["Monthly Dashboard"].max_row + 1)
    ]
    assert "Lidl Plus Market" in dash_text
    assert "Lidl" not in dash_text

    with pytest.raises(KeyError):
        writer.update_receipt_meta("RCP-9999", {"store_name": "Other"})
    with pytest.raises(ValueError):
        writer.update_receipt_meta("RCP-0001", {"date": "bad-date"})
    with pytest.raises(ValueError):
        writer.update_receipt_meta("RCP-0001", {"status": "Invalid"})


def test_update_item_recalculates_parent_receipt_and_validates_categories(tmp_path: Path) -> None:
    writer = _seed_two_month_writer(tmp_path)

    updated_item = writer.update_item(
        "RCP-0001",
        1,
        {
            "clean_name": "Detergent lichid Ariel",
            "main_category": "Groceries",
            "subcategory": "Bottle Deposit (SGR)",
            "quantity": 2.0,
            "unit_price": 6.00,
            "discount_amount": 1.00,
            "net_total": 11.00,
        },
    )

    assert updated_item["clean_name"] == "Detergent lichid Ariel"
    assert updated_item["main_category"] == "Groceries"
    assert updated_item["subcategory"] == "Bottle Deposit (SGR)"
    assert updated_item["quantity"] == 2.0
    assert updated_item["unit_price"] == 6.00
    assert updated_item["discount_amount"] == 1.00
    assert updated_item["net_total"] == 11.00

    wb = load_workbook(writer.excel_path)
    receipts_ws = wb["Receipts"]
    # RCP-0001 is row 2: Items Count=3, Discounts Saved=3.00+1.00=4.00, SGR=0.50+11.00=11.50, Total Paid=30.00+11.00+0.50=41.50
    assert receipts_ws.cell(row=2, column=7).value == 3
    assert receipts_ws.cell(row=2, column=8).value == 4.00
    assert receipts_ws.cell(row=2, column=9).value == 11.50
    assert receipts_ws.cell(row=2, column=10).value == 41.50

    # Test recalculate_receipt_total=False preserves manual total_paid
    writer.update_item(
        "RCP-0001",
        0,
        {"net_total": 25.00},
        recalculate_receipt_total=False,
    )
    wb_after = load_workbook(writer.excel_path)
    assert wb_after["Receipts"].cell(row=2, column=10).value == 41.50

    with pytest.raises(ValueError, match="main_category"):
        writer.update_item("RCP-0001", 0, {"main_category": "NotACategory"})
    with pytest.raises(ValueError, match="subcategory"):
        writer.update_item("RCP-0001", 0, {"subcategory": "NotASubcategory"})
    with pytest.raises(KeyError):
        writer.update_item("RCP-0001", 99, {"clean_name": "Missing"})
    with pytest.raises(KeyError):
        writer.update_item("RCP-9999", 0, {"clean_name": "Missing"})


def test_delete_receipt_removes_rows_rebuilds_dashboard_and_handles_missing(tmp_path: Path) -> None:
    writer = _seed_two_month_writer(tmp_path)

    assert writer.delete_receipt("RCP-9999") is False
    assert writer.delete_receipt("RCP-0001") is True
    assert writer.delete_receipt("RCP-0001") is False

    payload = writer.get_dashboard_payload()
    assert [r["receipt_id"] for r in payload["receipts"]] == ["RCP-0002"]
    assert all(it["receipt_id"] == "RCP-0002" for it in payload["items"])
    assert payload["kpis"]["total_spent"] == 25.00
    assert payload["kpis"]["total_discounts"] == 0.00
    assert payload["kpis"]["total_sgr"] == 0.00

    wb = load_workbook(writer.excel_path)
    assert wb["Receipts"].max_row == 2
    assert wb["Items"].max_row == 3
    kpi_values = {
        wb["Monthly Dashboard"].cell(row=r, column=1).value: wb["Monthly Dashboard"].cell(row=r, column=2).value
        for r in range(5, 11)
    }
    assert kpi_values["Total All-Time Spent (RON)"] == 25.00
    assert kpi_values["Total Receipts Processed"] == 1


def test_config_mask_secret_and_update_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import mask_secret, update_env_file

    for key in (
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "DRIVE_FOLDER_NAME",
        "POLL_INTERVAL_SECONDS",
        "APP_HOST",
        "APP_PORT",
        "AUTO_START_WATCHER",
        "EXCEL_FILE_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    assert mask_secret("") == ""
    assert mask_secret("   ") == ""
    assert mask_secret("your_gemini_api_key_here") == ""
    assert mask_secret("short") == "***"
    assert mask_secret("1234567890") == "***"
    assert mask_secret("AIzaSyDummyKey9876") == "AIzaSy...9876"

    (tmp_path / ".env.example").write_text(
        "# Example\nGEMINI_API_KEY=your_gemini_api_key_here\nDRIVE_FOLDER_NAME=receipts\nPOLL_INTERVAL_SECONDS=20\n",
        encoding="utf-8",
    )

    settings = update_env_file(
        tmp_path,
        {
            "GEMINI_API_KEY": "AIzaSyTestKey1234\r\n",
            "DRIVE_FOLDER_NAME": "bonuri_2026",
            "POLL_INTERVAL_SECONDS": "45",
            "APP_PORT": "9001",
            "AUTO_START_WATCHER": "false",
        },
    )

    assert settings.gemini_api_key == "AIzaSyTestKey1234"
    assert settings.drive_folder_name == "bonuri_2026"
    assert settings.poll_interval_seconds == 45
    assert settings.app_port == 9001
    assert settings.auto_start_watcher is False
    assert not (tmp_path / ".env.tmp").exists()

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert env_text.count("GEMINI_API_KEY=") == 1
    assert "GEMINI_API_KEY=AIzaSyTestKey1234\n" in env_text
    assert "DRIVE_FOLDER_NAME=bonuri_2026\n" in env_text
    assert "AUTO_START_WATCHER=false\n" in env_text

    # Subsequent update replaces existing lines without duplicating keys
    settings2 = update_env_file(tmp_path, {"DRIVE_FOLDER_NAME": "receipts_eu"})
    assert settings2.drive_folder_name == "receipts_eu"
    assert (tmp_path / ".env").read_text(encoding="utf-8").count("DRIVE_FOLDER_NAME=") == 1


def test_delete_receipt_purges_state_store_and_allows_reupload(tmp_path: Path) -> None:
    from src.excel_writer import compute_receipt_fingerprint

    excel_path = tmp_path / "budget_tracker.xlsx"
    state_path = tmp_path / "data" / "processed_state.json"
    writer = ExcelBudgetWriter(excel_path, ProcessedStateStore(state_path))
    writer.ensure_initialized()

    receipt = _sample_receipt(
        store_name="Auchan",
        receipt_date="2026-11-14",
        total_amount=19.50,
        items=[
            ExtractedLineItem(
                raw_name="CAFEA BOABE",
                clean_name="Cafea boabe",
                main_category="Groceries",
                subcategory="Beverages & Coffee",
                unit_price=19.50,
                net_total=19.50,
            )
        ],
    )
    fp = compute_receipt_fingerprint(receipt)

    first = writer.append_receipt(receipt, source_filename="auchan1.jpg", file_hash="hash_auchan_1")
    assert first.status == "added" and first.receipt_id == "RCP-0001"

    # A re-photographed duplicate with a different hash must not clear the original receipt_id link
    dup = writer.append_receipt(receipt, source_filename="auchan_rephoto.jpg", file_hash="hash_auchan_2")
    assert dup.status == "duplicate"
    assert writer.state_store._data["fingerprints"][fp]["receipt_id"] == "RCP-0001"

    assert writer.delete_receipt("RCP-0001") is True

    # Both original and re-photographed hashes + fingerprint are purged from memory & disk
    reloaded_store = ProcessedStateStore(state_path)
    assert not reloaded_store.has_hash("hash_auchan_1")
    assert not reloaded_store.has_hash("hash_auchan_2")
    assert not reloaded_store.has_fingerprint(fp)

    readded = writer.append_receipt(receipt, source_filename="auchan1.jpg", file_hash="hash_auchan_1")
    assert readded.status == "added"
    assert readded.receipt_id == "RCP-0001"


def test_rebuild_dashboard_normalizes_datetime_months_and_manual_cells(tmp_path: Path) -> None:
    from datetime import datetime

    writer = _seed_two_month_writer(tmp_path)

    # Simulate manual edits in Excel/LibreOffice: datetime Month cell, comma decimal, non-numeric text, short row
    wb = load_workbook(writer.excel_path)
    receipts_ws = wb["Receipts"]
    receipts_ws.cell(row=3, column=4, value=datetime(2026, 11, 1, 0, 0))
    receipts_ws.cell(row=3, column=8, value=" 1,50 ")
    receipts_ws.cell(row=3, column=9, value="n/a")
    items_ws = wb["Items"]
    items_ws.cell(row=5, column=3, value=datetime(2026, 11, 1, 0, 0))
    items_ws.cell(row=5, column=13, value="bad-num")
    items_ws.append(["RCP-0002", "2026-11-05", datetime(2026, 11, 1, 0, 0), "Lidl"])  # short row
    wb.save(writer.excel_path)
    wb.close()

    # Rebuilding dashboard via update_receipt_meta and reading payload must not raise ValueError/IndexError
    updated = writer.update_receipt_meta("RCP-0002", {"status": "OK"})
    assert updated["month"] == "2026-11"
    assert updated["discounts_saved"] == 1.50
    assert updated["sgr_deposit"] == 0.0

    payload = writer.get_dashboard_payload()
    assert payload["months"] == ["2026-10", "2026-11"]
    assert payload["kpis"]["total_discounts"] == 4.50
