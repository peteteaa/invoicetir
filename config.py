"""Settings, all read from environment variables (placeholders in .env.example).

VISION_MODEL      small vision model for scans and photos
TEXT_MODEL        cheap text model for PDFs with a text layer
ESCALATION_MODEL  large model for failed documents and the baseline
Per-model input/output price per million tokens, for cost tracking.
MAX_RETRIES = 2, ZOOM_ENABLED (off by default), vendor fuzzy-match ambiguous score band.
"""
import os
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv():
    """Read simple KEY=VALUE lines from ./.env without overriding real env vars."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def _price(name):
    value = _env(name)
    return Decimal(value) if value else None


FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
FIREWORKS_API_KEY = _env("FIREWORKS_API_KEY")

VISION_MODEL = _env("VISION_MODEL")
TEXT_MODEL = _env("TEXT_MODEL")
ESCALATION_MODEL = _env("ESCALATION_MODEL")

# Per model ID: USD per million tokens {model: (input_price, output_price)}, and the reasoning
# effort to request ("none", "low", "medium", "high"; empty = the model's default). Reasoning
# models left at their default can think for tens of thousands of tokens on one invoice.
PRICES = {}
REASONING_EFFORT = {}
for _role in ("VISION_MODEL", "TEXT_MODEL", "ESCALATION_MODEL"):
    if _env(_role):
        PRICES[_env(_role)] = (_price(f"{_role}_INPUT_PRICE"), _price(f"{_role}_OUTPUT_PRICE"))
        if _env(f"{_role}_REASONING_EFFORT"):
            REASONING_EFFORT[_env(_role)] = _env(f"{_role}_REASONING_EFFORT")

MAX_OUTPUT_TOKENS = int(_env("MAX_OUTPUT_TOKENS", "8000"))   # a cut-off reply counts as a retry
REQUEST_TIMEOUT_S = float(_env("REQUEST_TIMEOUT_S", "120"))
API_MAX_RETRIES = int(_env("API_MAX_RETRIES", "6"))   # rate limits / server errors, with backoff
WORKERS = int(_env("WORKERS", "4"))   # documents extracted at once; 12 hit Fireworks rate limits

MAX_RETRIES = int(_env("MAX_RETRIES", "2"))
# Fields a document must have (checks.py). Some sources never print some of them (CORD masks
# the store, so no vendor or date): set e.g. REQUIRED_FIELDS=total for those.
REQUIRED_FIELDS = [f.strip() for f in _env("REQUIRED_FIELDS", "vendor_name,invoice_date,currency,total").split(",")
                   if f.strip()]
# Currency to assume when none is printed at all (e.g. IDR for Indonesian receipts); always
# marked uncertain. Empty = none: a missing currency stays missing.
DEFAULT_CURRENCY = _env("DEFAULT_CURRENCY").upper() or None
# Escalate when the small model passes the checks but is unsure about a money or vendor field.
ESCALATE_ON_UNCERTAIN = _env("ESCALATE_ON_UNCERTAIN", "true").lower() in ("1", "true", "yes")
# Also escalate / review over doubts about text that can't change an amount (item names, the
# vendor). Off: on real receipts those doubts were mostly false alarms.
REVIEW_TEXT_DOUBTS = _env("REVIEW_TEXT_DOUBTS", "false").lower() in ("1", "true", "yes")
ZOOM_ENABLED = _env("ZOOM_ENABLED", "false").lower() in ("1", "true", "yes")
MAX_ZOOMS = int(_env("MAX_ZOOMS", "4"))            # zoom tool calls allowed per attempt

# Ambiguous numeric dates such as 07/10/2020 (pipeline/dates.py): "auto" = month first for USD
# documents, day first otherwise; or force "MDY" / "DMY". Such dates are always marked uncertain.
AMBIGUOUS_DATE_ORDER = _env("AMBIGUOUS_DATE_ORDER", "auto")

# ISO code for documents that print no currency at all, e.g. IDR for the CORD receipts (they
# never print one). Used only when nothing is printed and the model reads none; always marked
# uncertain. Empty = no default: such a document fails the checks ("currency missing").
DEFAULT_CURRENCY = _env("DEFAULT_CURRENCY").upper()

# Ingest
TEXT_LAYER_MIN_CHARS = int(_env("TEXT_LAYER_MIN_CHARS", "30"))   # per page
RENDER_DPI = int(_env("RENDER_DPI", "150"))        # page images sent to the vision model
ZOOM_DPI = int(_env("ZOOM_DPI", "300"))            # crops returned by the zoom tool
MAX_IMAGE_SIDE = int(_env("MAX_IMAGE_SIDE", "2000"))  # photos are shrunk to fit this

# Vendor normalization (clean.py): scores in [LOW, HIGH) are decided by the model.
VENDOR_MATCH_LOW = int(_env("VENDOR_MATCH_LOW", "85"))
VENDOR_MATCH_HIGH = int(_env("VENDOR_MATCH_HIGH", "95"))

PROMPTS_DIR = ROOT / "prompts"
LOG_PATH = ROOT / "logs" / "calls.jsonl"


def require(*names):
    """Exit with a clear message if any of the named settings is empty."""
    missing = [n for n in names if not globals()[n]]
    if missing:
        raise SystemExit(f"Missing settings: {', '.join(missing)}. Set them in .env (see .env.example).")
