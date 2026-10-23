import io
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

# Isolate tests from real data before the app is imported.
_TMP = Path(tempfile.mkdtemp(prefix="claimflow-test-"))
os.environ["CLAIMFLOW_DATA_DIR"] = str(_TMP / "data")
os.environ["CLAIMFLOW_LOG_DIR"] = str(_TMP / "logs")
os.environ["CLAIMFLOW_DB"] = str(_TMP / "data" / "test.db")
os.environ["EMAIL_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app import config, db  # noqa: E402
from app.models import ReceiptData  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "claims.db")
    monkeypatch.setattr(config, "RECEIPTS_DIR", tmp_path)
    monkeypatch.setattr(config, "VOUCHERS_DIR", tmp_path)
    db.init_db()
    yield


def recent_weekday(days_ago: int = 3) -> date:
    d = date.today() - timedelta(days=days_ago)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def receipt(**kw) -> ReceiptData:
    base = dict(is_receipt=True, vendor="Spice Garden Restaurant", receipt_date=recent_weekday(),
                total_amount=850.0, currency="INR", category="meals",
                line_items=[{"description": "Veg Thali", "amount": 850.0}], invoice_number=None, confidence=0.95)
    base.update(kw)
    return ReceiptData(**base)


def image_bytes(seed: int = 0) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), ((seed * 37) % 255, (seed * 91) % 255, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeExtractor:
    """Stands in for Gemini so tests are deterministic, fast and free."""

    def __init__(self, data: ReceiptData = None, error: Exception = None):
        self.data, self.error, self.calls = data, error, 0

    def __call__(self, image_bytes, mime, log=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data, self.data.model_dump(mode="json"), 1, 42
