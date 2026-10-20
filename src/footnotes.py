"""Endnote parsing and linking footnote markers in the body to the notes they point to."""

import re

from .layout import MARKER_RE, Document, Line, render_line

ENDNOTE_HEADINGS = {"endnotes", "notes", "footnotes"}


def _note_start(line: Line, expected: int) -> bool:
    """A note starts with a small number span equal to the next expected note number."""
    spans = [s for s in line.spans if s.text.strip()]
    if len(spans) < 2 or spans[0].text.strip() != str(expected):
        return False
    return spans[0].size <= 0.85 * max(s.size for s in spans[1:])


def parse_endnotes(doc: Document, claim: bool = True) -> list[dict]:
    """Parse numbered endnotes that follow an 'Endnotes' heading.

    Notes are matched strictly in sequence (1, 2, 3, …), so numbers inside note text such as
    'ISO 14040' or '1) No use of…' are never mistaken for new notes. A note continues while
    lines keep the note's font size; lines already claimed by a table are linked to the note
    they sit inside rather than merged into its text.
    """
    lines = doc.lines
    start = next(
        (i for i, l in enumerate(lines) if l.kind == "h1" and l.text.lower() in ENDNOTE_HEADINGS),
        None,
    )
    if start is None:
        return []

    notes: list[dict] = []
    current: dict | None = None
    for line in lines[start + 1 :]:
        if line.kind == "boilerplate" or (line.kind == "h1" and line.text.lower() in ENDNOTE_HEADINGS):
            continue
        if line.claimed_by:
            if current:
                current["related_elements"].append(line.claimed_by)
            continue
        if _note_start(line, len(notes) + 1):
            current = {
                "number": len(notes) + 1,
                "lines": [line],
                "size": line.size,
                "related_elements": [],
            }
            notes.append(current)
        elif current and abs(line.size - current["size"]) <= 0.6:
            current["lines"].append(line)
        elif current:
            break

    parsed = []
    for note in notes:
        first = note["lines"][0]
        body = [render_line(first)[len(str(note["number"])) :]] + [render_line(l) for l in note["lines"][1:]]
        if claim:
            for line in note["lines"]:
                line.claimed_by = f"fn-{note['number']}"
        parsed.append(
            {
                "number": note["number"],
                "text": re.sub(r"\s+", " ", " ".join(body)).strip(),
                "pages": sorted({l.page for l in note["lines"]}),
                "related_elements": sorted(set(note["related_elements"])),
            }
        )
    return parsed


def marker_spans(line: Line, max_note: int, body_size: float) -> dict[int, int]:
    """Span index -> note number for footnote markers in a body line.

    A marker is a 1–2 digit span within the endnote range that is either flagged superscript or
    clearly smaller than the surrounding text. The '2' in 'CO2' is excluded.
    """
    found = {}
    others = [s.size for s in line.spans if s.text.strip() and not MARKER_RE.match(s.text.strip())]
    ref_size = max(others) if others else body_size
    for i, span in enumerate(line.spans):
        text = span.text.strip()
        if not MARKER_RE.match(text) or not 1 <= int(text) <= max_note:
            continue
        if not (span.superscript or span.size <= 0.8 * ref_size):
            continue
        if i > 0 and line.spans[i - 1].text.rstrip().endswith("CO"):
            continue
        found[i] = int(text)
    return found


def is_orphan_marker(line: Line, max_note: int, body_size: float) -> int | None:
    """A line holding only a small note number, not attached to any text (e.g. a lost graphic)."""
    text = line.text
    if MARKER_RE.match(text) and 1 <= int(text) <= max_note and line.size <= 0.8 * body_size:
        return int(text)
    return None


def sentence_with_marker(text: str, number: int, width: int = 200) -> str:
    """The sentence (or list item) around the first [^n] marker in text, at most ~width chars."""
    pos = text.find(f"[^{number}]")
    if pos < 0:
        return ""
    bounds = [m.end() for m in re.finditer(r"[.!?;](?=\s)|\n", text[:pos])]
    start = max(bounds[-1] if bounds else 0, pos - width)
    after = re.search(r"[.!?;](?=\s|$)|\n", text[pos:])
    end = min(pos + (after.end() if after else len(text) - pos), pos + width)
    return text[start:end].strip()
