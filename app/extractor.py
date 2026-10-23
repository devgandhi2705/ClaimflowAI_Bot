"""AI document understanding: Gemini Vision reads a receipt image into structured JSON.

Resilience: transient API failures are retried with exponential backoff (2s, 4s, 8s).
After the final attempt an AIServiceError is raised and the pipeline parks the claim
in the retry queue (dead-letter queue) instead of losing it.
"""
import json
import time
from datetime import date

from pydantic import ValidationError
from tenacity import RetryError, Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app import config, db
from app.models import ReceiptData

CHAOS_FLAG = config.DATA_DIR / "chaos_ai_outage.flag"


class AIServiceError(Exception):
    """The AI service is unreachable / rate-limited / failing after all retries."""


class InvalidExtraction(Exception):
    """The AI answered, but the result is not a valid receipt structure."""

    def __init__(self, message: str, raw: dict | None = None):
        super().__init__(message)
        self.raw = raw or {}


class _TransientError(Exception):
    pass


def chaos_enabled() -> bool:
    return CHAOS_FLAG.exists()


def set_chaos(enabled: bool) -> None:
    if enabled:
        CHAOS_FLAG.write_text("AI outage simulation active")
    else:
        CHAOS_FLAG.unlink(missing_ok=True)


PROMPT = """You are an expense-audit document parser. Today's date is {today}.
Read the attached image and extract the receipt fields defined by the response schema.

Rules:
- total_amount is the final payable total (after tax), as a plain number.
- currency is an ISO code inferred from symbols (Rs, Rs., INR, ₹ -> INR; $ -> USD).
- receipt_date in YYYY-MM-DD. Indian receipts use DD/MM/YYYY.
- category: restaurants/cafes/bars/food delivery -> meals; flights/train/cab/bus -> travel;
  hotels -> lodging; petrol/diesel/EV charging -> fuel; stationery/electronics -> office_supplies; else other.
- line_items: every purchased item with its amount (exclude subtotal/tax/total rows).
- confidence (0.0-1.0): be strict. Lower it for blurry, cropped, faded or partially visible text.
- If the image is not a bill/receipt/invoice, set is_receipt=false, confidence below 0.3 and leave other fields null.
- notes: anything unusual (handwritten, cropped, blurry, edited, etc.).
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_receipt": {"type": "BOOLEAN"},
        "vendor": {"type": "STRING", "nullable": True},
        "receipt_date": {"type": "STRING", "nullable": True, "description": "YYYY-MM-DD"},
        "total_amount": {"type": "NUMBER", "nullable": True},
        "currency": {"type": "STRING"},
        "category": {"type": "STRING", "enum": ["meals", "travel", "lodging", "fuel", "office_supplies", "other"]},
        "line_items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "description": {"type": "STRING"}, "amount": {"type": "NUMBER"}}, "required": ["description", "amount"]}},
        "invoice_number": {"type": "STRING", "nullable": True},
        "confidence": {"type": "NUMBER"},
        "notes": {"type": "STRING", "nullable": True},
    },
    "required": ["is_receipt", "vendor", "receipt_date", "total_amount", "currency", "category",
                 "line_items", "confidence"],
}


_CLIENT = None


def _client():
    """One long-lived client (re-used connection pool)."""
    global _CLIENT
    if _CLIENT is None:
        from google import genai  # imported lazily so tests run without network/SDK setup
        _CLIENT = genai.Client(api_key=config.GEMINI_API_KEY)
    return _CLIENT


def model_for_attempt(attempt: int) -> str:
    """Model failover: attempt 1 -> primary, attempt 2 -> first fallback, ... (separate free-tier quotas)."""
    return config.GEMINI_MODELS[(attempt - 1) % len(config.GEMINI_MODELS)]


def _call_gemini(image_bytes: bytes, mime_type: str, model: str) -> str:
    if chaos_enabled():
        raise _TransientError("503 UNAVAILABLE: simulated AI outage (chaos mode)")
    from google.genai import types
    try:
        resp = _client().models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                      PROMPT.format(today=date.today().isoformat())],
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=RESPONSE_SCHEMA, temperature=0),
        )
        return resp.text
    except Exception as e:  # network errors, 429 rate limits, 5xx
        raise _TransientError(f"{type(e).__name__}: {e}") from e


def extract(image_bytes: bytes, mime_type: str = "image/jpeg", log=None):
    """Returns (ReceiptData, raw_dict, attempts, latency_ms). The model used is in raw["_model"]."""
    attempts = 0
    model = config.GEMINI_MODELS[0]
    started = time.perf_counter()

    def _before_sleep(state):
        if log:
            err = str(state.outcome.exception())
            log.warning(f"AI call attempt {state.attempt_number} on {model_for_attempt(state.attempt_number)} failed "
                        f"({err[:160]}); failing over to {model_for_attempt(state.attempt_number + 1)} "
                        f"in {state.next_action.sleep:.0f}s")

    try:
        for attempt in Retrying(stop=stop_after_attempt(config.AI_MAX_ATTEMPTS),
                                wait=wait_exponential(multiplier=2, min=2, max=8),
                                retry=retry_if_exception_type(_TransientError),
                                before_sleep=_before_sleep):
            with attempt:
                attempts = attempt.retry_state.attempt_number
                model = model_for_attempt(attempts)
                t0 = time.perf_counter()
                try:
                    text = _call_gemini(image_bytes, mime_type, model)
                    db.record_metric("gemini", True, int((time.perf_counter() - t0) * 1000), model=model)
                except _TransientError as e:
                    db.record_metric("gemini", False, int((time.perf_counter() - t0) * 1000), str(e)[:300], model)
                    raise
    except RetryError as e:
        raise AIServiceError(f"AI service failed after {attempts} attempts across "
                             f"{min(attempts, len(config.GEMINI_MODELS))} model(s): "
                             f"{str(e.last_attempt.exception())[:200]}") from e

    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        raw = json.loads(text.strip().removeprefix("```json").removesuffix("```"))
        if isinstance(raw, list):
            raw = raw[0]
    except (json.JSONDecodeError, IndexError) as e:
        raise InvalidExtraction(f"AI returned non-JSON output: {e}") from e
    raw["_model"] = model
    try:
        return ReceiptData.model_validate(raw), raw, attempts, latency_ms
    except ValidationError as e:
        raise InvalidExtraction(f"Schema validation failed: {e.error_count()} error(s)", raw) from e
