"""Settings, all read from environment variables (placeholders in .env.example).

VISION_MODEL      small vision model for scans and photos
TEXT_MODEL        cheap text model for PDFs with a text layer
ESCALATION_MODEL  large model for failed documents and the baseline
Per-model input/output price per million tokens, for cost tracking.
MAX_RETRIES = 2, ZOOM_ENABLED (off by default), vendor fuzzy-match ambiguous score band.
"""
