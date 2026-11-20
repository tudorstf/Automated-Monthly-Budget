"""Tests for Pydantic receipt models, SGR normalization, and math reconciliation."""

from __future__ import annotations

from datetime import date

from src.models import ExtractedLineItem, ExtractedReceipt


def _make_item(**overrides: object) -> ExtractedLineItem:
    defaults: dict[str, object] = {
        "raw_name": "LAPT ZUZU 1.5% 1L",
        "clean_name": "Lapte Zuzu 1.5% 1L",
        "main_category": "Groceries",
        "subcategory": "Dairy & Eggs",
        "quantity": 1.0,
        "unit": "buc",
        "unit_price": 8.50,
        "discount_amount": 0.0,
        "net_total": 8.50,
        "is_sgr_deposit": False,
    }
    defaults.update(overrides)
    return ExtractedLineItem(**defaults)


def test_reconcile_exact_match_with_discount_and_sgr() -> None:
    receipt = ExtractedReceipt(
        is_valid_receipt=True,
        store_name="Lidl",
        receipt_date="2026-11-10",
        receipt_time="18:42",
        currency="RON",
        payment_method="Card",
        subtotal_discounts=0.0,
        sgr_deposit_total=0.0,
        total_amount=22.50,
        items=[
            _make_item(
                raw_name="IAURT ZUZU STRAIN",
                clean_name="Iaurt grecesc Zuzu",
                unit_price=12.00,
                discount_amount=2.00,
                net_total=10.00,
            ),
            _make_item(
                raw_name="APA MINERALA BORSEC 2L",
                clean_name="Apă minerală Borsec 2L",
                subcategory="Beverages & Coffee",
                unit_price=6.00,
                discount_amount=0.0,
                net_total=12.00,
            ),
            _make_item(
                raw_name="GARANTIE SGR",
                clean_name="Garanție SGR ambalaj",
                main_category="Other",  # Should normalize to Groceries / Bottle Deposit (SGR)
                subcategory="Bottle Deposit (SGR)",
                unit_price=0.50,
                net_total=0.50,
                is_sgr_deposit=True,
            ),
        ],
    )

    status = receipt.reconcile_totals()

    assert status == "OK"
    assert receipt.status == "OK"
    assert len(receipt.items) == 3
    assert receipt.subtotal_discounts == 2.00
    assert receipt.sgr_deposit_total == 0.50
    assert receipt.items[2].main_category == "Groceries"
    assert receipt.items[2].subcategory == "Bottle Deposit (SGR)"
    assert receipt.items[2].is_sgr_deposit is True


def test_reconcile_within_tolerance_stays_ok() -> None:
    receipt = ExtractedReceipt(
        is_valid_receipt=True,
        store_name="Kaufland",
        receipt_date="2026-11-11",
        total_amount=10.03,
        items=[_make_item(unit_price=10.00, net_total=10.00)],
    )

    status = receipt.reconcile_totals()

    assert status == "OK"
    assert len(receipt.items) == 1


def test_reconcile_adds_adjustment_row_and_is_idempotent() -> None:
    receipt = ExtractedReceipt(
        is_valid_receipt=True,
        store_name="Carrefour",
        receipt_date="2026-11-12",
        total_amount=25.00,
        items=[_make_item(unit_price=20.00, net_total=20.00)],
    )

    status_first = receipt.reconcile_totals()
    status_second = receipt.reconcile_totals()

    assert status_first == "Needs Review"
    assert status_second == "Needs Review"
    assert len(receipt.items) == 2

    adjustment = receipt.items[-1]
    assert adjustment.raw_name == "RECONCILIATION_ADJUSTMENT"
    assert adjustment.main_category == "Other"
    assert adjustment.subcategory == "Unreconciled Adjustment"
    assert adjustment.net_total == 5.00
    assert round(sum(i.net_total for i in receipt.items), 2) == 25.00


def test_unknown_categories_and_missing_date_fallback() -> None:
    item = ExtractedLineItem(
        raw_name="PRODUS X",
        clean_name="Produs X",
        main_category="InvalidMain",  # type: ignore[arg-type]
        subcategory="InvalidSub",  # type: ignore[arg-type]
        unit_price=5.0,
        net_total=5.0,
    )
    assert item.main_category == "Other"
    assert item.subcategory == "Home & Misc"

    receipt = ExtractedReceipt(
        is_valid_receipt=True,
        store_name="Profi",
        receipt_date="not-a-date",
        total_amount=5.0,
        items=[item],
    )
    status = receipt.reconcile_totals()
    assert status == "Needs Review"
    assert receipt.receipt_date == date.today().isoformat()
