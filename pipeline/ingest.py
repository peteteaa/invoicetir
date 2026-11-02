"""Load PDFs and images.

If a PDF has a usable text layer (PyMuPDF), return its text with page markers for TEXT_MODEL;
otherwise render the pages to images for VISION_MODEL.

The text keeps the page layout: words on the same line stay on one line, at roughly their
horizontal position, so table rows (description | qty | price | amount) stay together.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pymupdf

import config

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}


@dataclass
class Page:
    number: int                       # 1-based
    text: Optional[str] = None        # layout text, for text documents
    image_png: Optional[bytes] = None  # rendered page, for image documents


@dataclass
class Document:
    doc_id: str
    path: Path
    kind: str                         # "text" -> TEXT_MODEL, "image" -> VISION_MODEL
    pages: list[Page] = field(default_factory=list)
    reason: str = ""                  # why this kind was chosen

    def text(self) -> str:
        """All pages' text with page markers."""
        return "\n\n".join(f"=== page {p.number} ===\n{p.text}" for p in self.pages)


def layout_text(page: pymupdf.Page) -> str:
    """Page text with words grouped into visual lines and spaced by x position."""
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    if not words:
        return ""
    widths = [(w[2] - w[0]) / len(w[4]) for w in words if w[4]]
    char_w = sorted(widths)[len(widths) // 2] or 5.0  # median character width

    # Cluster words into lines by vertical centre.
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines, current, current_y = [], [], None
    for w in words:
        y = (w[1] + w[3]) / 2
        if current and abs(y - current_y) > (w[3] - w[1]) * 0.5:
            lines.append(current)
            current = []
        if not current:
            current_y = y
        current.append(w)
    lines.append(current)

    out = []
    for line in lines:
        text, prev_x1 = "", None
        for w in sorted(line, key=lambda w: w[0]):
            if prev_x1 is not None and w[0] - prev_x1 < char_w * 1.5:
                text += " " + w[4]  # same phrase: one space
            else:  # new column: pad to the word's x position
                text += " " * max(int(w[0] / char_w) - len(text), 1 if text else 0) + w[4]
            prev_x1 = w[2]
        out.append(text.rstrip())
    return "\n".join(out)


def _usable(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < config.TEXT_LAYER_MIN_CHARS:
        return False
    junk = stripped.count("�") + stripped.count("(cid:")
    return junk / len(stripped) < 0.05


def _png(pix: pymupdf.Pixmap) -> bytes:
    while max(pix.width, pix.height) > config.MAX_IMAGE_SIDE:
        pix.shrink(1)  # halves each side
    if pix.alpha or pix.n - pix.alpha != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix, 0)
    return pix.tobytes("png")


def render_page(path: Path, page_number: int, dpi: int, clip: Optional[pymupdf.Rect] = None) -> bytes:
    """Render one page (or a region of it, in PDF points) to PNG. Used by the zoom tool."""
    with pymupdf.open(path) as doc:
        page = doc[page_number - 1]
        return _png(page.get_pixmap(dpi=dpi, clip=clip))


def page_rect(path: Path, page_number: int) -> pymupdf.Rect:
    with pymupdf.open(path) as doc:
        return doc[page_number - 1].rect


def load(path, doc_id: Optional[str] = None) -> Document:
    path = Path(path)
    doc_id = doc_id or path.stem
    suffix = path.suffix.lower()

    if suffix in IMAGE_SUFFIXES:
        return Document(doc_id, path, "image", [Page(1, image_png=_png(pymupdf.Pixmap(str(path))))],
                        reason="image file")
    if suffix not in PDF_SUFFIXES:
        raise ValueError(f"{path.name}: unsupported file type {suffix}")

    with pymupdf.open(path) as pdf:
        texts = [layout_text(page) for page in pdf]
        if texts and all(_usable(t) for t in texts):
            return Document(doc_id, path, "text",
                            [Page(i, text=t) for i, t in enumerate(texts, 1)], reason="text layer")
        bad = [i for i, t in enumerate(texts, 1) if not _usable(t)]
        pages = [Page(i, image_png=_png(page.get_pixmap(dpi=config.RENDER_DPI)))
                 for i, page in enumerate(pdf, 1)]
        return Document(doc_id, path, "image", pages,
                        reason=f"no usable text layer on page(s) {bad}" if texts else "empty PDF")


def load_dir(directory) -> list[Document]:
    """Every supported file under directory, recursively. doc_id is the file stem; must be unique."""
    paths = sorted(p for p in Path(directory).rglob("*")
                   if p.is_file() and p.suffix.lower() in PDF_SUFFIXES | IMAGE_SUFFIXES)
    seen = {}
    for p in paths:
        if p.stem in seen:
            raise ValueError(f"duplicate doc_id {p.stem!r}: {seen[p.stem]} and {p}")
        seen[p.stem] = p
    return [load(p) for p in paths]
