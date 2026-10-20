"""Figures: charts and infographics built from numeric callouts, raster images, and gaps where a
graphic appears to be missing.

Chart values are only ever taken from the PDF text layer ("text_layer" provenance). When the
optional vision step runs, its output is stored separately with "vision_model" provenance and
every number it reports is checked against the page's text layer.
"""

import hashlib
import json
import re
from collections import Counter

import pymupdf

from .extract_tables import parse_value
from .layout import Document, Line, render_line

CALLOUT_RE = re.compile(r"^[–-]?\d[\d,.]*\s?(?:kg|g|%|kWh|W)$")
DANGLING = {"and", "or", "of", "for", "the", "to", "in", "with"}
MIN_GAP = 150  # points of vertical empty space reported as a possible missing graphic


def _is_callout(line: Line, body: float) -> bool:
    return line.bold and line.size >= 1.2 * body and bool(CALLOUT_RE.match(line.text))


def _is_label(line: Line, body: float, block_width: dict) -> bool:
    """Short text in a block of only short lines (so never the tail line of a paragraph)."""
    return (
        line.kind in {"body", "h3"}
        and not line.claimed_by
        and not line.hidden
        and block_width[(line.page, line.block)] <= 45
        and line.size < 1.2 * body
    )


def _runs(doc: Document) -> list[list[Line]]:
    """Contiguous runs (stream order) of callouts and short labels that contain a callout.

    A run that ends a page and one that starts the next page are merged, because a chart can be
    split across a page break (as the carbon chart on pages 2–3 is).
    """
    body = doc.body_size
    runs, current = [], []
    lines = [l for l in doc.lines if l.kind != "boilerplate"]
    block_width: dict = {}
    for l in lines:
        key = (l.page, l.block)
        block_width[key] = max(block_width.get(key, 0), len(l.text))
    for line in lines:
        if _is_callout(line, body) or _is_label(line, body, block_width):
            current.append(line)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    runs = [r for r in runs if any(_is_callout(l, body) for l in r)]

    def first_on(page):
        return next(l for l in lines if l.page == page)

    def last_on(page):
        return [l for l in lines if l.page == page][-1]

    merged = []
    for run in runs:
        if merged and run[0].page == merged[-1][-1].page + 1 and merged[-1][-1] is last_on(merged[-1][-1].page) and run[0] is first_on(run[0].page):
            merged[-1].extend(run)
        else:
            merged.append(run)
    return merged


def _caption(run: list[Line], start: int, body: float) -> list[Line]:
    """Lines directly under a callout in the same column."""
    callout = run[start]
    caption, prev = [], callout
    for line in run[start + 1 :]:
        if _is_callout(line, body) or line.page != prev.page or abs(line.bbox[0] - callout.bbox[0]) > 20:
            break
        gap = line.bbox[1] - prev.bbox[3]
        limit = 1.2 * (callout.bbox[3] - callout.bbox[1]) if prev is callout else 0.6 * (line.bbox[3] - line.bbox[1])
        if gap > limit:
            break
        caption.append(line)
        prev = line
    return caption


def _labels(lines: list[Line], markers_for) -> list[str]:
    """Merge label lines of the same PDF block, rejoin labels split after a dangling word."""
    groups: list[list[Line]] = []
    for line in lines:
        prev = groups[-1][-1] if groups else None
        if prev and prev.page == line.page and (
            prev.block == line.block
            or (0 <= line.bbox[1] - prev.bbox[3] < 0.6 * (line.bbox[3] - line.bbox[1])
                and line.bbox[0] < prev.bbox[2] and prev.bbox[0] < line.bbox[2])
        ):
            groups[-1].append(line)
        else:
            groups.append([line])
    labels = [" ".join(render_line(l, markers_for(l)) for l in g) for g in groups]
    joined: list[str] = []
    for label in labels:
        if joined and joined[-1].split()[-1].lower() in DANGLING:
            joined[-1] = f"{joined[-1]} {label}"
        else:
            joined.append(label)
    out = []
    for label in joined:
        words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label).split()
        out.extend(words if len(set(words)) == 1 and len(words) > 1 else [" ".join(words)])
    return out


def _preceding_heading(doc: Document, line: Line) -> str | None:
    heading = None
    for l in doc.lines:
        if l.order >= line.order:
            break
        if l.kind in {"h1", "h2"} and not CALLOUT_RE.match(l.text):
            heading = l.text
    return heading


def _consistency_checks(values: list[dict]) -> list[str]:
    """Report any callout that equals the sum of two others with the same unit (waterfall check)."""
    checks = []
    nums = [v for v in values if "value" in v]
    for a in nums:
        for b in nums:
            for c in nums:
                if len({id(a), id(b), id(c)}) == 3 and a["unit"] == b["unit"] == c["unit"] and b["value"] < 0 < a["value"]:
                    if abs(a["value"] + b["value"] - c["value"]) < 1e-6:
                        checks.append(f"{a['raw']} + ({b['raw']}) = {c['raw']} (consistent)")
    return checks


def extract_callout_figures(doc: Document, markers_for) -> list[dict]:
    figures = []
    body = doc.body_size
    for run in _runs(doc):
        callouts, caption_lines = [], set()
        for i, line in enumerate(run):
            if _is_callout(line, body):
                caption = _caption(run, i, body)
                caption_lines.update(id(l) for l in caption)
                callouts.append(
                    {
                        **parse_value(line.text),
                        "caption": " ".join(render_line(l, markers_for(l)) for l in caption),
                        "page": line.page,
                        "source": "text_layer",
                    }
                )
        label_lines = [l for l in run if not _is_callout(l, body) and id(l) not in caption_lines]
        labels = _labels(label_lines, markers_for)
        counts = Counter(re.sub(r"\[\^\d+\]", "", l).strip().lower() for l in labels)
        kind = "chart" if any(c > 1 for c in counts.values()) else "infographic"
        pages = sorted({l.page for l in run})
        graphics = any(doc.page(p).drawings or doc.page(p).images for p in pages)
        unique_labels = list(dict.fromkeys(labels))
        figure = {
            "id": f"figure-{len(figures) + 1}",
            "kind": kind,
            "pages": pages,
            "title": _preceding_heading(doc, run[0]),
            "values": callouts,
            "labels": unique_labels if kind == "chart" else [],
            "checks": _consistency_checks(callouts),
            "graphics_present": graphics,
            "footnote_refs": sorted({n for l in run for n in markers_for(l).values()}),
        }
        figures.append(figure)
        for line in run:
            line.claimed_by = figure["id"]
    return figures


def describe_figure(figure: dict) -> str:
    """Searchable text for a figure; states clearly what was and was not extracted."""
    pages = "–".join(str(p) for p in (figure["pages"][0], figure["pages"][-1])) if len(figure["pages"]) > 1 else str(figure["pages"][0])
    title = f" under '{figure['title']}'" if figure.get("title") else ""
    out = [f"{figure['kind'].capitalize()} (page{'s' if len(figure['pages']) > 1 else ''} {pages}){title}."]
    if figure["kind"] == "missing-graphic":
        out.append(figure["note"])
        if figure.get("context"):
            out.append(f"Text near the missing graphic: {figure['context']}")
        return "\n".join(out)
    if figure["kind"] == "image":
        if figure.get("nearest_text"):
            out.append(f"Nearest text on the page: {figure['nearest_text']}")
        if figure.get("vision"):
            out.append(f"Vision-model description (unverified, not from the text layer): {figure['vision']['description']}")
        return "\n".join(out)
    if figure["values"]:
        out.append("Values printed on the figure (extracted from the PDF text layer):")
        out += [f"- {v['raw']} — {v['caption'] or '(no caption)'}" for v in figure["values"]]
    if figure["labels"]:
        out.append("Category labels printed on the chart: " + "; ".join(figure["labels"]) + ".")
    out += [f"Arithmetic check: {c}." for c in figure["checks"]]
    if not figure["graphics_present"]:
        out.append(
            "Extraction note: the chart's bars/segments are not present in this PDF, only its text "
            "labels; segment sizes cannot be read and no values were estimated."
            if figure["kind"] == "chart"
            else "Extraction note: values are taken from the text layer; no graphics are present."
        )
    return "\n".join(out)


def extract_image_figures(doc: Document, start: int) -> list[dict]:
    figures = []
    for page in doc.pages:
        for image in page.images:
            box = image["bbox"]
            nearest = min(
                (l for l in page.lines if l.kind != "boilerplate" and not l.hidden),
                key=lambda l: _box_distance(box, l.bbox),
                default=None,
            )
            figures.append(
                {
                    "id": f"figure-{start + len(figures)}",
                    "kind": "image",
                    "pages": [page.number],
                    "bbox": list(box),
                    "pixels": [image["width"], image["height"]],
                    "xref": image["xref"],
                    "nearest_text": nearest.text if nearest and _box_distance(box, nearest.bbox) < 60 else None,
                    "graphics_present": True,
                    "values": [],
                    "labels": [],
                    "checks": [],
                    "footnote_refs": [],
                }
            )
    return figures


def _box_distance(a, b) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0)
    dy = max(b[1] - a[3], a[1] - b[3], 0)
    return (dx * dx + dy * dy) ** 0.5


def blank_regions(doc: Document) -> list[dict]:
    """Vertical bands with no visible text, image or drawing, larger than MIN_GAP points."""
    regions = []
    for page in doc.pages:
        boxes = [l.bbox for l in page.lines if not l.hidden] + [i["bbox"] for i in page.images] + page.drawings
        if not boxes:
            continue
        boxes.sort(key=lambda b: b[1])
        covered_to = boxes[0][3]
        for box in boxes[1:]:
            if box[1] - covered_to > MIN_GAP:
                regions.append({"page": page.number, "y0": round(covered_to), "y1": round(box[1])})
            covered_to = max(covered_to, box[3])
    return regions


def missing_graphic_placeholders(doc: Document, regions: list[dict], start: int, markers_for) -> list[dict]:
    """A bold standalone title (h3) on a page that also has a large empty band: likely a chart
    whose graphics did not survive in this PDF. Recorded so questions about it can be answered
    with an explicit 'not present in the document' rather than a guess."""
    placeholders = []
    for region in regions:
        titles = [
            l for l in doc.page(region["page"]).lines
            if l.kind == "h3" and not l.claimed_by and l.bbox[3] <= region["y0"]
        ]
        for title in titles:
            following = [l for l in doc.lines if l.order > title.order and l.page == title.page and l.kind == "body"]
            context = " ".join(render_line(l, markers_for(l)) for l in following[:4])
            placeholders.append(
                {
                    "id": f"figure-{start + len(placeholders)}",
                    "kind": "missing-graphic",
                    "pages": [title.page],
                    "title": title.text,
                    "note": (
                        f"A figure titled '{title.text}' appears to be missing: page {title.page} has an "
                        f"empty region (y≈{region['y0']}–{region['y1']} pt) and no chart graphics or "
                        "chart values exist in this PDF. Its data cannot be recovered from this document."
                    ),
                    "context": context[:300],
                    "graphics_present": False,
                    "values": [],
                    "labels": [],
                    "checks": [],
                    "footnote_refs": sorted({n for l in following[:4] for n in markers_for(l).values()}),
                }
            )
            title.claimed_by = placeholders[-1]["id"]
    return placeholders


VISION_PROMPT = (
    "You are describing one image cropped from a product environmental report. Return JSON with "
    "keys: description (one or two factual sentences about what is shown), visible_text (text "
    "you can read in the image, or empty), numbers (list of numeric values printed in the image, "
    "as strings, empty if none), data_bearing (true only if it is a chart, table or diagram that "
    "conveys data). Do not guess values that are not legible."
)


def describe_images_with_vision(figures: list[dict], doc: Document, pdf_path, llm, cache_path) -> list[str]:
    """Optional: caption raster images with a vision model, cached by image hash.

    Only images are sent (charts here have no graphics to look at). Numbers the model reports are
    marked verified only if they also appear in the page's text layer.
    """
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    log = []
    with pymupdf.open(pdf_path) as pdf:
        for fig in figures:
            if fig["kind"] != "image":
                continue
            page = pdf[fig["pages"][0] - 1]
            png = page.get_pixmap(clip=pymupdf.Rect(fig["bbox"]), dpi=144).tobytes("png")
            key = hashlib.sha256(png + VISION_PROMPT.encode()).hexdigest()
            if key not in cache:
                cache[key] = llm.describe_image(png, VISION_PROMPT)
                log.append(f"{fig['id']}: described by {llm.name}")
            result = cache[key]
            page_text = " ".join(l.text for l in doc.page(fig["pages"][0]).lines)
            result["numbers_verified"] = [n for n in result.get("numbers", []) if n in page_text]
            result["numbers_unverified"] = [n for n in result.get("numbers", []) if n not in page_text]
            fig["vision"] = result
    cache_path.write_text(json.dumps(cache, indent=2))
    return log
