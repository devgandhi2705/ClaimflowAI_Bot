"""Unit tests for the policy engine and risk scoring."""
from datetime import date, timedelta

from app import policy_engine, risk
from app.models import Status
from tests.conftest import receipt


def codes(flags):
    return {f.code for f in flags}


def test_clean_receipt_has_no_flags():
    assert codes(policy_engine.evaluate(receipt())) == set()


def test_weekend_expense_flagged():
    saturday = date.today() - timedelta(days=(date.today().weekday() - 5) % 7 or 7)
    assert "WEEKEND_EXPENSE" in codes(policy_engine.evaluate(receipt(receipt_date=saturday)))


def test_future_date_is_hard_violation():
    flags = policy_engine.evaluate(receipt(receipt_date=date.today() + timedelta(days=3)))
    assert any(f.code == "FUTURE_DATE" and f.severity == "hard" for f in flags)


def test_over_category_limit_and_hard_limit():
    assert "OVER_CATEGORY_LIMIT" in codes(policy_engine.evaluate(receipt(total_amount=1800)))
    assert "EXCEEDS_HARD_LIMIT" in codes(policy_engine.evaluate(receipt(total_amount=5000)))


def test_round_amount_and_foreign_currency():
    c = codes(policy_engine.evaluate(receipt(total_amount=1500, currency="USD")))
    assert {"ROUND_AMOUNT", "FOREIGN_CURRENCY"} <= c


def test_risk_routing_thresholds():
    low = receipt(total_amount=500)
    assert risk.decide(low, policy_engine.evaluate(low)).status == Status.AUTO_APPROVED
    big = receipt(category="travel", total_amount=6200)
    assert risk.decide(big, policy_engine.evaluate(big)).status == Status.PENDING_APPROVAL
    hard = receipt(total_amount=9000)
    assert risk.decide(hard, policy_engine.evaluate(hard)).status == Status.AUTO_REJECTED
