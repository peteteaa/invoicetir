"""Where the Mendeley sample invoices are: directly in data/docs/ (as the README says), or in the
folder the download unzips to, data/docs/Samples of electronic invoices/. If neither has them,
DOCS is a folder that doesn't exist, so `not DOCS.is_dir()` still skips the tests that need them."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "data" / "docs"
_UNZIPPED = _ROOT / "Samples of electronic invoices"
DOCS = next((d for d in (_UNZIPPED, _ROOT) if (d / "invoice_106.pdf").is_file()), _UNZIPPED)
