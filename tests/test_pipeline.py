"""End-to-end scenario tests (TC01-TC10 in the project report)."""
import json
from datetime import date, timedelta

import pytest

from app import config, db, notifier, pipeline
from app.extractor import AIServiceError, InvalidExtraction
from app.models import Status
from tests.conftest import FakeExtractor, image_bytes, receipt


def submit(data=None, seed=0, extractor=None, purpose="Client meeting"):
    ex = extractor or FakeExtractor(data or receipt())
    return pipeline.submit_claim(image_bytes(seed), chat_id=1, employee_name="Test User", purpose=purpose,
                                 mime_type="image/png", extractor=ex, notify=False)


def flag_codes(claim_id):
    return {f["code"] for f in json.loads(db.get_claim(claim_id)["flags_json"] or "[]")}


def test_tc01_clear_meal_receipt_is_auto_approved():
    r = submit(receipt(total_amount=850))
    assert r.status == Status.AUTO_APPROVED
    c = db.get_claim(r.claim_id)
    assert c["vendor"] == "Spice Garden Restaurant" and c["total_amount"] == 850
    assert c["risk_score"] < 30 and c["processing_ms"] is not None


def test_tc02_hotel_bill_above_limit_goes_to_manager_and_is_approved():
    r = submit(receipt(vendor="Hotel Sea Breeze", category="lodging", total_amount=7450,
                       line_items=[{"description": "Deluxe room 1 night", "amount": 7450}]))
    assert r.status == Status.PENDING_APPROVAL
    c = pipeline.manager_decision(r.claim_id, True, "Manager", notify=False)
    assert c["status"] == "APPROVED" and c["decided_by"] == "Manager"
    actions = [a["action"] for a in db.audit_trail(r.claim_id)]
    assert actions == ["SUBMITTED", "AI_EXTRACTED", "DECISION", "MANAGER_APPROVED"]


def test_tc03_manager_rejects_and_decision_is_idempotent():
    r = submit(receipt(vendor="Hotel Sea Breeze", category="lodging", total_amount=7450))
    pipeline.manager_decision(r.claim_id, False, "Manager", "No hotel stay approved", notify=False)
    assert db.get_claim(r.claim_id)["status"] == "REJECTED"
    with pytest.raises(ValueError):  # double-click / second manager cannot flip a decision
        pipeline.manager_decision(r.claim_id, True, "Manager", notify=False)


def test_tc04_non_receipt_image_goes_to_manual_review():  # Exception E1
    r = submit(receipt(is_receipt=False, vendor=None, total_amount=None, confidence=0.1))
    assert r.status == Status.NEEDS_REVIEW
    assert "does not look like a receipt" in db.get_claim(r.claim_id)["reason"]


def test_tc04b_blurry_receipt_low_confidence_goes_to_manual_review():  # Exception E1
    r = submit(receipt(confidence=0.45))
    assert r.status == Status.NEEDS_REVIEW


def test_tc04c_malformed_ai_output_goes_to_manual_review():  # Exception E1
    r = submit(extractor=FakeExtractor(error=InvalidExtraction("Schema validation failed")))
    assert r.status == Status.NEEDS_REVIEW


def test_tc05_exact_duplicate_blocked_before_ai_call():  # Exception E2
    first = submit(seed=7)
    ex = FakeExtractor(receipt())
    second = submit(seed=7, extractor=ex)
    assert second.status == Status.DUPLICATE
    assert second.duplicate_of == first.claim_id
    assert ex.calls == 0, "duplicate must be caught before spending an AI call"


def test_tc06_different_photo_of_same_bill_flagged_as_suspected_duplicate():
    first = submit(receipt(invoice_number="INV-881"), seed=1)
    second = submit(receipt(vendor="SPICE GARDEN RESTAURANT", invoice_number=None), seed=2)
    assert first.status == Status.AUTO_APPROVED
    assert "SUSPECTED_DUPLICATE" in flag_codes(second.claim_id)
    assert second.status == Status.PENDING_APPROVAL


def test_tc07_receipt_older_than_policy_is_auto_rejected():  # Exception E4
    r = submit(receipt(receipt_date=date.today() - timedelta(days=60)))
    assert r.status == Status.AUTO_REJECTED
    assert "RECEIPT_TOO_OLD" in flag_codes(r.claim_id)


def test_tc08_alcohol_on_bill_is_flagged_and_not_auto_approved():  # Exception E4
    r = submit(receipt(vendor="Skyline Bar & Grill", total_amount=1650,
                       line_items=[{"description": "Paneer Tikka", "amount": 450},
                                   {"description": "Beer pint x2", "amount": 1200}]))
    assert "PROHIBITED_ITEM" in flag_codes(r.claim_id)
    assert r.status != Status.AUTO_APPROVED


def test_tc09_ai_outage_parks_claim_then_retry_queue_recovers():  # Exception E3
    r = submit(extractor=FakeExtractor(error=AIServiceError("503 UNAVAILABLE")))
    assert r.status == Status.RETRY_PENDING
    assert db.get_claim(r.claim_id)["retry_count"] == 1
    results = pipeline.process_retry_queue(extractor=FakeExtractor(receipt()), notify=False)
    assert [x.status for x in results] == [Status.AUTO_APPROVED]


def test_tc09b_real_extractor_retries_with_backoff_under_chaos(monkeypatch):  # Exception E3
    from app import extractor
    monkeypatch.setattr(config, "AI_MAX_ATTEMPTS", 2)
    extractor.set_chaos(True)
    try:
        with pytest.raises(AIServiceError):
            extractor.extract(image_bytes(), "image/png")
    finally:
        extractor.set_chaos(False)
    with db.connect() as c:
        fails = c.execute("SELECT COUNT(*) FROM api_metrics WHERE service='gemini' AND success=0").fetchone()[0]
    assert fails == 2


def test_tc10_email_failure_does_not_lose_the_claim(monkeypatch):
    r = submit(receipt())
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "x@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "dummy")

    def boom(msg):
        raise ConnectionError("SMTP server unreachable")
    monkeypatch.setattr(notifier, "_send", boom)
    assert notifier.send_decision_email(r.claim_id) is False
    assert db.get_claim(r.claim_id)["status"] == "AUTO_APPROVED"
    assert "EMAIL_FAILED" in [a["action"] for a in db.audit_trail(r.claim_id)]


def test_voucher_pdf_is_generated():
    r = submit(receipt())
    pdf = notifier.build_voucher(db.get_claim(r.claim_id))
    assert pdf.exists() and pdf.read_bytes()[:4] == b"%PDF"


def test_email_goes_to_registered_employee_with_finance_copy(monkeypatch):
    r = submit(receipt())
    sent = []
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "bot@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "dummy")
    monkeypatch.setattr(config, "NOTIFY_EMAIL", "finance@example.com")
    monkeypatch.setattr(notifier, "_send", lambda msg: sent.append(msg))
    notifier.send_decision_email(r.claim_id)                      # no email registered -> finance only
    db.upsert_user(1, "Test User", email="emp@example.com", email_asked=1)
    notifier.send_decision_email(r.claim_id)                      # registered -> employee, cc finance
    assert (sent[0]["To"], sent[0]["Cc"]) == ("finance@example.com", None)
    assert (sent[1]["To"], sent[1]["Cc"]) == ("emp@example.com", "finance@example.com")
