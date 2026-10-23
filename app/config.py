"""Central configuration: environment variables, paths and the policy file."""
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Project .env first, then the parent folder's .env (where the keys were first placed).
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")


def _env(*names: str, default: str = "") -> str:
    """Return the first non-empty env var among several accepted spellings."""
    for name in names:
        for variant in (name, name.upper(), name.lower()):
            value = os.getenv(variant)
            if value:
                return value.strip().strip('"')
    return default


GEMINI_API_KEY = _env("GEMINI_API_KEY", "Gemini_API_key")
# Primary model first, then fallbacks used when it is rate-limited or failing.
GEMINI_MODELS = [m.strip() for m in _env(
    "GEMINI_MODELS", default="gemini-2.5-flash,gemini-3.1-flash-lite,gemini-flash-lite-latest").split(",") if m.strip()]
GEMINI_MODEL = GEMINI_MODELS[0]
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "Telegram_bot_token")
MANAGER_CHAT_ID = _env("MANAGER_CHAT_ID")  # empty -> same chat as the employee (single-account mode)

GMAIL_ADDRESS = _env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD").replace(" ", "")
NOTIFY_EMAIL = _env("NOTIFY_EMAIL", default=GMAIL_ADDRESS)
EMAIL_ENABLED = _env("EMAIL_ENABLED", default="true").lower() == "true"

DATA_DIR = Path(_env("CLAIMFLOW_DATA_DIR", default=str(BASE_DIR / "data")))
RECEIPTS_DIR = DATA_DIR / "receipts"
VOUCHERS_DIR = DATA_DIR / "vouchers"
DB_PATH = Path(_env("CLAIMFLOW_DB", default=str(DATA_DIR / "claimflow.db")))
LOG_DIR = Path(_env("CLAIMFLOW_LOG_DIR", default=str(BASE_DIR / "logs")))
POLICY_PATH = BASE_DIR / "config" / "policy.yaml"

for _d in (DATA_DIR, RECEIPTS_DIR, VOUCHERS_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Retry settings for the AI call (exponential backoff 2s -> 4s -> 8s).
AI_MAX_ATTEMPTS = int(_env("AI_MAX_ATTEMPTS", default="3"))
RETRY_QUEUE_INTERVAL_SEC = int(_env("RETRY_QUEUE_INTERVAL_SEC", default="30"))


_POLICY_CACHE = {"mtime": None, "data": None}


def load_policy() -> dict:
    """Policy-as-code: re-reads policy.yaml whenever the file changes (live reload, no restart)."""
    mtime = POLICY_PATH.stat().st_mtime
    if _POLICY_CACHE["mtime"] != mtime:
        with open(POLICY_PATH, encoding="utf-8") as f:
            _POLICY_CACHE.update(mtime=mtime, data=yaml.safe_load(f))
    return _POLICY_CACHE["data"]


def reload_policy() -> dict:
    _POLICY_CACHE["mtime"] = None
    return load_policy()


def update_policy_value(key: str, value) -> None:
    """Edits one scalar in policy.yaml in place, keeping comments and layout."""
    text = POLICY_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^([ \t]*{re.escape(key)}:[ \t]*)([^#\n]*?)([ \t]*(#.*)?)$", re.M)
    if not pattern.search(text):
        raise KeyError(key)
    text = pattern.sub(lambda m: f"{m.group(1)}{value}{m.group(3)}", text, count=1)
    POLICY_PATH.write_text(text, encoding="utf-8")
    reload_policy()
