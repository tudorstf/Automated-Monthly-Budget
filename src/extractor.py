"""Gemini Vision receipt extraction and Romanian receipt normalization."""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image, ImageOps, UnidentifiedImageError
import pillow_heif
from pydantic import ValidationError

from src.config import MAIN_CATEGORIES, SUBCATEGORIES, Settings
from src.models import ExtractedReceipt, ReceiptExtractionSchema

pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are a meticulous financial receipt parser specializing in Romanian fiscal receipts (BON FISCAL) and invoices (Factură), as well as international receipts.

Your task is to inspect the provided receipt image or PDF and extract structured JSON matching the schema.

Rules:
1. Validation:
   - If the document is NOT a receipt/invoice or is completely unreadable, set `is_valid_receipt` to false and explain why in `confidence_notes`.
   - Extract `store_name` cleanly (e.g., "Lidl", "Kaufland", "Carrefour", "Mega Image", "Profi", "Penny", "Auchan", "DM", "Farmacia Tei").
   - Extract `receipt_date` strictly as `YYYY-MM-DD` and `receipt_time` as `HH:MM` (24-hour).
   - Default `currency` to `RON` unless another currency (EUR, USD, etc.) is explicitly printed.

2. Romanian Product Name Cleaning:
   - Put the exact printed receipt line in `raw_name`.
   - Put a clean, expanded, readable Romanian product name in `clean_name`, preserving brand names (e.g. `LAPT ZUZU 1.5% 1L` -> `Lapte Zuzu 1.5% 1L`, `PIEPT PUI DEZ` -> `Piept de pui dezosat`, `PAINE FEL` -> `Pâine feliată`).

3. Discounts & Net Pricing:
   - Romanian receipts (especially Lidl Plus, Kaufland Card, Carrefour, Mega Image) often print a discount line immediately below an item (e.g. `REDUCERE`, `LIDL PLUS`, `DISCOUNT`, `-2.50`).
   - Do NOT create a separate item row for an item-level discount. Instead, fold the discount into the preceding product: set `discount_amount` to the positive discount saved (e.g. `2.50`), keep `unit_price` as the pre-discount unit price, and set `net_total` to the actual net price paid (`gross_line_total - discount_amount`).
   - Set `subtotal_discounts` to the total discounts saved across the receipt.

4. Romanian SGR Bottle Deposits:
   - Lines such as `GARANTIE SGR`, `AMBALAJ SGR`, `GAR. SGR`, `TAXA SGR` (typically 0.50 RON per bottle/can) MUST have `is_sgr_deposit = true`, `main_category = "Groceries"`, and `subcategory = "Bottle Deposit (SGR)"`.
   - Sum all SGR deposit charges into `sgr_deposit_total`.

5. Categorization:
   - Every item MUST use one of these exact `main_category` values: {", ".join(MAIN_CATEGORIES)}.
   - Every item MUST use one of these exact `subcategory` values: {", ".join(SUBCATEGORIES)}.
   - For food/drink bought at a supermarket, use `Groceries` + the matching food subcategory.
   - For prepared meals/coffee at a restaurant, cafe, or delivery (Glovo/Bolt Food/Wolt), use `Dining & Drinks` + `Restaurants & Takeout`.
   - Ignore payment lines (`CARD`, `NUMERAR`, `REST`), VAT summary tables (`TVA A 9%`), and fiscal footer lines (`CUI`, `Z`, `NR BON`).

6. Math Accuracy:
   - Ensure the sum of `net_total` across all extracted `items` equals the printed `total_amount` paid.
"""


class ReceiptExtractionError(RuntimeError):
    """Raised when a receipt file is corrupt, unsupported, or not a valid receipt."""


class GeminiServiceError(RuntimeError):
    """Raised on configuration, auth, rate-limit, or transient Gemini API failures."""


def load_receipt_media(file_path: Path) -> tuple[bytes, str]:
    """Validate and prepare receipt bytes and MIME type for Gemini."""
    if not file_path.exists() or not file_path.is_file():
        raise ReceiptExtractionError(f"Receipt file does not exist: {file_path}")

    raw_bytes = file_path.read_bytes()
    if not raw_bytes:
        raise ReceiptExtractionError(f"Receipt file is empty: {file_path.name}")

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        if not raw_bytes.lstrip().startswith(b"%PDF"):
            raise ReceiptExtractionError(f"Corrupt or invalid PDF file: {file_path.name}")
        return raw_bytes, "application/pdf"

    if suffix in {".heic", ".heif"}:
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                transposed = ImageOps.exif_transpose(img).convert("RGB")
                buf = io.BytesIO()
                transposed.save(buf, format="JPEG", quality=92)
                return buf.getvalue(), "image/jpeg"
        except (UnidentifiedImageError, OSError) as exc:
            raise ReceiptExtractionError(f"Cannot decode HEIC image {file_path.name}: {exc}") from exc

    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    mime_type = mime_map.get(suffix)
    if not mime_type:
        raise ReceiptExtractionError(f"Unsupported receipt extension '{suffix}': {file_path.name}")

    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ReceiptExtractionError(f"Corrupt or unreadable image {file_path.name}: {exc}") from exc

    return raw_bytes, mime_type


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        return code in {408, 429, 500, 502, 503, 504}
    return False


def _should_try_fallback_model(exc: Exception) -> bool:
    if _is_retryable_error(exc):
        return True
    if isinstance(exc, genai_errors.APIError):
        return getattr(exc, "code", None) == 404
    return False


class ReceiptExtractor:
    """Extracts structured receipt data from images and PDFs using Gemini Vision."""

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> None:
        self.settings = settings
        self._client = client
        self.max_retries = max_retries
        self.base_delay = base_delay

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.settings.gemini_api_key or self.settings.gemini_api_key == "your_gemini_api_key_here":
                raise GeminiServiceError(
                    "GEMINI_API_KEY is missing in .env. Please add your Gemini API key first."
                )
            timeout_ms = max(60, int(self.settings.gemini_timeout_seconds)) * 1000
            self._client = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=timeout_ms,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        return self._client

    def extract(self, file_path: Path) -> ExtractedReceipt:
        """Extract and reconcile line items from a receipt image or PDF."""
        media_bytes, mime_type = load_receipt_media(file_path)
        client = self._get_client()

        part = types.Part.from_bytes(data=media_bytes, mime_type=mime_type)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ReceiptExtractionSchema,
            temperature=0.1,
        )

        candidate_models: list[str] = []
        for model_name in (self.settings.gemini_model, *self.settings.gemini_fallback_models):
            if model_name and model_name not in candidate_models:
                candidate_models.append(model_name)

        last_exc: Exception | None = None
        response = None
        for model_idx, model_name in enumerate(candidate_models):
            attempts = self.max_retries if model_idx == 0 else 1
            for attempt in range(1, attempts + 1):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[
                            part,
                            "Extract all receipt details, line items, discounts, and SGR deposits.",
                        ],
                        config=config,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt < attempts and _is_retryable_error(exc):
                        delay = self.base_delay * (2 ** (attempt - 1))
                        logger.warning(
                            "Transient Gemini error (%s) on %s (attempt %d/%d): %s. Retrying in %.1fs...",
                            model_name,
                            file_path.name,
                            attempt,
                            attempts,
                            exc,
                            delay,
                        )
                        time.sleep(delay)
                        continue
                    has_more_models = model_idx < len(candidate_models) - 1
                    if has_more_models and _should_try_fallback_model(exc):
                        logger.warning(
                            "Model %s unavailable for %s (%s); trying fallback model %s...",
                            model_name,
                            file_path.name,
                            exc,
                            candidate_models[model_idx + 1],
                        )
                        break
                    raise GeminiServiceError(
                        f"Gemini API error for {file_path.name}: {exc}"
                    ) from exc
            if response is not None:
                break

        if response is None:
            raise GeminiServiceError(
                f"Gemini API failed for {file_path.name}: {last_exc}"
            )

        try:
            parsed_obj = getattr(response, "parsed", None)
            if parsed_obj is not None:
                raw_dict = (
                    parsed_obj.model_dump()
                    if hasattr(parsed_obj, "model_dump")
                    else dict(parsed_obj)
                )
                receipt = ExtractedReceipt.model_validate(raw_dict)
            else:
                raw_text = getattr(response, "text", None) or ""
                if not raw_text.strip():
                    raise ReceiptExtractionError(
                        f"Empty response from Gemini for {file_path.name}."
                    )
                receipt = ExtractedReceipt.model_validate_json(raw_text)
        except ValidationError as exc:
            raise ReceiptExtractionError(
                f"Malformed structured output from Gemini for {file_path.name}: {exc}"
            ) from exc

        if not receipt.is_valid_receipt:
            reason = receipt.confidence_notes or "Document was not recognized as a valid receipt."
            raise ReceiptExtractionError(f"Invalid receipt ({file_path.name}): {reason}")

        receipt.reconcile_totals()
        return receipt
