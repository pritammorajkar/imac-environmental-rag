"""Reconstruct borderless tables from the text layer.

This PDF's tables have no ruling lines, and their header rows are white text whose dark
background band is missing, so generic table finders (PyMuPDF find_tables, Docling) do not
detect them. A table here is: a run of bold white header lines followed by data lines that
parse as rows. Two row layouts occur:

* key/value rows, one per line: 'Total product footprint 346 kg CO2e'
* multi-column rows flattened into running text under a column header line such as
  '100V 115V 230V': 'Off 0.27W 0.27W 0.29W Sleep 0.56W …'

Every cell value is parsed into number, unit and qualifier so numeric answers can be checked.
"""

import re

from .layout import Document, Line, render_line

UNIT = r"(?:kg\s?CO2e|kWh|kg|W|%|g)"
NUMBER = r"[–-]?\d[\d,]*(?:\.\d+)?"
VALUE = rf"[↓↑]?\s?[<>≤≥~]?\s?{NUMBER}\s?{UNIT}"
VALUE_RE = re.compile(rf"^(?P<dir>[↓↑])?\s?(?P<qual>[<>≤≥~])?\s?(?P<num>{NUMBER})\s?(?P<unit>{UNIT})$")
KEY_VALUE_RE = re.compile(rf"^(?P<label>.+?)\s+(?P<value>{VALUE})$")
COLUMN_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?\s?[A-Za-z%]+$")


def parse_value(raw: str) -> dict:
    raw = raw.strip()
    match = VALUE_RE.match(raw)
    if not match:
        return {"raw": raw}
    unit = re.sub(r"\s", "", match["unit"])
    value = {
        "raw": raw,
        "value": float(match["num"].replace(",", "").replace("–", "-")),
        "unit": "kg CO2e" if unit == "kgCO2e" else unit,
    }
    if match["qual"]:
        value["qualifier"] = match["qual"]
    if match["dir"]:
        value["direction"] = "decrease" if match["dir"] == "↓" else "increase"
    return value


def parse_flattened_rows(text: str, n_columns: int) -> tuple[list[dict], str]:
    """Split 'Off 0.27W 0.27W 0.29W Sleep 0.56W …' into rows of a label plus n values.

    Returns the rows and any text left unparsed (non-empty means the table is suspect).
    """
    values = r"\s+".join(f"(?P<v{i}>{VALUE})" for i in range(n_columns))
    pattern = re.compile(rf"(?P<label>[^\d↓↑<>]+?)\s+{values}(?=\s|$)")
    rows, leftover, pos = [], [], 0
    for match in pattern.finditer(text):
        leftover.append(text[pos : match.start()])
        rows.append(
            {
                "label": match["label"].strip(" ,"),
                "cells": [parse_value(match[f"v{i}"]) for i in range(n_columns)],
            }
        )
        pos = match.end()
    leftover.append(text[pos:])
    return rows, " ".join(part.strip() for part in leftover if part.strip())


def parse_key_value_row(text: str) -> dict | None:
    match = KEY_VALUE_RE.match(text.strip())
    if not match:
        return None
    label = match["label"].strip()
    row = {"label": label.lstrip("• ").strip(), "cells": [parse_value(match["value"])]}
    if label.startswith("•"):
        row["indent"] = 1
    return row


def _is_column_header(line: Line) -> bool:
    tokens = line.text.split()
    return len(tokens) >= 2 and all(COLUMN_TOKEN_RE.match(t) for t in tokens)


def extract_tables(doc: Document, markers_for) -> tuple[list[dict], list[str]]:
    """Find tables, claim their lines, and return (tables, warnings).

    `markers_for(line)` returns the footnote-marker spans of a line, so markers inside table
    cells (e.g. 'GHG reductions achieved⁷') are kept as [^n] references.
    """
    tables, warnings = [], []
    lines = [l for l in doc.lines if l.kind != "boilerplate"]
    i = 0
    while i < len(lines):
        if not (lines[i].hidden and lines[i].bold and not lines[i].claimed_by):
            i += 1
            continue
        header = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].hidden and lines[i].page == header[0].page:
            header.append(lines[i])
            i += 1
        column_line = next((l for l in header if _is_column_header(l)), None)
        data: list[Line] = []
        notes: list[Line] = []
        while i < len(lines):
            line = lines[i]
            text = render_line(line, markers_for(line))
            if line.hidden or line.claimed_by or line.page != header[0].page or line.kind.startswith("h"):
                break
            if text.lower().startswith("note:"):
                notes.append(line)
            elif (column_line and re.search(VALUE, text)) or (not column_line and parse_key_value_row(text)):
                data.append(line)
            else:
                break
            i += 1
        table = _build_table(len(tables) + 1, header, column_line, data, notes, markers_for, warnings)
        if table:
            tables.append(table)
            for line in header + data + notes:
                line.claimed_by = table["id"]
    return tables, warnings


def _build_table(number, header, column_line, data, notes, markers_for, warnings) -> dict | None:
    page = header[0].page
    table_id = f"table-{number}"
    if not data:
        warnings.append(f"p{page}: hidden header {' '.join(l.text for l in header)!r} has no parsable rows")
        return None
    rendered = [render_line(l, markers_for(l)) for l in data]
    if column_line:
        col_names = column_line.text.split()
        others = [l.text for l in header if l is not column_line]
        title = others[0] if others else "Table"
        row_header = next((t for t in others[1:] if len(t.split()) <= 2), "Item")
        rows, leftover = parse_flattened_rows(" ".join(rendered), len(col_names))
        columns = [row_header] + col_names
        if leftover:
            warnings.append(f"p{page} {table_id}: unparsed table text {leftover!r}")
    else:
        spans = [s.text.strip() for l in header for s in l.spans if s.text.strip()]
        columns = [spans[0], " ".join(spans[1:])] if len(spans) > 1 else [spans[0], "Value"]
        title = " — ".join(columns)
        rows = [parse_key_value_row(t) for t in rendered]
        rows = [r for r in rows if r]
        for row in rows:
            if row.get("indent"):
                parent = next((r for r in reversed(rows[: rows.index(row)]) if not r.get("indent")), None)
                if parent:
                    row["parent"] = parent["label"]
    width = len(columns) - 1
    for row in rows:
        if len(row["cells"]) != width or any("value" not in c for c in row["cells"]):
            warnings.append(f"p{page} {table_id}: row {row['label']!r} has unparsed or missing cells")
    refs = sorted({n for l in header + data + notes for n in markers_for(l).values()})
    return {
        "id": table_id,
        "pages": [page],
        "title": title,
        "columns": columns,
        "rows": rows,
        "notes": [render_line(l, markers_for(l)) for l in notes],
        "footnote_refs": refs,
        "hidden_header": True,
        "bbox": _union([l.bbox for l in header + data + notes]),
    }


def _union(boxes):
    return [round(min(b[0] for b in boxes), 1), round(min(b[1] for b in boxes), 1),
            round(max(b[2] for b in boxes), 1), round(max(b[3] for b in boxes), 1)]


def table_to_markdown(table: dict) -> str:
    def cell(c):
        return c["raw"]

    head = "| " + " | ".join(table["columns"]) + " |"
    sep = "|" + "---|" * len(table["columns"])
    body = []
    for row in table["rows"]:
        label = ("  ↳ " if row.get("indent") else "") + row["label"]
        body.append("| " + " | ".join([label] + [cell(c) for c in row["cells"]]) + " |")
    out = [f"Table: {table['title']}", head, sep, *body]
    out += table["notes"]
    return "\n".join(out)


def table_row_sentences(table: dict) -> list[str]:
    """One self-contained sentence per row, header included, for exact keyword matching."""
    sentences = []
    for row in table["rows"]:
        label = row["label"] + (f" (part of {row['parent']})" if row.get("parent") else "")
        cells = "; ".join(f"{col}: {c['raw']}" for col, c in zip(table["columns"][1:], row["cells"]))
        sentences.append(f"{table['title']} — {table['columns'][0]} = {label}; {cells}")
    return sentences
