"""Frontier-model baseline: Claude, through the official Anthropic SDK.

Used by eval/run_eval.py for two configs that mirror the Kimi K3 ones:
  claude_per_doc   Claude reads each document with the same extraction prompt, no checks
  claude_all       Claude gets every document at once and reports spend by category

Claude gets exactly what the Fireworks models get: prompts/extract.md (or prompts/summary.md),
the same layout text or page images, and the same Pydantic schema, which client.beta.messages.parse
enforces; normalize.py then turns printed dates and currencies into ISO values the same way.

Settings (.env): ANTHROPIC_API_KEY (or an `ant auth login` profile), CLAUDE_MODEL (default
claude-opus-5-5), CLAUDE_EFFORT (default medium, which is Opus 5.5's own default), and
CLAUDE_INPUT_PRICE / CLAUDE_OUTPUT_PRICE in USD per million tokens (default 4 / 20, Opus 5.5).
Server-side refusal fallbacks are on: a declined request is re-run on a fallback model inside
the same call; the model that answered is logged.
"""
import base64
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import config
import llm
from pipeline import ingest, normalize
from schemas import ExtractionResult

MODEL = config._env("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = config._env("CLAUDE_EFFORT", "medium")
PRICE_IN = Decimal(config._env("CLAUDE_INPUT_PRICE", "4"))
PRICE_OUT = Decimal(config._env("CLAUDE_OUTPUT_PRICE", "20"))
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=config.REQUEST_TIMEOUT_S, max_retries=config.API_MAX_RETRIES)
    return _client


def has_credentials() -> bool:
    """An API key, an auth token, or an `ant auth login` profile on disk."""
    import os
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE") or (Path.home() / ".config" / "anthropic").is_dir())


def content(doc: ingest.Document) -> list[dict]:
    """The document as Anthropic content blocks: layout text, or one image per page."""
    if doc.kind == "text":
        return [{"type": "text", "text": f"Document {doc.doc_id} (text layer):\n\n{doc.text()}"}]
    blocks = [{"type": "text", "text": f"Document {doc.doc_id}, {len(doc.pages)} page(s):"}]
    for p in doc.pages:
        blocks += [{"type": "text", "text": f"Page {p.number}"},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                "data": base64.standard_b64encode(p.image_png).decode()}}]
    return blocks


def call(*, doc_id: str, stage: str, system: str, user: list[dict], schema):
    """One parse() call. Returns (parsed or None, error or None, input tokens, output tokens,
    latency seconds, cost). Logged to logs/calls.jsonl like every Fireworks call."""
    start = time.monotonic()
    try:
        response = client().beta.messages.parse(
            model=MODEL, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema, output_config={"effort": EFFORT},
            betas=[FALLBACK_BETA], fallbacks="default")
    except Exception as e:
        latency = time.monotonic() - start
        llm.log({"ts": datetime.now(timezone.utc).isoformat(), "doc_id": doc_id, "stage": stage, "model": MODEL,
                 "input_tokens": 0, "output_tokens": 0, "latency_s": round(latency, 3), "cost_usd": None,
                 "ok": False, "error": f"{type(e).__name__}: {e}"})
        return None, f"{type(e).__name__}: {e}", 0, 0, latency, Decimal(0)
    latency = time.monotonic() - start
    usage = response.usage
    tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    tokens_out = usage.output_tokens or 0
    cost = (PRICE_IN * tokens_in + PRICE_OUT * tokens_out) / Decimal(1_000_000)
    parsed, error = response.parsed_output, None
    if response.stop_reason == "refusal":
        parsed, error = None, f"refused ({getattr(response.stop_details, 'category', None)})"
    elif response.stop_reason == "max_tokens":
        parsed, error = None, "cut off at max_tokens"
    elif parsed is None:
        error = "no parsed output"
    llm.log({"ts": datetime.now(timezone.utc).isoformat(), "doc_id": doc_id, "stage": stage,
             "model": response.model, "input_tokens": tokens_in, "output_tokens": tokens_out,
             "latency_s": round(latency, 3), "cost_usd": cost, "ok": error is None, "error": error})
    return parsed, error, tokens_in, tokens_out, latency, cost


def extract(doc: ingest.Document, stage: str = "claude_extract"):
    """Extraction with the shared prompt and schema, finished the way pipeline/extract.py
    finishes Fireworks answers. Returns (ExtractionResult or None, error, usage dict)."""
    system = (config.PROMPTS_DIR / "extract.md").read_text()
    result, error, t_in, t_out, latency, cost = call(doc_id=doc.doc_id, stage=stage, system=system,
                                                     user=content(doc), schema=ExtractionResult)
    if result is not None and result.invoice is not None:
        normalize.resolve(result.invoice, result.uncertain_fields)
        result.invoice.doc_id = doc.doc_id
        result.invoice.source_pages = [p.number for p in doc.pages]
        if len(doc.pages) == 1:
            for item in result.invoice.line_items:
                item.page = 1
    return result, error, {"input_tokens": t_in, "output_tokens": t_out, "latency_s": latency, "cost_usd": cost}
