"""End-to-end pipeline and media extraction tests using mocked Drive and Gemini clients."""

from __future__ import annotations

from dataclasses import replace
import io
from pathlib import Path
from types import SimpleNamespace

from openpyxl import load_workbook
from PIL import Image
import pytest

from src.config import Settings
from src.drive_client import DriveFile, GoogleDriveClient
from src.excel_writer import ExcelBudgetWriter
from src.extractor import GeminiServiceError, ReceiptExtractionError, ReceiptExtractor, load_receipt_media
from src.models import ExtractedLineItem, ExtractedReceipt, ReceiptExtractionSchema
from src.notifier import DesktopNotifier
from src.pipeline import ReceiptPipeline


def _make_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        base_dir=tmp_path,
        gemini_api_key="test-key",
        gemini_model="gemini-3.8-flash",
        gemini_fallback_models=(),
        drive_folder_name="receipts",
        poll_interval_seconds=20,
        excel_path=tmp_path / "budget_tracker.xlsx",
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "token.json",
        state_path=tmp_path / "data" / "processed_state.json",
        failed_dir=tmp_path / "failed_receipts",
        logs_dir=tmp_path / "logs",
    )
    settings.ensure_directories()
    return settings


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 80), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _write_temp_png(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


class FakeDriveClient:
    """In-memory Drive stub recording downloads and permanent deletions."""

    def __init__(self, files: dict[str, tuple[DriveFile, bytes]]) -> None:
        self.files = dict(files)
        self.deleted_ids: list[str] = []

    def list_pending_files(self) -> list[DriveFile]:
        return [meta for meta, _ in self.files.values()]

    def download_file(self, file_id: str, dest_path: Path) -> Path:
        _, payload = self.files[file_id]
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(payload)
        return dest_path

    def delete_file_permanently(self, file_id: str) -> None:
        self.deleted_ids.append(file_id)
        self.files.pop(file_id, None)


class FakeGeminiModels:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0
        self.models_called: list[str] = []

    def generate_content(self, **kwargs: object) -> object:
        self.calls += 1
        self.models_called.append(str(kwargs.get("model", "")))
        next_item = self._outcomes.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return SimpleNamespace(parsed=next_item, text="")


def test_end_to_end_drive_success_and_permanent_delete(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    png_payload = _png_bytes()
    drive_file = DriveFile(
        id="drv-1",
        name="lidl_bon.png",
        mime_type="image/png",
        size=len(png_payload),
        created_time="2026-11-17T10:00:00Z",
    )
    drive = FakeDriveClient({"drv-1": (drive_file, png_payload)})

    parsed_schema = ReceiptExtractionSchema(
        is_valid_receipt=True,
        store_name="Lidl",
        receipt_date="2026-11-17",
        receipt_time="10:05",
        currency="RON",
        payment_method="Card",
        subtotal_discounts=1.50,
        sgr_deposit_total=0.50,
        total_amount=14.00,
        items=[
            ExtractedLineItem(
                raw_name="LAPT PROSPAT",
                clean_name="Lapte proaspăt Pilos 1.5%",
                main_category="Groceries",
                subcategory="Dairy & Eggs",
                quantity=1.0,
                unit="buc",
                unit_price=7.00,
                discount_amount=1.50,
                net_total=5.50,
            ),
            ExtractedLineItem(
                raw_name="APA PLATA 2L",
                clean_name="Apă plată 2L",
                main_category="Groceries",
                subcategory="Beverages & Coffee",
                quantity=2.0,
                unit="buc",
                unit_price=4.00,
                net_total=8.00,
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
    fake_client = SimpleNamespace(models=FakeGeminiModels([parsed_schema]))
    extractor = ReceiptExtractor(settings, client=fake_client)
    writer = ExcelBudgetWriter.from_settings(settings)

    notifications: list[list[str]] = []
    notifier = DesktopNotifier(enabled=True, runner=notifications.append)

    pipeline = ReceiptPipeline(
        settings=settings,
        drive_client=drive,  # type: ignore[arg-type]
        extractor=extractor,
        excel_writer=writer,
        notifier=notifier,
    )

    stats = pipeline.process_drive_once()

    assert stats.processed == 1
    assert stats.failed == 0
    assert drive.deleted_ids == ["drv-1"]
    assert drive.list_pending_files() == []
    assert len(notifications) == 1
    assert "Lidl" in notifications[0][0]
    assert "deleted from Drive" in notifications[0][1]

    wb = load_workbook(settings.excel_path)
    assert wb["Receipts"].max_row == 2
    assert wb["Items"].max_row == 4


def test_corrupt_receipt_archived_locally_and_deleted_from_drive(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    bad_bytes = b"not-a-valid-image-at-all"
    drive_file = DriveFile(
        id="drv-bad",
        name="corrupt.jpg",
        mime_type="image/jpeg",
        size=len(bad_bytes),
        created_time="2026-11-17T10:01:00Z",
    )
    drive = FakeDriveClient({"drv-bad": (drive_file, bad_bytes)})

    fake_client = SimpleNamespace(models=FakeGeminiModels([]))
    extractor = ReceiptExtractor(settings, client=fake_client)
    writer = ExcelBudgetWriter.from_settings(settings)
    writer.ensure_initialized()

    notifications: list[list[str]] = []
    notifier = DesktopNotifier(enabled=True, runner=notifications.append)

    pipeline = ReceiptPipeline(
        settings=settings,
        drive_client=drive,  # type: ignore[arg-type]
        extractor=extractor,
        excel_writer=writer,
        notifier=notifier,
    )

    stats = pipeline.process_drive_once()

    assert stats.processed == 0
    assert stats.failed == 1
    # Must delete from Drive so cloud storage is freed
    assert drive.deleted_ids == ["drv-bad"]
    # Must preserve local copy + error log in failed_receipts/
    archived = settings.failed_dir / "corrupt.jpg"
    assert archived.exists()
    assert archived.read_bytes() == bad_bytes
    assert (settings.failed_dir / "corrupt.jpg.error.txt").exists()
    # Excel must remain untouched (header row only)
    wb = load_workbook(settings.excel_path)
    assert wb["Receipts"].max_row == 1
    assert len(notifications) == 1
    assert "Receipt Failed" in notifications[0][0]


def test_service_error_and_lock_preserve_file_in_drive(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    png_payload = _png_bytes()
    drive_file = DriveFile(
        id="drv-keep",
        name="keep.png",
        mime_type="image/png",
        size=len(png_payload),
        created_time="2026-11-17T10:02:00Z",
    )
    drive = FakeDriveClient({"drv-keep": (drive_file, png_payload)})

    # Missing API key must raise GeminiServiceError (not ReceiptExtractionError)
    bad_key_settings = replace(settings, gemini_api_key="")
    with pytest.raises(GeminiServiceError, match="GEMINI_API_KEY"):
        ReceiptExtractor(bad_key_settings).extract(
            _write_temp_png(tmp_path / "missing_key.png", png_payload)
        )

    # Simulate transient network outage exhausting retries
    fake_client = SimpleNamespace(
        models=FakeGeminiModels([TimeoutError("t1"), TimeoutError("t2")])
    )
    extractor = ReceiptExtractor(settings, client=fake_client, max_retries=2, base_delay=0.0)
    writer = ExcelBudgetWriter.from_settings(settings)
    writer.ensure_initialized()

    pipeline = ReceiptPipeline(
        settings=settings,
        drive_client=drive,  # type: ignore[arg-type]
        extractor=extractor,
        excel_writer=writer,
        notifier=DesktopNotifier(enabled=False),
    )

    stats = pipeline.process_drive_once()
    assert stats.service_errors == 1
    assert drive.deleted_ids == []
    assert len(drive.list_pending_files()) == 1

    # Now simulate Excel lock before retrying
    lock_path = tmp_path / ".~lock.budget_tracker.xlsx#"
    lock_path.write_text("locked", encoding="utf-8")
    stats_locked = pipeline.process_drive_once()
    assert stats_locked.processed == 0
    assert drive.deleted_ids == []


def test_pdf_loading_and_retry_recovery(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    pdf_path = tmp_path / "efactura.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF")

    media_bytes, mime = load_receipt_media(pdf_path)
    assert mime == "application/pdf"
    assert media_bytes.startswith(b"%PDF")

    parsed_schema = ReceiptExtractionSchema(
        is_valid_receipt=True,
        store_name="Auchan",
        receipt_date="2026-11-17",
        total_amount=10.0,
        items=[
            ExtractedLineItem(
                raw_name="CAFEA",
                clean_name="Cafea măcinată",
                main_category="Groceries",
                subcategory="Beverages & Coffee",
                unit_price=10.0,
                net_total=10.0,
            )
        ],
    )
    fake_client = SimpleNamespace(
        models=FakeGeminiModels([ConnectionError("transient"), parsed_schema])
    )
    extractor = ReceiptExtractor(settings, client=fake_client, max_retries=2, base_delay=0.0)
    receipt = extractor.extract(pdf_path)

    assert isinstance(receipt, ExtractedReceipt)
    assert receipt.store_name == "Auchan"
    assert receipt.status == "OK"


def test_heic_conversion_and_invalid_receipt_rejected(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    heic_path = tmp_path / "iphone_receipt.heic"
    buf = io.BytesIO()
    Image.new("RGB", (30, 60), color=(240, 240, 240)).save(buf, format="HEIF")
    heic_path.write_bytes(buf.getvalue())

    converted_bytes, mime = load_receipt_media(heic_path)
    assert mime == "image/jpeg"
    assert converted_bytes[:2] == b"\xff\xd8"  # JPEG SOI header

    non_receipt = ReceiptExtractionSchema(
        is_valid_receipt=False,
        confidence_notes="Photo shows a cat, not a receipt.",
    )
    fake_client = SimpleNamespace(models=FakeGeminiModels([non_receipt]))
    extractor = ReceiptExtractor(settings, client=fake_client)
    with pytest.raises(ReceiptExtractionError, match="cat"):
        extractor.extract(heic_path)


def test_google_drive_client_folder_listing_and_permanent_delete(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    deleted_calls: list[str] = []
    created_bodies: list[dict[str, object]] = []

    class _Exec:
        def __init__(self, payload: object) -> None:
            self._payload = payload

        def execute(self) -> object:
            return self._payload

    class _FilesResource:
        def list(self, **kwargs: object) -> _Exec:
            q = str(kwargs.get("q", ""))
            if "mimeType = 'application/vnd.google-apps.folder'" in q:
                return _Exec({"files": []})
            return _Exec(
                {
                    "files": [
                        {
                            "id": "f1",
                            "name": "bon.jpg",
                            "mimeType": "image/jpeg",
                            "size": "1234",
                            "createdTime": "2026-11-17T08:00:00Z",
                        },
                        {
                            "id": "f2",
                            "name": "notes.txt",
                            "mimeType": "text/plain",
                            "size": "42",
                            "createdTime": "2026-11-17T08:01:00Z",
                        },
                        {
                            "id": "f3",
                            "name": "factura.pdf",
                            "mimeType": "application/octet-stream",
                            "size": "5000",
                            "createdTime": "2026-11-17T08:02:00Z",
                        },
                    ]
                }
            )

        def create(self, **kwargs: object) -> _Exec:
            created_bodies.append(dict(kwargs.get("body", {})))  # type: ignore[arg-type]
            return _Exec({"id": "folder-receipts-id", "name": "receipts"})

        def delete(self, *, fileId: str) -> _Exec:
            deleted_calls.append(fileId)
            return _Exec(None)

    fake_service = SimpleNamespace(files=lambda: _FilesResource())
    client = GoogleDriveClient(settings, service=fake_service)

    pending = client.list_pending_files()
    assert len(created_bodies) == 1
    assert created_bodies[0]["name"] == "receipts"
    assert [f.id for f in pending] == ["f1", "f3"]

    client.delete_file_permanently("f1")
    assert deleted_calls == ["f1"]


def test_fallback_model_used_when_primary_unavailable(tmp_path: Path) -> None:
    settings = replace(
        _make_settings(tmp_path),
        gemini_model="gemini-3.8-flash",
        gemini_fallback_models=("gemini-3.7-flash", "gemini-3.6-flash"),
    )
    png_path = _write_temp_png(tmp_path / "bon.png", _png_bytes())

    parsed_schema = ReceiptExtractionSchema(
        is_valid_receipt=True,
        store_name="Penny",
        receipt_date="2026-11-17",
        total_amount=8.0,
        items=[
            ExtractedLineItem(
                raw_name="IAURT",
                clean_name="Iaurt natural",
                main_category="Groceries",
                subcategory="Dairy & Eggs",
                unit_price=8.0,
                net_total=8.0,
            )
        ],
    )
    fake_models = FakeGeminiModels([TimeoutError("503 busy"), parsed_schema])
    extractor = ReceiptExtractor(
        settings,
        client=SimpleNamespace(models=fake_models),
        max_retries=1,
        base_delay=0.0,
    )
    receipt = extractor.extract(png_path)

    assert receipt.store_name == "Penny"
    assert fake_models.models_called == ["gemini-3.8-flash", "gemini-3.7-flash"]


def test_google_drive_client_handles_multiple_matching_folders(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    queries: list[str] = []

    class _Exec:
        def __init__(self, payload: object) -> None:
            self._payload = payload

        def execute(self) -> object:
            return self._payload

    class _FilesResource:
        def list(self, **kwargs: object) -> _Exec:
            q = str(kwargs.get("q", ""))
            queries.append(q)
            if "mimeType = 'application/vnd.google-apps.folder'" in q:
                return _Exec(
                    {
                        "files": [
                            {"id": "folder_upper", "name": "Receipts"},
                            {"id": "folder_lower", "name": "receipts"},
                        ]
                    }
                )
            return _Exec(
                {
                    "files": [
                        {
                            "id": "pdf-1",
                            "name": "scan.pdf",
                            "mimeType": "application/pdf",
                            "size": "999",
                            "createdTime": "2026-11-17T15:11:57Z",
                        }
                    ]
                }
            )

    fake_service = SimpleNamespace(files=lambda: _FilesResource())
    client = GoogleDriveClient(settings, service=fake_service)

    pending = client.list_pending_files()
    assert [f.id for f in pending] == ["pdf-1"]
    assert "'folder_upper' in parents or 'folder_lower' in parents" in queries[1]


def test_extractor_timeout_defaults_to_90s_and_clamps_minimum_60s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_timeouts: list[int] = []

    def _fake_client(*, api_key: str, http_options: object) -> object:
        captured_timeouts.append(getattr(http_options, "timeout", 0))
        return SimpleNamespace(api_key=api_key)

    monkeypatch.setattr("src.extractor.genai.Client", _fake_client)

    default_settings = _make_settings(tmp_path)
    assert default_settings.gemini_timeout_seconds == 90
    ReceiptExtractor(default_settings)._get_client()

    low_settings = replace(default_settings, gemini_timeout_seconds=30)
    ReceiptExtractor(low_settings)._get_client()

    assert captured_timeouts == [90_000, 60_000]
