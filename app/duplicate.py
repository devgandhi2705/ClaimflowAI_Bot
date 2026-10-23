"""Duplicate & fraud detection.

Layer 1 - exact:  SHA-256 fingerprint of the image bytes (same file re-submitted).
Layer 2 - fuzzy:  same invoice number, or similar vendor + same amount + date within +/-1 day
                  (a different photo of the same bill).
"""
import hashlib
from datetime import date
from typing import Optional

from rapidfuzz import fuzz

from app import db
from app.models import ReceiptData

VENDOR_SIMILARITY_THRESHOLD = 85


def image_fingerprint(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def find_exact_duplicate(image_hash: str, claim_id: str) -> Optional[dict]:
    return db.find_by_hash(image_hash, claim_id)


def find_fuzzy_duplicate(data: ReceiptData, claim_id: str) -> Optional[dict]:
    """Returns {"claim_id", "similarity", "reason"} for the best suspicious match, else None."""
    if not data.vendor or data.total_amount is None:
        return None
    best = None
    for other in db.candidate_claims(claim_id):
        if data.invoice_number and other["invoice_number"] and \
                data.invoice_number.strip().lower() == other["invoice_number"].strip().lower() and \
                fuzz.token_set_ratio(data.vendor.lower(), (other["vendor"] or "").lower()) >= 70:
            return {"claim_id": other["id"], "similarity": 100, "reason": "same vendor and invoice number"}
        if other["total_amount"] is None or abs(other["total_amount"] - data.total_amount) > 0.01 * max(data.total_amount, 1):
            continue
        if data.receipt_date and other["receipt_date"]:
            gap = abs((data.receipt_date - date.fromisoformat(other["receipt_date"])).days)
            if gap > 1:
                continue
        sim = fuzz.token_set_ratio(data.vendor.lower(), (other["vendor"] or "").lower())
        if sim >= VENDOR_SIMILARITY_THRESHOLD and (best is None or sim > best["similarity"]):
            best = {"claim_id": other["id"], "similarity": int(sim), "reason": "same vendor, amount and date"}
    return best
