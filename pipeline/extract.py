"""One extraction attempt: prompts/extract.md + document -> ExtractionResult.

Has a `zoom` tool (high-res crop of a page region) behind config.ZOOM_ENABLED, off by default.
It is only offered for image documents; text documents already have exact text.

repair.py calls extract() again with `followup` messages (the previous answer and the exact
check failures) appended after the document.
"""
import base64
import json
from dataclasses import dataclass, field
from typing import Optional

import pymupdf

import config
import llm
from pipeline import normalize
from pipeline.ingest import Document, page_rect, render_page
from schemas import ExtractionResult

ZOOM_TOOL = {
    "type": "function",
    "function": {
        "name": "zoom",
        "description": ("Get a high-resolution crop of a region of a page, to read small or "
                        "blurry text. Coordinates are fractions of the page width and height, "
                        "0 = left/top, 1 = right/bottom."),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "1-based page number"},
                "x0": {"type": "number"}, "y0": {"type": "number"},
                "x1": {"type": "number"}, "y1": {"type": "number"},
            },
            "required": ["page", "x0", "y0", "x1", "y1"],
        },
    },
}


@dataclass
class Attempt:
    model: str
    result: Optional[ExtractionResult]   # None when the reply was invalid
    error: Optional[str]                 # validation error, phrased for a re-prompt
    raw: str                             # the model's final text
    calls: list = field(default_factory=list)  # llm.CallResult for every call in this attempt

    @property
    def input_tokens(self): return sum(c.input_tokens for c in self.calls)
    @property
    def output_tokens(self): return sum(c.output_tokens for c in self.calls)
    @property
    def latency_s(self): return sum(c.latency_s for c in self.calls)
    @property
    def cost_usd(self):
        costs = [c.cost_usd for c in self.calls]
        return None if None in costs else sum(costs)


def prompt() -> str:
    return (config.PROMPTS_DIR / "extract.md").read_text()


def _image_part(png: bytes) -> dict:
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}


def document_message(doc: Document) -> dict:
    if doc.kind == "text":
        return {"role": "user", "content": f"Document {doc.doc_id} (text layer):\n\n{doc.text()}"}
    content = [{"type": "text", "text": f"Document {doc.doc_id}, {len(doc.pages)} page(s):"}]
    for p in doc.pages:
        content += [{"type": "text", "text": f"Page {p.number}"}, _image_part(p.image_png)]
    return {"role": "user", "content": content}


def default_model(doc: Document) -> str:
    return config.TEXT_MODEL if doc.kind == "text" else config.VISION_MODEL


def _zoom(doc: Document, args: dict):
    """Return (tool result text, PNG bytes or None)."""
    try:
        page = int(args["page"])
        if not 1 <= page <= len(doc.pages):
            return f"error: page must be 1..{len(doc.pages)}", None
        x0, y0, x1, y1 = (min(max(float(args[k]), 0.0), 1.0) for k in ("x0", "y0", "x1", "y1"))
        if x1 <= x0 or y1 <= y0:
            return "error: need x0 < x1 and y0 < y1", None
        if doc.path.suffix.lower() == ".pdf":
            r = page_rect(doc.path, page)
            clip = pymupdf.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height,
                                r.x0 + x1 * r.width, r.y0 + y1 * r.height)
            png = render_page(doc.path, page, config.ZOOM_DPI, clip)
        else:  # image file: crop the original pixels
            pix = pymupdf.Pixmap(str(doc.path))
            irect = pymupdf.IRect(int(x0 * pix.width), int(y0 * pix.height),
                                  int(x1 * pix.width), int(y1 * pix.height))
            crop = pymupdf.Pixmap(pix.colorspace, irect, pix.alpha)
            crop.copy(pix, irect)
            png = crop.tobytes("png")
        return f"zoomed page {page} region ({x0:.2f},{y0:.2f})-({x1:.2f},{y1:.2f}); image follows", png
    except (KeyError, ValueError, TypeError) as e:
        return f"error: bad zoom arguments ({e})", None


def _assistant_dict(message) -> dict:
    d = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in message.tool_calls]
    return d


def extract(doc: Document, model: Optional[str] = None, stage: str = "extract",
            followup: Optional[list] = None) -> Attempt:
    """One attempt. Invalid JSON is not retried here; it is returned for repair.py to count."""
    model = model or default_model(doc)
    if not model:
        config.require("TEXT_MODEL" if doc.kind == "text" else "VISION_MODEL")
    messages = [{"role": "system", "content": prompt()}, document_message(doc), *(followup or [])]
    use_zoom = config.ZOOM_ENABLED and doc.kind == "image"
    calls = []

    for _ in range(config.MAX_ZOOMS if use_zoom else 0):
        c = llm.call(doc_id=doc.doc_id, stage=f"{stage}:zoom", model=model, messages=messages,
                     schema=ExtractionResult, tools=[ZOOM_TOOL])
        calls.append(c)
        if not c.message.tool_calls:
            break
        messages.append(_assistant_dict(c.message))
        images = []
        for tc in c.message.tool_calls:
            if tc.function.name != "zoom":
                text, png = f"error: unknown tool {tc.function.name}", None
            else:
                try:
                    text, png = _zoom(doc, json.loads(tc.function.arguments or "{}"))
                except json.JSONDecodeError:
                    text, png = "error: arguments were not valid JSON", None
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
            if png:
                images.append(_image_part(png))
        if images:  # tool messages carry text only, so crops go in a user message
            messages.append({"role": "user", "content": [{"type": "text", "text": "Zoomed crops:"}, *images]})
    else:
        # No zoom, or zoom budget used up: final answer in JSON mode, no tools.
        calls.append(llm.call(doc_id=doc.doc_id, stage=stage, model=model, messages=messages,
                              schema=ExtractionResult))

    final = calls[-1]
    result = final.parsed
    if result is not None and result.invoice is not None:
        normalize.resolve(result.invoice, result.uncertain_fields)
        result.invoice.doc_id = doc.doc_id
        result.invoice.source_pages = [p.number for p in doc.pages]
        if len(doc.pages) == 1:  # provenance, not a document value: there is only one page
            for item in result.invoice.line_items:
                item.page = 1
    return Attempt(model, result, final.error, final.text, calls)
