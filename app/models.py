"""Data contracts shared across the pipeline."""
from datetime import date as Date
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Category(str, Enum):
    meals = "meals"
    travel = "travel"
    lodging = "lodging"
    fuel = "fuel"
    office_supplies = "office_supplies"
    other = "other"


class LineItem(BaseModel):
    description: str
    amount: float


class ReceiptData(BaseModel):
    """Structured output of the AI extraction stage."""
    is_receipt: bool = Field(description="False if the image is not a bill/receipt/invoice")
    vendor: Optional[str] = None
    receipt_date: Optional[Date] = None
    total_amount: Optional[float] = None
    currency: str = "INR"
    category: Category = Category.other
    line_items: List[LineItem] = []
    invoice_number: Optional[str] = None
    confidence: float = Field(ge=0, le=1, description="Model's confidence that all fields are read correctly")
    notes: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, values):
        """LLMs sometimes return "" or "null" instead of null."""
        if isinstance(values, dict):
            return {k: (None if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a") else v)
                    for k, v in values.items()}
        return values

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, v) -> str:
        return str(v or "INR").upper()[:3]


class Status(str, Enum):
    PROCESSING = "PROCESSING"
    AUTO_APPROVED = "AUTO_APPROVED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AUTO_REJECTED = "AUTO_REJECTED"
    DUPLICATE = "DUPLICATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"


FINAL_STATUSES = {Status.AUTO_APPROVED, Status.APPROVED, Status.REJECTED, Status.AUTO_REJECTED, Status.DUPLICATE}


class Flag(BaseModel):
    code: str
    severity: str  # "info" | "warn" | "hard"
    message: str
    points: int = 0


class Decision(BaseModel):
    status: Status
    risk_score: int
    flags: List[Flag] = []
    reason: str
