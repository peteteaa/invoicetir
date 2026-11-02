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

MAX_RETRIES = int(_env("MAX_RETRIES", "2"))
ZOOM_ENABLED = _env("ZOOM_ENABLED", "false").lower() in ("1", "true", "yes")
MAX_ZOOMS = int(_env("MAX_ZOOMS", "4"))            # zoom tool calls allowed per attempt

# Ambiguous numeric dates such as 07/10/2020 (pipeline/dates.py): "auto" = month first for USD
# documents, day first otherwise; or force "MDY" / "DMY". Such dates are always marked uncertain.
AMBIGUOUS_DATE_ORDER = _env("AMBIGUOUS_DATE_ORDER", "auto")

# Ingest
TEXT_LAYER_MIN_CHARS = int(_env("TEXT_LAYER_MIN_CHARS", "30"))   # per page
RENDER_DPI = int(_env("RENDER_DPI", "150"))        # page images sent to the vision model
ZOOM_DPI = int(_env("ZOOM_DPI", "300"))            # crops returned by the zoom tool
MAX_IMAGE_SIDE = int(_env("MAX_IMAGE_SIDE", "2000"))  # photos are shrunk to fit this

# Vendor normalization (clean.py): scores in [LOW, HIGH) are decided by the model.
VENDOR_MATCH_LOW = int(_env("VENDOR_MATCH_LOW", "80"))
VENDOR_MATCH_HIGH = int(_env("VENDOR_MATCH_HIGH", "92"))

PROMPTS_DIR = ROOT / "prompts"
LOG_PATH = ROOT / "logs" / "calls.jsonl"


def require(*names):
    """Exit with a clear message if any of the named settings is empty."""
    missing = [n for n in names if not globals()[n]]
    if missing:
        raise SystemExit(f"Missing settings: {', '.join(missing)}. Set them in .env (see .env.example).")
