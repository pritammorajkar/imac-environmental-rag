"""Ingestion: PDF -> structured elements (text, tables, figures, footnotes) + ingestion report.

Run:  python -m src.ingest            (local only)
      python -m src.ingest --vision   (also caption raster images with the configured LLM)
"""

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from .config import load_settings
from .extract_figures import (
    blank_regions,
    describe_figure,
    describe_images_with_vision,
    extract_callout_figures,
    extract_image_figures,
    missing_graphic_placeholders,
)
from .extract_tables import extract_tables, table_row_sentences, table_to_markdown
from .footnotes import is_orphan_marker, marker_spans, parse_endnotes, sentence_with_marker
from .layout import Document, load_document, render_line

SCHEMA_VERSION = 1


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sections(doc: Document) -> dict[int, tuple[str, str | None]]:
    """Line order -> (section, subsection) from h1 and h2/h3 headings in reading order."""
    section, subsection = "Cover", None
    out = {}
    prev = None
    for line in doc.lines:
        continued = prev is not None and prev.kind == line.kind and prev.order + 1 == line.order
        if line.kind == "h1":
            section, subsection = (f"{section} {line.text}" if continued else line.text), None
        elif line.kind in {"h2", "h3"} and not line.claimed_by:
            subsection = f"{subsection} {line.text}" if continued and subsection else line.text
        out[line.order] = (section, subsection)
        prev = line
    return out


def _continues(prev, line, heading: bool) -> bool:
    if prev.page != line.page:
        return False
    if heading:
        return prev.order + 1 == line.order
    return prev.block == line.block


def _text_elements(doc: Document, sections, markers_for, max_note) -> tuple[list[dict], list[dict]]:
    """Paragraphs (one per PDF block) and headings from lines no other extractor claimed."""
    elements, orphans = [], []
    for line in doc.lines:
        if line.claimed_by or line.kind == "boilerplate":
            continue
        orphan = is_orphan_marker(line, max_note, doc.body_size)
        if orphan:
            orphans.append({"number": orphan, "page": line.page, "bbox": [round(v) for v in line.bbox]})
            continue
        heading = line.kind if line.kind.startswith("h") else None
        text = render_line(line, markers_for(line))
        refs = set(markers_for(line).values())
        prev = elements[-1] if elements else None
        if prev and prev["heading"] == heading and _continues(prev["_last"], line, bool(heading)):
            prev["text"] += " " + text
            prev["footnote_refs"] |= refs
            prev["_last"] = line
            continue
        section, subsection = sections[line.order]
        elements.append(
            {
                "id": f"text-{len(elements) + 1}",
                "type": "text",
                "pages": [line.page],
                "section": section,
                "subsection": None if heading == "h1" else subsection,
                "heading": heading,
                "order": line.order,
                "text": text,
                "footnote_refs": refs,
                "_last": line,
            }
        )
    for el in elements:
        el.pop("_last")
        el["footnote_refs"] = sorted(el["footnote_refs"])
    return elements, orphans


def ingest(pdf_path: Path, vision_llm=None, vision_cache: Path | None = None) -> tuple[list[dict], dict]:
    started = time.time()
    doc = load_document(pdf_path)
    warnings: list[str] = []
    max_note = len(parse_endnotes(doc, claim=False))

    def markers_for(line):
        return marker_spans(line, max_note, doc.body_size)

    tables, table_warnings = extract_tables(doc, markers_for)
    warnings += table_warnings
    figures = extract_callout_figures(doc, markers_for)
    regions = blank_regions(doc)
    figures += missing_graphic_placeholders(doc, regions, len(figures) + 1, markers_for)
    notes = parse_endnotes(doc)
    figures += extract_image_figures(doc, len(figures) + 1)
    sections = _sections(doc)
    texts, orphans = _text_elements(doc, sections, markers_for, max_note)

    vision_log = []
    if vision_llm is not None:
        vision_log = describe_images_with_vision(figures, doc, pdf_path, vision_llm, vision_cache)

    first_order = {}
    for line in doc.lines:
        if line.claimed_by and line.claimed_by not in first_order:
            first_order[line.claimed_by] = line.order

    elements: list[dict] = list(texts)
    for table in tables:
        order = first_order[table["id"]]
        section, subsection = sections[order]
        before = [t for t in texts if t["order"] < order and t["pages"] == table["pages"] and not t["heading"]]
        context = before[-1]["text"] if before and len(before[-1]["text"]) <= 400 else None
        elements.append(
            {
                **table,
                "type": "table",
                "section": section,
                "subsection": subsection,
                "order": order,
                "context": context,
                "text": table_to_markdown(table),
                "row_sentences": table_row_sentences(table),
            }
        )
    for fig in figures:
        order = first_order.get(fig["id"], min((l.order for l in doc.page(fig["pages"][0]).lines), default=0))
        section, subsection = sections.get(order, ("Cover", None))
        # Vision captions are unverified; only index them for images the model says carry data.
        indexable = fig["kind"] != "image" or bool(fig.get("vision", {}).get("data_bearing"))
        elements.append(
            {
                **fig,
                "type": "figure",
                "section": section,
                "subsection": subsection,
                "order": order,
                "indexable": indexable,
                "text": describe_figure(fig),
            }
        )

    by_note: dict[int, list[dict]] = {n["number"]: [] for n in notes}
    for el in elements:
        for number in el.get("footnote_refs", []):
            by_note.setdefault(number, []).append(
                {"element_id": el["id"], "page": el["pages"][0], "context": sentence_with_marker(el["text"], number)}
            )
    for orphan in orphans:
        by_note.setdefault(orphan["number"], []).append(
            {
                "element_id": None,
                "page": orphan["page"],
                "context": "Marker not attached to any text on this page (possibly a graphic missing from this PDF).",
            }
        )
    for note in notes:
        order = first_order[f"fn-{note['number']}"]
        elements.append(
            {
                "id": f"fn-{note['number']}",
                "type": "footnote",
                "number": note["number"],
                "pages": note["pages"],
                "section": "Endnotes",
                "subsection": None,
                "order": order,
                "text": note["text"],
                "referenced_by": by_note.get(note["number"], []),
                "related_elements": note["related_elements"],
                "footnote_refs": [],
            }
        )
    for table in (e for e in elements if e["type"] == "table"):
        table["related_footnotes"] = sorted(
            set(table["footnote_refs"]) | {n["number"] for n in notes if table["id"] in n["related_elements"]}
        )
    elements.sort(key=lambda e: e["order"])

    unreferenced = [n for n, refs in by_note.items() if not refs]
    if unreferenced:
        warnings.append(f"Endnotes never referenced by a marker: {unreferenced}")
    for orphan in orphans:
        warnings.append(f"p{orphan['page']}: footnote marker {orphan['number']} is not attached to any text")
    for region in regions:
        warnings.append(
            f"p{region['page']}: empty region y≈{region['y0']}–{region['y1']} pt (possible graphic missing from the PDF)"
        )
    for fig in figures:
        if fig["kind"] == "chart" and not fig["graphics_present"]:
            warnings.append(
                f"{fig['id']} (pages {fig['pages']}): chart has text labels but no bars/segments in the PDF; "
                "only printed values are available"
            )
        if fig["kind"] == "missing-graphic":
            warnings.append(f"{fig['id']} (p{fig['pages'][0]}): '{fig['title']}' has no graphics or values in the PDF")
    hidden = Counter(l.page for l in doc.lines if l.hidden)
    if hidden:
        warnings.append(
            f"White (invisible) text lines on pages {dict(hidden)}: table headers whose background band is "
            "missing; recovered from the text layer"
        )

    pages = []
    for page in doc.pages:
        on_page = [e for e in elements if page.number in e["pages"]]
        issues = []
        if page.text_chars < 100 and page.images:
            issues.append("little text but has images: needs OCR/vision")
        elif page.text_chars < 100:
            issues.append("very little text")
        pages.append(
            {
                "page": page.number,
                "printed_label": page.label,
                "text_chars": page.text_chars,
                "images": len(page.images),
                "vector_drawings": len(page.drawings),
                "elements": dict(Counter(e["type"] for e in on_page)),
                "issues": issues,
            }
        )
        warnings += [f"p{page.number}: {i}" for i in issues]

    figure_kinds = Counter(f["kind"] for f in figures)
    report = {
        "schema_version": SCHEMA_VERSION,
        "source": str(pdf_path),
        "source_sha256": file_sha256(pdf_path),
        "page_count": len(doc.pages),
        "body_font_size": doc.body_size,
        "seconds": round(time.time() - started, 2),
        "counts": {
            "text_blocks": sum(1 for t in texts if not t["heading"]),
            "headings": sum(1 for t in texts if t["heading"]),
            "tables": len(tables),
            "table_rows": sum(len(t["rows"]) for t in tables),
            "figures": dict(figure_kinds),
            "endnotes": len(notes),
            "footnote_markers_linked": sum(len(e.get("footnote_refs", [])) for e in elements),
            "orphan_markers": len(orphans),
        },
        "tables": [{"id": t["id"], "title": t["title"], "rows": len(t["rows"]), "columns": t["columns"]} for t in tables],
        "figures": [{"id": f["id"], "kind": f["kind"], "pages": f["pages"], "title": f.get("title")} for f in figures],
        "pages": pages,
        "warnings": warnings,
        "vision": vision_log or ("not run" if vision_llm is None else "cached results reused"),
        "failures": [],
    }
    return elements, report


def write_report_markdown(report: dict, path: Path) -> None:
    c = report["counts"]
    lines = [
        "# Ingestion report",
        "",
        f"- Source: `{report['source']}` (sha256 `{report['source_sha256'][:12]}…`)",
        f"- Pages: {report['page_count']}; body font size {report['body_font_size']} pt; {report['seconds']} s",
        f"- Text blocks: {c['text_blocks']}, headings: {c['headings']}",
        f"- Tables: {c['tables']} ({c['table_rows']} rows)",
        f"- Figures: {c['figures']}",
        f"- Endnotes: {c['endnotes']}; linked marker references: {c['footnote_markers_linked']}; orphan markers: {c['orphan_markers']}",
        f"- Vision: {report['vision']}",
        "",
        "## Tables",
        *[f"- {t['id']}: {t['title']} — {t['rows']} rows, columns {t['columns']}" for t in report["tables"]],
        "",
        "## Figures",
        *[f"- {f['id']}: {f['kind']} on pages {f['pages']}" + (f" — {f['title']}" if f["title"] else "") for f in report["figures"]],
        "",
        "## Pages",
        "| Page | Printed label | Chars | Images | Elements | Issues |",
        "|---|---|---|---|---|---|",
        *[
            f"| {p['page']} | {p['printed_label'] or ''} | {p['text_chars']} | {p['images']} | "
            f"{', '.join(f'{k}: {v}' for k, v in p['elements'].items())} | {'; '.join(p['issues'])} |"
            for p in report["pages"]
        ],
        "",
        "## Warnings",
        *[f"- {w}" for w in report["warnings"]],
        "",
        "## Failures",
        *([f"- {f}" for f in report["failures"]] or ["- none"]),
    ]
    path.write_text("\n".join(lines) + "\n")


def run(vision: bool = False, settings=None) -> dict:
    settings = settings or load_settings()
    if not settings.pdf_path.exists():
        raise FileNotFoundError(f"PDF not found at {settings.pdf_path}; set PDF_PATH or copy it to data/")
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    llm = None
    if vision:
        from .llm import get_llm

        llm = get_llm(settings)
        if llm is None:
            raise SystemExit("--vision needs LLM_PROVIDER and an API key (see .env.example)")
    elements, report = ingest(settings.pdf_path, llm, settings.vision_cache_path)
    settings.elements_path.write_text(json.dumps({"source_sha256": report["source_sha256"], "elements": elements}, indent=1, ensure_ascii=False))
    settings.report_json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    write_report_markdown(report, settings.report_md_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vision", action="store_true", help="caption raster images with the configured LLM (sends image crops to it)")
    args = parser.parse_args()
    report = run(args.vision)
    c = report["counts"]
    print(f"Pages {report['page_count']} | text blocks {c['text_blocks']} | tables {c['tables']} | "
          f"figures {c['figures']} | endnotes {c['endnotes']} | linked markers {c['footnote_markers_linked']}")
    print(f"{len(report['warnings'])} warnings; full report: artifacts/ingestion_report.md")


if __name__ == "__main__":
    main()
