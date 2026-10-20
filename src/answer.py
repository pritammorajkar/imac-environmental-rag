"""Question answering over retrieved evidence, with citation, number and calculation checks.

Two modes:
* LLM (LLM_PROVIDER=gemini|openai): the model writes the answer from numbered evidence and
  cites evidence IDs; pages always come from index metadata, never from the model.
* Extractive (default, no API): exact table-cell lookup or the best-matching evidence sentences,
  with abstention when retrieval signals are weak.

Run:  python -m src.answer "What is the sleep power at 115V?"  [--extractive] [--json]
"""

import argparse
import ast
import json
import operator
import re
import time
from dataclasses import asdict, dataclass, field

from .config import Settings, load_settings
from .retrieve import Evidence, RetrievalResult, Retriever, content_terms, stem, tokenize

SYSTEM_PROMPT = """You answer questions about Apple's iMac Product Environmental Report (iMac introduced October 2024) using ONLY the numbered evidence excerpts provided.

Rules:
1. Use only facts stated in the evidence. Never use outside knowledge about Apple, iMac or other products.
2. If the evidence does not answer the question, set status to "insufficient_evidence" and say briefly what the report does not contain. Do not guess. If only part of the question is supported, answer that part and say which part is not in the report.
3. After each claim cite the evidence that directly supports it, e.g. [E2]. Cite only evidence IDs that appear below.
4. Copy numbers exactly as written in the evidence, with their units (e.g. "0.57W", "346 kg CO2e", "<1%"). If you calculate anything, show the arithmetic in the answer and list it in "calculations".
5. Markers like [^7] point to endnote 7. When an endnote qualifies a claim (exclusions, scope, which configuration or model it applies to), state the qualification.
6. For charts, use only values printed on the chart. If the evidence says a chart's graphics or values are missing from the PDF, say the value is not available in this document instead of estimating it.
7. If evidence excerpts conflict, report both and cite each.
8. Keep the answer concise: one to four sentences unless a list is needed.

Return a JSON object: {"status": "answered" | "insufficient_evidence", "answer": "...", "citations": ["E1", ...], "calculations": [{"expression": "391 - 346", "result": "45", "unit": "kg CO2e"}]}"""

NUMBER_RE = re.compile(r"(?<![\w.])[–-]?\d+(?:,\d{3})*(?:\.\d+)?")
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.USub: operator.neg}


@dataclass
class Answer:
    question: str
    status: str
    answer: str
    mode: str
    citations: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def cited_pages(self) -> list[int]:
        return sorted({p for c in self.citations for p in c["pages"]})

    @property
    def content_types_used(self) -> list[str]:
        return sorted({c["content_type"] for c in self.citations})

    @property
    def warnings(self) -> list[str]:
        out = []
        if self.checks.get("invalid_citations"):
            out.append(f"Dropped citations to unknown evidence: {self.checks['invalid_citations']}")
        if self.status == "answered" and not self.citations:
            out.append("The answer cites no evidence; treat it as unverified.")
        if self.checks.get("unsupported_numbers"):
            out.append(f"Numbers not found in the evidence or a verified calculation: {self.checks['unsupported_numbers']}")
        for calc in self.checks.get("calculations", []):
            if calc.get("ok") is False:
                out.append(f"Calculation {calc['expression']} = {calc['claimed']} is wrong; recomputed {calc['computed']}")
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(cited_pages=self.cited_pages, content_types_used=self.content_types_used, warnings=self.warnings)
        return data


def _evidence_rows(result: RetrievalResult) -> list[dict]:
    rows = []
    for n, ev in enumerate(result.evidence, start=1):
        item = ev.item
        rows.append(
            {
                "label": f"E{n}",
                "item_id": item["id"],
                "content_type": item["content_type"],
                "figure_kind": item.get("figure_kind"),
                "pages": item["pages"],
                "section": item["section"],
                "text": item["text"],
                "rank": ev.rank,
                "score": round(ev.score, 5),
                "dense": None if ev.dense is None else round(ev.dense, 4),
                "bm25": None if ev.bm25 is None else round(ev.bm25, 3),
                "reason": ev.reason,
                "attached_to": ev.attached_to,
            }
        )
    return rows


def format_evidence(rows: list[dict]) -> str:
    blocks = []
    for row in rows:
        pages = ", ".join(map(str, row["pages"]))
        kind = row["content_type"] + (f" ({row['figure_kind']})" if row["figure_kind"] else "")
        note = f" | attached: {row['reason']}" if row["attached_to"] else ""
        blocks.append(f"[{row['label']}] {kind} | page {pages} | section: {row['section']}{note}\n{row['text']}")
    return "\n\n".join(blocks)


def _numbers(text: str) -> set[float]:
    text = re.sub(r"\[(?:E\d+|\^\d+)\]", " ", text)
    out = set()
    for raw in NUMBER_RE.findall(text):
        try:
            out.add(round(float(raw.replace(",", "").replace("–", "-")), 6))
        except ValueError:
            pass
    return out


def _safe_eval(expression: str) -> float | None:
    expr = expression.replace("×", "*").replace("÷", "/").replace("–", "-").replace("−", "-")
    expr = re.sub(r"(?<=\d),(?=\d{3})", "", expr)
    expr = re.sub(r"[^\d.+\-*/() ]", " ", expr)

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")

    try:
        return ev(ast.parse(expr.strip(), mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError):
        return None


def verify_calculations(calculations: list[dict]) -> list[dict]:
    checked = []
    for calc in calculations or []:
        expression = str(calc.get("expression", ""))
        claimed_raw = str(calc.get("result", ""))
        computed = _safe_eval(expression)
        claimed = next(iter(_numbers(claimed_raw)), None)
        ok = None
        if computed is not None and claimed is not None:
            ok = abs(computed - claimed) <= max(0.01, 0.005 * abs(computed))
        checked.append(
            {"expression": expression, "claimed": claimed_raw, "computed": None if computed is None else round(computed, 4), "ok": ok}
        )
    return checked


def unsupported_numbers(answer: str, evidence_text: str, question: str, calculations: list[dict]) -> list[str]:
    allowed = _numbers(evidence_text) | _numbers(question)
    for calc in calculations:
        if calc["ok"]:
            allowed |= _numbers(calc["claimed"]) | _numbers(calc["expression"])
    allowed |= {abs(n) for n in allowed}
    return sorted({f"{n:g}" for n in _numbers(answer) if n not in allowed and abs(n) not in allowed})


def _insufficient(question, mode, result, rows, reason, started) -> Answer:
    return Answer(
        question=question,
        status="insufficient_evidence",
        answer=f"The report does not appear to contain enough information to answer this. {reason}".strip(),
        mode=mode,
        evidence=rows,
        signals=result.signals,
        seconds=round(time.time() - started, 2),
    )


def _weak_retrieval(signals: dict, settings: Settings) -> str | None:
    """Abstention rule for extractive mode (thresholds calibrated on the provisional question set)."""
    if signals["absent_terms"] and signals["absent_ratio"] >= settings.max_absent_ratio:
        return f"These terms do not occur anywhere in the report: {', '.join(signals['absent_terms'])}."
    if signals["max_dense"] < settings.min_dense_similarity:
        return f"No passage is semantically close to the question (best similarity {signals['max_dense']:.2f})."
    if signals["term_coverage"] < 0.5:
        return f"The best passages do not mention: {', '.join(signals['missing_terms'])}."
    return None


CALC_RE = re.compile(r"\bhow much (higher|lower|more|less)\b|\bdifference\b|\bby what percentage\b|\bapproximately how many\b|\brange\b|\bexpressed in\b|\bcompare", re.I)
DESCRIPTION_LINE = re.compile(r"^(Chart|Infographic|Missing-graphic|Image) \(page|^Values printed|^Extraction note|^Arithmetic check|^Category labels|^Context:|^Table:|^\|")


def table_lookups(question: str, evidence: list[Evidence]) -> list[tuple[Evidence, str]]:
    """Exact cells from retrieved tables whose row label (and column, if several) the question names."""
    terms = {stem(t) for t in tokenize(question)}
    found = []
    for ev in evidence[:3]:
        table = ev.item.get("table")
        if not table:
            continue
        columns = table["columns"][1:]
        col_hits = [i for i, c in enumerate(columns) if {stem(t) for t in tokenize(c)} & terms]
        best, best_score = None, 0
        for row in table["rows"]:
            label_terms = {stem(t) for t in tokenize(row["label"]) if t not in {"and", "the", "of"}}
            score = len(label_terms & terms) / max(len(label_terms), 1)
            if score > best_score:
                best, best_score = row, score
        if best and best_score >= 0.5:
            if len(columns) == 1:
                found.append((ev, f"{best['label']}: {best['cells'][0]['raw']}"))
            else:
                cols = col_hits or range(len(columns))
                found.append((ev, f"{best['label']} — " + ", ".join(f"{columns[i]}: {best['cells'][i]['raw']}" for i in cols)))
    return found


def _candidates(ev: Evidence) -> list[str]:
    """Quotable units of an evidence item: sentences, or a figure's printed values as one unit."""
    text = ev.item["text"]
    if ev.item["content_type"] == "figure":
        values = [l[2:] for l in text.splitlines() if l.startswith("- ")]
        return ["; ".join(values) + "."] if values else []
    out = []
    for line in text.splitlines():
        if line.startswith("## ") or DESCRIPTION_LINE.match(line):
            continue
        line = re.sub(r"^(?:-\s*(?:p\d+:\s*)?)+", "", line.strip())
        out += [s for s in re.split(r"(?<=[.!?\]])\s+(?=[A-Z])", line) if len(s) > 20]
    return out


def extractive_answer(question: str, result: RetrievalResult, settings: Settings, started: float) -> Answer:
    rows = _evidence_rows(result)
    label_of = {r["item_id"]: r["label"] for r in rows}
    weak = _weak_retrieval(result.signals, settings)
    if weak:
        return _insufficient(question, "extractive", result, rows, weak, started)

    calculation = bool(CALC_RE.search(question))
    picked = table_lookups(question, result.hits)
    if not calculation:
        picked = picked[:1]
    if not picked or calculation:
        terms = {stem(t) for t in content_terms(question)}
        wants_number = calculation or bool(re.search(r"\bhow (much|many|large)|percent|share|\bvalues?\b|\bwhat (is|are) the\b", question, re.I))
        top = {e.id for e in result.hits[:3]}
        pool = result.hits[:3] + [e for e in result.attached if e.attached_to in top]
        scored, seen = [], {s for _, s in picked}
        for ev in pool:
            for sentence in _candidates(ev):
                if sentence in seen or any(sentence in p for p in seen):
                    continue
                seen.add(sentence)
                overlap = len({stem(t) for t in tokenize(sentence)} & terms)
                if wants_number and re.search(r"\d", sentence):
                    overlap += 0.5
                if overlap >= 1:
                    scored.append((overlap, -(ev.rank or 4), sentence, ev))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        picked += [(ev, s) for _, _, s, ev in scored[: 2 if not picked else 1]]
    if not picked:
        return _insufficient(question, "extractive", result, rows, "No retrieved passage matches the question terms.", started)
    text = " ".join(f"{s} [{label_of[ev.id]}]" for ev, s in picked)
    if calculation:
        text = "Local mode does not calculate. Relevant values from the report: " + text
    cited = list({ev.id: ev for ev, _ in picked}.values())
    citations = [
        {"evidence": label_of[ev.id], "item_id": ev.id, "content_type": ev.item["content_type"], "pages": ev.item["pages"]}
        for ev in cited
    ]
    return Answer(
        question=question,
        status="answered",
        answer=text,
        mode="extractive",
        citations=citations,
        evidence=rows,
        checks={"unsupported_numbers": [], "calculations": []},
        signals=result.signals,
        seconds=round(time.time() - started, 2),
    )


def llm_answer(question: str, result: RetrievalResult, llm, started: float) -> Answer:
    rows = _evidence_rows(result)
    by_label = {r["label"]: r for r in rows}
    note = ""
    if result.signals["absent_terms"]:
        note = f"\nRetrieval note: these question terms do not occur anywhere in the report: {', '.join(result.signals['absent_terms'])}."
    user = f"Question: {question}{note}\n\nEvidence:\n\n{format_evidence(rows)}"
    try:
        data = llm.complete_json(SYSTEM_PROMPT, user)
    except Exception as exc:  # network, quota, malformed output
        return Answer(question, "error", f"LLM call failed: {exc}", llm.name, evidence=rows, signals=result.signals,
                      seconds=round(time.time() - started, 2))

    status = data.get("status") if data.get("status") in {"answered", "insufficient_evidence"} else "answered"
    text = str(data.get("answer", "")).strip()
    inline = [l for group in re.findall(r"\[([^\]]*)\]", text) for l in re.findall(r"E\d+", group)]
    labels = list(dict.fromkeys([str(c).strip("[] ") for c in data.get("citations", [])] + inline))
    invalid = [l for l in labels if l not in by_label]
    citations = [
        {"evidence": l, "item_id": by_label[l]["item_id"], "content_type": by_label[l]["content_type"], "pages": by_label[l]["pages"]}
        for l in labels
        if l in by_label
    ]
    calculations = verify_calculations(data.get("calculations"))
    cited_text = " ".join(by_label[c["evidence"]]["text"] for c in citations) or " ".join(r["text"] for r in rows)
    checks = {
        "invalid_citations": invalid,
        "calculations": calculations,
        "unsupported_numbers": unsupported_numbers(text, cited_text, question, calculations) if status == "answered" else [],
    }
    return Answer(question, status, text, llm.name, citations, rows, checks, result.signals, round(time.time() - started, 2))


def answer_question(question: str, retriever: Retriever, llm=None, top_k: int | None = None) -> Answer:
    started = time.time()
    result = retriever.search(question, top_k)
    if llm is None:
        return extractive_answer(question, result, retriever.settings, started)
    signals = result.signals
    if signals["max_dense"] < 0.5 and signals["term_coverage"] == 0:
        return _insufficient(question, llm.name, result, _evidence_rows(result), "No relevant passage was retrieved.", started)
    return llm_answer(question, result, llm, started)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="+")
    parser.add_argument("--extractive", action="store_true", help="do not call an LLM even if one is configured")
    parser.add_argument("--json", action="store_true", help="print the full answer record as JSON")
    args = parser.parse_args()
    settings = load_settings()
    retriever = Retriever(settings)
    if retriever.status["state"] != "ok":
        print(f"Warning: index is {retriever.status['state']}: {retriever.status['reason']}")
    llm = None
    if not args.extractive:
        from .llm import get_llm

        llm = get_llm(settings)
    answer = answer_question(" ".join(args.question), retriever, llm)
    if args.json:
        print(json.dumps(answer.to_dict(), indent=2, ensure_ascii=False))
        return
    print(f"[{answer.status} | {answer.mode} | {answer.seconds}s]\n{answer.answer}\n")
    for c in answer.citations:
        print(f"  {c['evidence']}: {c['item_id']} ({c['content_type']}) — page {', '.join(map(str, c['pages']))}")
    for w in answer.warnings:
        print(f"  WARNING: {w}")


if __name__ == "__main__":
    main()
