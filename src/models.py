"""Pydantic models for Gemini structured receipt extraction and math reconciliation."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src.config import (
    MAIN_CATEGORIES,
    RECONCILIATION_TOLERANCE_RON,
    SUBCATEGORIES,
)

MainCategoryType = Literal[
    "Groceries",
    "Household",
    "Personal Care",
    "Dining & Drinks",
    "Health & Pharmacy",
    "Pets",
    "Electronics & Home",
    "Other",
]

SubcategoryType = Literal[
    "Dairy & Eggs",
    "Meat & Fish",
    "Produce (Fruits & Veg)",
    "Bakery & Bread",
    "Pantry & Staples",
    "Snacks & Sweets",
    "Beverages & Coffee",
    "Alcohol & Tobacco",
    "Bottle Deposit (SGR)",
    "Cleaning & Laundry",
    "Kitchen & Paper Goods",
    "Toiletries & Hygiene",
    "Medicine & Pharmacy",
    "Restaurants & Takeout",
    "Pet Food & Supplies",
    "Home & Misc",
    "Unreconciled Adjustment",
]

_MAIN_CATEGORY_LOOKUP = {c.lower(): c for c in MAIN_CATEGORIES}
_SUBCATEGORY_LOOKUP = {c.lower(): c for c in SUBCATEGORIES}


class ExtractedLineItem(BaseModel):
    """Single purchased line item extracted from a receipt."""

    raw_name: str = Field(description="Exact abbreviated item text printed on the receipt.")
    clean_name: str = Field(
        description="Expanded, human-readable product name in Romanian preserving brand names."
    )
    main_category: MainCategoryType = Field(description="Top-level budget category.")
    subcategory: SubcategoryType = Field(description="Detailed item subcategory.")
    quantity: float = Field(default=1.0, description="Quantity purchased (e.g., 1, 2, 0.542).")
    unit: str = Field(default="buc", description="Unit of measurement (buc, kg, l, g, ml, set).")
    unit_price: float = Field(description="Unit price before item discount.")
    discount_amount: float = Field(
        default=0.0,
        description="Positive discount amount deducted from this item (0.0 if none).",
    )
    net_total: float = Field(description="Final net amount paid for this line item after discount.")
    is_sgr_deposit: bool = Field(
        default=False,
        description="True if this line is a Romanian SGR bottle deposit (Garanție SGR / Ambalaj SGR).",
    )

    @field_validator("main_category", mode="before")
    @classmethod
    def _normalize_main_category(cls, value: object) -> str:
        if isinstance(value, str):
            matched = _MAIN_CATEGORY_LOOKUP.get(value.strip().lower())
            if matched:
                return matched
        return "Other"

    @field_validator("subcategory", mode="before")
    @classmethod
    def _normalize_subcategory(cls, value: object) -> str:
        if isinstance(value, str):
            matched = _SUBCATEGORY_LOOKUP.get(value.strip().lower())
            if matched:
                return matched
        return "Home & Misc"

    @field_validator("unit", mode="before")
    @classmethod
    def _normalize_unit(cls, value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return "buc"

    @field_validator("quantity", mode="before")
    @classmethod
    def _normalize_quantity(cls, value: object) -> float:
        try:
            qty = float(value)  # type: ignore[arg-type]
            return qty if qty > 0 else 1.0
        except (TypeError, ValueError):
            return 1.0

    @field_validator("discount_amount", mode="before")
    @classmethod
    def _normalize_discount(cls, value: object) -> float:
        try:
            return round(abs(float(value)), 2)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    @field_validator("unit_price", "net_total", mode="before")
    @classmethod
    def _round_money(cls, value: object) -> float:
        try:
            return round(float(value), 2)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0


class ReceiptExtractionSchema(BaseModel):
    """Structured schema passed to Gemini Vision for receipt extraction."""

    is_valid_receipt: bool = Field(
        description="True if the image/PDF is a readable receipt or invoice, False otherwise."
    )
    store_name: str = Field(default="Unknown Store", description="Clean store or merchant name.")
    receipt_date: str = Field(default="", description="Purchase date in YYYY-MM-DD format.")
    receipt_time: str = Field(default="", description="Purchase time in HH:MM 24h format, if visible.")
    currency: str = Field(default="RON", description="3-letter ISO currency code (usually RON).")
    payment_method: str = Field(
        default="Unknown", description="Payment method: Card, Cash, Voucher, or Unknown."
    )
    subtotal_discounts: float = Field(
        default=0.0, description="Total discounts/loyalty savings printed on the receipt."
    )
    sgr_deposit_total: float = Field(
        default=0.0, description="Total SGR bottle deposit fees on the receipt."
    )
    total_amount: float = Field(default=0.0, description="Final total paid as printed on the receipt.")
    items: list[ExtractedLineItem] = Field(
        default_factory=list, description="All extracted line items."
    )
    confidence_notes: str = Field(
        default="", description="Notes about illegible areas, crumpled text, or ambiguities."
    )

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip().upper()[:5]
        return "RON"

    @field_validator("store_name", mode="before")
    @classmethod
    def _normalize_store(cls, value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "Unknown Store"

    @field_validator("payment_method", mode="before")
    @classmethod
    def _normalize_payment(cls, value: object) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "Unknown"

    @field_validator("subtotal_discounts", "sgr_deposit_total", "total_amount", mode="before")
    @classmethod
    def _round_totals(cls, value: object) -> float:
        try:
            return round(float(value), 2)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0


class ExtractedReceipt(ReceiptExtractionSchema):
    """Full receipt model including reconciliation status and validation helpers."""

    status: Literal["OK", "Needs Review"] = "OK"

    @property
    def month_key(self) -> str:
        """Return YYYY-MM month key derived from `receipt_date`."""
        if len(self.receipt_date) >= 7:
            return self.receipt_date[:7]
        return date.today().strftime("%Y-%m")

    def reconcile_totals(self, tolerance: float = RECONCILIATION_TOLERANCE_RON) -> Literal["OK", "Needs Review"]:
        """Normalize line items and verify their sum matches `total_amount`.

        If the difference exceeds `tolerance`, adds an explicit
        `Unreconciled Adjustment` balancing item so Excel sums match the receipt
        total, and marks `status = "Needs Review"`.
        """
        needs_review = False

        # Validate or normalize receipt date
        date_valid = False
        if self.receipt_date:
            try:
                parsed_date = datetime.strptime(self.receipt_date.strip(), "%Y-%m-%d").date()
                self.receipt_date = parsed_date.isoformat()
                date_valid = True
            except ValueError:
                date_valid = False
        if not date_valid:
            self.receipt_date = date.today().isoformat()
            needs_review = True
            note = "Missing or unparseable receipt date; defaulted to today."
            self.confidence_notes = f"{self.confidence_notes} {note}".strip()

        # Keep method idempotent by removing any prior balancing line
        self.items = [
            item for item in self.items if item.raw_name != "RECONCILIATION_ADJUSTMENT"
        ]

        # Normalize SGR deposits
        for item in self.items:
            if item.is_sgr_deposit or item.subcategory == "Bottle Deposit (SGR)":
                item.is_sgr_deposit = True
                item.main_category = "Groceries"
                item.subcategory = "Bottle Deposit (SGR)"

        sgr_sum = round(sum(item.net_total for item in self.items if item.is_sgr_deposit), 2)
        if sgr_sum != 0.0 and self.sgr_deposit_total == 0.0:
            self.sgr_deposit_total = sgr_sum

        item_discounts = round(sum(item.discount_amount for item in self.items), 2)
        if item_discounts > self.subtotal_discounts:
            self.subtotal_discounts = item_discounts

        items_sum = round(sum(item.net_total for item in self.items), 2)
        diff = round(self.total_amount - items_sum, 2)

        if abs(diff) > tolerance:
            self.items.append(
                ExtractedLineItem(
                    raw_name="RECONCILIATION_ADJUSTMENT",
                    clean_name="Diferență neidentificată (Reconciliere bon)",
                    main_category="Other",
                    subcategory="Unreconciled Adjustment",
                    quantity=1.0,
                    unit="buc",
                    unit_price=diff,
                    discount_amount=0.0,
                    net_total=diff,
                    is_sgr_deposit=False,
                )
            )
            needs_review = True

        self.status = "Needs Review" if needs_review else "OK"
        return self.status
