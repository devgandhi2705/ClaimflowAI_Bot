"""Claim processing pipeline (orchestrator).

  1. Intake        -> claim id, image stored, SHA-256 fingerprint
  2. Exact dup     -> blocked BEFORE the AI call (saves API cost)                      [E2]
  3. AI extraction -> Gemini Vision, retry + backoff, dead-letter queue on outage     [E3]
  4. Validation    -> schema + confidence gate -> manual review queue                 [E1]
  5. Policy & risk -> fuzzy duplicates, YAML rules, risk score                         [E4]
  6. Decision      -> auto-approve / manager approval / auto-reject, audit + email

Each stage is isolated: an unexpected error marks only that claim FAILED, never the system.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from app import config, db, duplicate, notifier, policy_engine, risk
from app.config import load_policy
from app.extractor import AIServiceError, InvalidExtraction, extract
from app.logger import get_logger
from app.models import FINAL_STATUSES, Decision, ReceiptData, Status

MAX_QUEUE_RETRIES = 10
TOTAL_STAGES = 6

ProgressFn = Optional[Callable[[int, str], None]]


@dataclass
class PipelineResult:
    claim_id: str
    status: Status
    message: str
    data: Optional[ReceiptData] = None
    decision: Optional[Decision] = None
    duplicate_of: Optional[str] = None
    stage_times: List[tuple] = field(default_factory=list)


def submit_claim(image_bytes: bytes, chat_id, employee_name: str, purpose: str, mime_type: str = "image/jpeg",
                 progress: ProgressFn = None, extractor=extract, notify: bool = True) -> PipelineResult:
    """Stage 1 (intake) + the rest of the pipeline."""
    claim_id = db.next_claim_id()
    log = get_logger(claim_id, "intake")
    _p(progress, 1, "📥 Receipt received, creating claim")
    ext = "png" if "png" in mime_type else "jpg"
    image_path = config.RECEIPTS_DIR / f"{claim_id}.{ext}"
    image_path.write_bytes(image_bytes)
    image_hash = duplicate.image_fingerprint(image_bytes)
    db.insert_claim(claim_id, chat_id=str(chat_id), employee_name=employee_name, purpose=purpose,
                    image_path=str(image_path), image_hash=image_hash, status=Status.PROCESSING.value)
    db.audit(claim_id, employee_name or "employee", "SUBMITTED", None, Status.PROCESSING.value,
             {"purpose": purpose, "sha256": image_hash[:16]})
    log.info(f"Claim created by {employee_name} | purpose='{purpose}' | {len(image_bytes) / 1024:.0f} KB | "
             f"sha256={image_hash[:12]}...")
    return process_claim(claim_id, progress=progress, extractor=extractor, notify=notify)


def process_claim(claim_id: str, progress: ProgressFn = None, extractor=extract, notify: bool = True) -> PipelineResult:
    """Stages 2-6. Also used by the retry-queue worker to re-process parked claims."""
    started = time.perf_counter()
    log = get_logger(claim_id, "pipeline")
    claim = db.get_claim(claim_id)
    try:
        result = _run(claim, progress, extractor, started)
    except Exception as e:  # stage isolation: a bug in one claim never stops the bot
        log.exception(f"Unexpected pipeline error: {e}")
        db.set_status(claim_id, Status.FAILED.value, "system", "PIPELINE_ERROR", {"error": str(e)[:300]},
                      reason=f"System error: {e}")
        return PipelineResult(claim_id, Status.FAILED, "⚠️ An internal error occurred. The claim was logged for IT support.")

    total_ms = int((time.perf_counter() - started) * 1000)
    db.update_claim(claim_id, processing_ms=total_ms)
    get_logger(claim_id, "pipeline").info(f"Pipeline finished -> {result.status.value} in {total_ms} ms")
    if notify and result.status in FINAL_STATUSES:
        notifier.send_decision_email(claim_id)
    return result


def _run(claim: dict, progress: ProgressFn, extractor, started: float) -> PipelineResult:
    cid = claim["id"]
    timings = []

    # ---- Stage 2: exact duplicate (before spending an AI call) -------------------------
    t = time.perf_counter()
    _p(progress, 2, "🔐 Fingerprinting image & checking for duplicates")
    original = duplicate.find_exact_duplicate(claim["image_hash"], cid)
    timings.append(("duplicate_check", _ms(t)))
    if original:
        reason = f"Exact duplicate of {original['id']} (identical image fingerprint)"
        get_logger(cid, "duplicate").warning(f"EXCEPTION E2 - {reason}")
        db.set_status(cid, Status.DUPLICATE.value, "system", "DUPLICATE_BLOCKED",
                      {"original": original["id"]}, reason=reason, duplicate_of=original["id"],
                      risk_score=100, decided_by="ClaimFlow AI")
        return PipelineResult(cid, Status.DUPLICATE, reason, duplicate_of=original["id"], stage_times=timings)

    # ---- Stage 3: AI extraction ---------------------------------------------------------
    t = time.perf_counter()
    _p(progress, 3, "🧠 AI is reading the receipt (Gemini Vision)")
    log = get_logger(cid, "ai_extract")
    image_bytes = open(claim["image_path"], "rb").read()
    mime = "image/png" if claim["image_path"].endswith(".png") else "image/jpeg"
    try:
        data, raw, attempts, latency = extractor(image_bytes, mime, log)
    except AIServiceError as e:
        retries = (claim.get("retry_count") or 0) + 1
        if retries > MAX_QUEUE_RETRIES:
            db.set_status(cid, Status.FAILED.value, "system", "RETRY_EXHAUSTED", {"error": str(e)},
                          retry_count=retries, reason="AI service unavailable after repeated retries")
            log.error(f"Retry queue exhausted after {MAX_QUEUE_RETRIES} cycles - escalated to IT")
            return PipelineResult(cid, Status.FAILED, "AI service unavailable after repeated retries.")
        log.error(f"EXCEPTION E3 - {e} -> parked in retry queue (cycle {retries}/{MAX_QUEUE_RETRIES})")
        db.set_status(cid, Status.RETRY_PENDING.value, "system", "AI_SERVICE_DOWN", {"error": str(e)[:300]},
                      retry_count=retries, ai_attempts=config.AI_MAX_ATTEMPTS,
                      reason="AI service temporarily unavailable - queued for automatic retry")
        return PipelineResult(cid, Status.RETRY_PENDING, str(e), stage_times=timings)
    except InvalidExtraction as e:
        log.warning(f"EXCEPTION E1 - {e} -> manual review queue")
        db.set_status(cid, Status.NEEDS_REVIEW.value, "system", "INVALID_EXTRACTION", {"error": str(e)},
                      extraction_json=json.dumps(e.raw), reason=f"AI output could not be validated: {e}")
        return PipelineResult(cid, Status.NEEDS_REVIEW, str(e), stage_times=timings)
    timings.append(("ai_extraction", _ms(t)))
    log.info(f"Extracted vendor='{data.vendor}' amount={data.total_amount} {data.currency} date={data.receipt_date} "
             f"category={data.category.value} confidence={data.confidence:.2f} "
             f"[{raw.get('_model')}, {latency} ms, {attempts} attempt(s)]")
    db.update_claim(cid, vendor=data.vendor, receipt_date=data.receipt_date.isoformat() if data.receipt_date else None,
                    total_amount=data.total_amount, currency=data.currency, category=data.category.value,
                    invoice_number=data.invoice_number, confidence=data.confidence,
                    extraction_json=data.model_dump_json(), ai_latency_ms=latency, ai_attempts=attempts)
    db.audit(cid, "gemini", "AI_EXTRACTED", details={"confidence": data.confidence, "attempts": attempts,
                                                         "model": raw.get("_model")})

    # ---- Stage 4: validation / confidence gate ------------------------------------------
    t = time.perf_counter()
    _p(progress, 4, "✅ Validating extracted data")
    problems = []
    if not data.is_receipt:
        problems.append("image does not look like a receipt")
    if data.confidence < load_policy()["min_ai_confidence"]:
        problems.append(f"AI confidence {data.confidence:.0%} is below {load_policy()['min_ai_confidence']:.0%}")
    if data.total_amount is None or data.total_amount <= 0:
        problems.append("total amount not readable")
    if not data.vendor:
        problems.append("vendor not readable")
    timings.append(("validation", _ms(t)))
    if problems:
        reason = "Manual review needed: " + "; ".join(problems)
        get_logger(cid, "validation").warning(f"EXCEPTION E1 - {reason}")
        db.set_status(cid, Status.NEEDS_REVIEW.value, "system", "LOW_CONFIDENCE", {"problems": problems},
                      reason=reason, risk_score=None)
        return PipelineResult(cid, Status.NEEDS_REVIEW, reason, data=data, stage_times=timings)

    # ---- Stage 5: fuzzy duplicates + policy + risk ----------------------------------------
    t = time.perf_counter()
    _p(progress, 5, "📋 Applying expense policy & fraud checks")
    fuzzy = duplicate.find_fuzzy_duplicate(data, cid)
    flags = policy_engine.evaluate(data)
    decision = risk.decide(data, flags, fuzzy)
    timings.append(("policy_risk", _ms(t)))
    plog = get_logger(cid, "policy")
    for f in decision.flags:
        (plog.warning if f.severity == "hard" else plog.info)(f"[{f.severity.upper()}] {f.code}: {f.message} (+{f.points})")
    plog.info(f"Risk score {decision.risk_score}/100 -> {decision.status.value}")

    # ---- Stage 6: decision ------------------------------------------------------------
    _p(progress, 6, "⚖️ Routing decision")
    db.set_status(cid, decision.status.value, "ClaimFlow AI", "DECISION",
                  {"risk": decision.risk_score, "flags": [f.code for f in decision.flags]},
                  risk_score=decision.risk_score, reason=decision.reason,
                  flags_json=json.dumps([f.model_dump() for f in decision.flags]),
                  duplicate_of=fuzzy["claim_id"] if fuzzy else None,
                  decided_by="ClaimFlow AI" if decision.status != Status.PENDING_APPROVAL else None)
    return PipelineResult(cid, decision.status, decision.reason, data=data, decision=decision,
                          duplicate_of=fuzzy["claim_id"] if fuzzy else None, stage_times=timings)


def manager_decision(claim_id: str, approve: bool, actor: str, comment: str = "", notify: bool = True) -> dict:
    """Human-in-the-loop decision. Idempotent: a claim can only be decided once."""
    claim = db.get_claim(claim_id)
    if not claim:
        raise ValueError(f"{claim_id} not found")
    if claim["status"] not in (Status.PENDING_APPROVAL.value, Status.NEEDS_REVIEW.value):
        raise ValueError(f"{claim_id} is already {claim['status']}")
    new = Status.APPROVED if approve else Status.REJECTED
    reason = f"{'Approved' if approve else 'Rejected'} by {actor}" + (f": {comment}" if comment else "")
    db.set_status(claim_id, new.value, actor, "MANAGER_APPROVED" if approve else "MANAGER_REJECTED",
                  {"comment": comment}, decided_by=actor, reason=reason)
    get_logger(claim_id, "approval").info(f"{reason}")
    if notify:
        notifier.send_decision_email(claim_id)
    return db.get_claim(claim_id)


def process_retry_queue(extractor=extract, notify: bool = True) -> List[PipelineResult]:
    """Dead-letter queue worker: re-processes claims parked during an AI outage."""
    results = []
    for claim in db.claims_by_status(Status.RETRY_PENDING.value):
        get_logger(claim["id"], "retry_queue").info(f"Re-processing parked claim (cycle {claim['retry_count']})")
        db.audit(claim["id"], "scheduler", "RETRY_ATTEMPT")
        results.append(process_claim(claim["id"], extractor=extractor, notify=notify))
    return results


def _p(progress: ProgressFn, stage: int, label: str) -> None:
    if progress:
        try:
            progress(stage, label)
        except Exception:
            pass


def _ms(t: float) -> int:
    return int((time.perf_counter() - t) * 1000)
