"""Policy engine: evaluates extracted receipt data against the YAML expense policy.

Every rule produces an explainable Flag (code, severity, message, risk points), so each
decision can be justified to the employee, the manager and an auditor.
"""
from datetime import date
from typing import List, Optional

from app.config import load_policy
from app.models import Flag, ReceiptData


def evaluate(data: ReceiptData, today: Optional[date] = None, policy: Optional[dict] = None) -> List[Flag]:
    p = policy or load_policy()
    w = p["risk_weights"]
    today = today or date.today()
    flags: List[Flag] = []
    amount = data.total_amount or 0.0
    cat = data.category.value

    # 1. Receipt date rules
    if data.receipt_date:
        age = (today - data.receipt_date).days
        if age < 0:
            flags.append(Flag(code="FUTURE_DATE", severity="hard",
                              message=f"Receipt is dated in the future ({data.receipt_date})"))
        elif age > p["max_receipt_age_days"]:
            flags.append(Flag(code="RECEIPT_TOO_OLD", severity="hard",
                              message=f"Receipt is {age} days old (policy max {p['max_receipt_age_days']} days)"))
        if data.receipt_date.weekday() >= 5:
            flags.append(Flag(code="WEEKEND_EXPENSE", severity="warn", points=w["weekend_expense"],
                              message=f"Expense incurred on a {data.receipt_date.strftime('%A')}"))
    else:
        flags.append(Flag(code="MISSING_DATE", severity="warn", points=10, message="Receipt date not readable"))

    # 2. Category spending limits
    limit = p["category_limits"].get(cat, p["category_limits"]["other"])
    if amount > limit * p["hard_limit_multiplier"]:
        flags.append(Flag(code="EXCEEDS_HARD_LIMIT", severity="hard",
                          message=f"₹{amount:,.0f} is more than {p['hard_limit_multiplier']}x the {cat} limit (₹{limit:,})"))
    elif amount > limit:
        flags.append(Flag(code="OVER_CATEGORY_LIMIT", severity="warn", points=w["over_category_limit"],
                          message=f"₹{amount:,.0f} exceeds the {cat} limit of ₹{limit:,}"))

    # 3. Non-reimbursable items
    text = " ".join([data.vendor or ""] + [li.description for li in data.line_items]).lower()
    hits = sorted({k for k in p["prohibited_keywords"] if k in text})
    if hits:
        flags.append(Flag(code="PROHIBITED_ITEM", severity="warn", points=w["prohibited_item"],
                          message=f"Non-reimbursable item(s) detected: {', '.join(hits)}"))

    # 4. Fraud heuristics
    if amount >= 1000 and amount % 500 == 0:
        flags.append(Flag(code="ROUND_AMOUNT", severity="info", points=w["round_amount"],
                          message=f"Suspiciously round amount (₹{amount:,.0f})"))
    if data.currency != p["base_currency"]:
        flags.append(Flag(code="FOREIGN_CURRENCY", severity="warn", points=w["foreign_currency"],
                          message=f"Foreign currency ({data.currency}) needs conversion check"))
    return flags
