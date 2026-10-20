"""Load a PDF into a small span/line model with page provenance, using PyMuPDF.

Every later extractor (tables, figures, footnotes, text) works on these Line objects, and each
line is claimed by at most one extracted element, so the same content is never indexed twice.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pymupdf

SUPERSCRIPT_FLAG = 1
BOLD_FLAG = 16
WHITE = 0xFFFFFF
MARKER_RE = re.compile(r"^\d{1,2}$")


@dataclass
class Span:
    text: str
    size: float
    bold: bool
    superscript: bool
    color: int
    bbox: tuple[float, float, float, float]


@dataclass
class Line:
    page: int
    order: int
    block: int
    spans: list[Span]
    bbox: tuple[float, float, float, float]
    kind: str = "body"  # body | h1 | h2 | h3 | boilerplate
    claimed_by: str | None = None

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(s.text for s in self.spans)).strip()

    @property
    def size(self) -> float:
        weights = Counter()
        for s in self.spans:
            weights[round(s.size, 1)] += len(s.text.strip())
        return weights.most_common(1)[0][0] if weights else 0.0

    @property
    def bold(self) -> bool:
        spans = [s for s in self.spans if s.text.strip() and not MARKER_RE.match(s.text.strip())]
        return bool(spans) and all(s.bold for s in spans)

    @property
    def hidden(self) -> bool:
        spans = [s for s in self.spans if s.text.strip()]
        return bool(spans) and all(s.color == WHITE for s in spans)


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list[Line] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    drawings: list[tuple[float, float, float, float]] = field(default_factory=list)
    label: str | None = None

    @property
    def text_chars(self) -> int:
        return sum(len(line.text) for line in self.lines)


@dataclass
class Document:
    path: str
    pages: list[Page]
    body_size: float

    @property
    def lines(self) -> list[Line]:
        return [line for page in self.pages for line in page.lines]

    def page(self, number: int) -> Page:
        return self.pages[number - 1]


def _split_heading_prefix(spans: list[Span]) -> int:
    """Index where a line switches from a large heading font to much smaller body text, else 0.

    Handles lines such as 'Endnotes 1 Product recycled…' or 'Definitions Bio-based plastics: …'.
    """
    for i in range(1, len(spans)):
        head = [s for s in spans[:i] if s.text.strip()]
        rest = [s for s in spans[i:] if s.text.strip() and not MARKER_RE.match(s.text.strip())]
        if head and rest and min(s.size for s in head) >= 1.8 * max(s.size for s in rest):
            return i
    return 0


def _bbox(spans: list[Span]) -> tuple[float, float, float, float]:
    return (
        min(s.bbox[0] for s in spans),
        min(s.bbox[1] for s in spans),
        max(s.bbox[2] for s in spans),
        max(s.bbox[3] for s in spans),
    )


def load_document(pdf_path) -> Document:
    pages: list[Page] = []
    order = 0
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    with pymupdf.open(pdf_path) as pdf:
        for pno, pdf_page in enumerate(pdf, start=1):
            page = Page(pno, pdf_page.rect.width, pdf_page.rect.height)
            for bno, block in enumerate(pdf_page.get_text("dict", flags=flags)["blocks"]):
                for raw in block.get("lines", []):
                    spans = [
                        Span(
                            s["text"],
                            s["size"],
                            bool(s["flags"] & BOLD_FLAG),
                            bool(s["flags"] & SUPERSCRIPT_FLAG),
                            s["color"],
                            tuple(s["bbox"]),
                        )
                        for s in raw["spans"]
                        if s["text"]
                    ]
                    if not "".join(s.text for s in spans).strip():
                        continue
                    cut = _split_heading_prefix(spans)
                    for part in (spans[:cut], spans[cut:]) if cut else (spans,):
                        page.lines.append(Line(pno, order, bno, part, _bbox(part)))
                        order += 1
            for info in pdf_page.get_image_info(xrefs=True):
                page.images.append(
                    {
                        "xref": info["xref"],
                        "bbox": tuple(round(v, 1) for v in info["bbox"]),
                        "width": info["width"],
                        "height": info["height"],
                    }
                )
            page_area = pdf_page.rect.get_area()
            for drawing in pdf_page.get_drawings():
                if drawing["rect"].get_area() < 0.9 * page_area:
                    page.drawings.append(tuple(round(v, 1) for v in drawing["rect"]))
            pages.append(page)

    sizes = Counter()
    for page in pages:
        for line in page.lines:
            sizes[line.size] += len(line.text)
    doc = Document(str(pdf_path), pages, sizes.most_common(1)[0][0])
    _mark_boilerplate(doc)
    _mark_headings(doc)
    return doc


def _mark_boilerplate(doc: Document) -> None:
    """Running headers/footers: the same line (digits ignored) on three or more pages."""
    pages_by_text = defaultdict(set)
    for line in doc.lines:
        pages_by_text[re.sub(r"\d+", "#", line.text)].add(line.page)
    for line in doc.lines:
        key = re.sub(r"\d+", "#", line.text)
        if len(pages_by_text[key]) >= 3 and len(line.text) > 8:
            line.kind = "boilerplate"
            number = re.search(r"(\d+)\s*$", line.text)
            page = doc.page(line.page)
            if number and page.label is None:
                page.label = number.group(1)


def _mark_headings(doc: Document) -> None:
    body = doc.body_size
    for line in doc.lines:
        if line.kind != "body" or line.hidden:
            continue
        text = line.text
        if line.size >= 2.2 * body:
            line.kind = "h1"
        elif line.bold and line.size >= 1.25 * body and len(text) <= 80:
            line.kind = "h2"
        elif line.bold and len(text) <= 80 and not text.endswith(".") and len(text.split()) >= 2:
            line.kind = "h3"


def heading_runs(lines: list[Line]) -> list[tuple[str, list[Line]]]:
    """Merge consecutive heading lines of the same level (multi-line headings)."""
    runs: list[tuple[str, list[Line]]] = []
    for line in lines:
        if runs and line.kind == runs[-1][0] and line.kind.startswith("h"):
            runs[-1][1].append(line)
        else:
            runs.append((line.kind, [line]))
    return runs


def render_line(line: Line, markers: dict[int, int] | None = None) -> str:
    """Line text with footnote-marker spans (span index -> note number) rendered as [^n]."""
    markers = markers or {}
    out = []
    for i, span in enumerate(line.spans):
        if i in markers:
            out.append(f"[^{markers[i]}]" + (" " if span.text.endswith(" ") else ""))
        else:
            out.append(span.text)
    return re.sub(r"\s+", " ", "".join(out)).strip()
