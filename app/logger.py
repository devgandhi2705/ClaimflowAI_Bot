"""Structured logging: colored console + human-readable file + JSON-lines file for the dashboard."""
import sys

from loguru import logger

from app.config import LOG_DIR

LOG_JSON = LOG_DIR / "claimflow.jsonl"
LOG_TEXT = LOG_DIR / "claimflow.log"

_FMT = "{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {extra[claim_id]:<14} | {extra[stage]:<11} | {message}"

logger.remove()
logger.configure(extra={"claim_id": "-", "stage": "system"})
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | "
           "<cyan>{extra[claim_id]:<14}</cyan> | <magenta>{extra[stage]:<11}</magenta> | {message}", level="INFO")
logger.add(LOG_TEXT, format=_FMT, level="DEBUG", rotation="1 day", retention="14 days", encoding="utf-8")
logger.add(LOG_JSON, serialize=True, level="INFO", rotation="5 MB", retention=5, encoding="utf-8")


def get_logger(claim_id: str = "-", stage: str = "system"):
    """Logger bound to a claim (correlation id) and a pipeline stage."""
    return logger.bind(claim_id=claim_id, stage=stage)
