"""Evaluate retrieval and answers against a question set (provisional now, official later).

Run:  python -m src.evaluate                                  # configured mode (LLM if set)
      python -m src.evaluate --mode extractive                # no API calls
      python -m src.evaluate --mode retrieval                 # retrieval metrics only
      python -m src.evaluate --dataset eval/official_questions.json

Any dataset with the same JSON schema works; nothing in the pipeline is tied to these questions.
Answer correctness is judged by deterministic key-fact checks (must_include groups and expected
numbers), not by a human or an LLM judge; results say so.
"""

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from .answer import NUMBER_RE, answer_question
from .config import ROOT, load_settings
from .retrieve import Retriever


def _norm(text: str) -> str:
    text = re.sub(r"\[\^\d+\]", "", text)
    return re.sub(r"\s+", " ", text.replace("–", "-").replace("—", "-").replace("’", "'")).lower()


def group_matches(answer: str, group: list[str]) -> bool:
    for alt in group:
        if alt.startswith("re:"):
            if re.search(alt[3:], answer.replace("–", "-")):
                return True
        elif _norm(alt) in _norm(answer):
            return True
    return False


def answer_numbers(answer: str) -> list[float]:
    text = re.sub(r"\[(?:E\d+(?:,\s*E\d+)*|\^\d+)\]", " ", answer)
    out = []
    for raw in NUMBER_RE.findall(text):
        try:
            out.append(abs(float(raw.replace(",", "").replace("–", "-"))))
        except ValueError:
            pass
    return out


def score_answer(q: dict, status: str, text: str) -> dict:
    expected = q["expected_status"]
    groups = q.get("must_include", [])
    numbers = q.get("expected_numbers", [])
    found_groups = [group_matches(text, g) for g in groups]
    nums = answer_numbers(text)
    found_numbers = [any(abs(n - abs(e["value"])) <= e.get("tol", 1e-6) for n in nums) for e in numbers]
    facts = found_groups + found_numbers
    fact_score = sum(facts) / len(facts) if facts else 1.0
    if expected == "insufficient_evidence":
        correct = status == "insufficient_evidence"
        unsupported = status == "answered"
    elif expected == "either":
        correct = status == "insufficient_evidence" or (status == "answered" and fact_score == 1.0)
        unsupported = False
    else:
        correct = status == "answered" and fact_score == 1.0
        unsupported = False
    return {
        "correct": correct,
        "fact_score": round(fact_score if status == "answered" else 0.0, 3) if expected != "insufficient_evidence" else None,
        "missing_facts": [g for g, ok in zip(groups, found_groups) if not ok] + [e["value"] for e, ok in zip(numbers, found_numbers) if not ok],
        "numeric_ok": all(found_numbers) if numbers and status == "answered" else (None if not numbers else False),
        "answered_unanswerable": unsupported,
        "wrong_abstention": expected == "answered" and status == "insufficient_evidence",
    }


def score_pages(groups: list[list[int]], pages: set[int]) -> float | None:
    if not groups:
        return None
    return sum(bool(set(g) & pages) for g in groups) / len(groups)


def score_retrieval(q: dict, evidence: list[dict]) -> dict:
    groups = q.get("evidence_pages", [])
    if not groups:
        return {"evidence_recall": None, "phrase_recall": None, "hit_at_k": None, "mrr": None, "type_coverage": None}
    phrases = q.get("evidence_phrases", [])
    evidence_text = _norm(" ".join(e["text"] for e in evidence))
    relevant = {p for g in groups for p in g}
    hits = [e for e in evidence if e["rank"] is not None]
    all_pages = {p for e in evidence for p in e["pages"]}
    hit_pages = {p for e in hits for p in e["pages"]}
    first = next((e["rank"] for e in hits if set(e["pages"]) & relevant), None)
    types = set(q.get("content_types", []))
    found_types = {e["content_type"] for e in evidence if set(e["pages"]) & relevant}
    return {
        "evidence_recall": score_pages(groups, all_pages),
        "phrase_recall": round(sum(_norm(p) in evidence_text for p in phrases) / len(phrases), 3) if phrases else None,
        "missing_phrases": [p for p in phrases if _norm(p) not in evidence_text],
        "hit_at_k": bool(set(hit_pages) & relevant),
        "mrr": round(1 / first, 3) if first else 0.0,
        "type_coverage": round(len(types & found_types) / len(types), 3) if types else None,
    }


def score_citations(q: dict, status: str, citations: list[dict]) -> dict:
    """Precision: share of cited evidence items located on a relevant page (an item spanning
    pages 5-6 counts as correct if page 5 is relevant). Recall: share of evidence page groups
    covered by cited pages."""
    groups = q.get("evidence_pages", [])
    if status != "answered" or not groups:
        return {"citation_precision": None, "citation_recall": None}
    acceptable = {p for g in groups for p in g} | set(q.get("acceptable_pages", []))
    precise = [bool(set(c["pages"]) & acceptable) for c in citations]
    cited = {p for c in citations for p in c["pages"]}
    return {
        "citation_precision": round(sum(precise) / len(precise), 3) if precise else 0.0,
        "citation_recall": score_pages(groups, cited),
    }


def _mean(values) -> float | None:
    values = [float(v) for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def summarize(rows: list[dict], mode: str) -> dict:
    def block(subset):
        out = {"n": len(subset)}
        for key in ("evidence_recall", "phrase_recall", "hit_at_k", "mrr", "type_coverage"):
            out[key] = _mean(r["retrieval"][key] for r in subset)
        if mode != "retrieval":
            out["accuracy"] = _mean(r["score"]["correct"] for r in subset)
            out["fact_score"] = _mean(r["score"]["fact_score"] for r in subset)
            out["citation_precision"] = _mean(r["citations"]["citation_precision"] for r in subset)
            out["citation_recall"] = _mean(r["citations"]["citation_recall"] for r in subset)
            out["numeric_accuracy"] = _mean(r["score"]["numeric_ok"] for r in subset)
            out["answered_unanswerable"] = sum(r["score"]["answered_unanswerable"] for r in subset)
            out["wrong_abstentions"] = sum(r["score"]["wrong_abstention"] for r in subset)
            out["answers_with_unsupported_numbers"] = sum(bool(r["unsupported_numbers"]) for r in subset)
            out["failed_calculations"] = sum(r["failed_calculations"] for r in subset)
            out["errors"] = sum(r["status"] == "error" for r in subset)
        return out

    by_category = defaultdict(list)
    for r in rows:
        by_category[r["category"]].append(r)
    return {"overall": block(rows), "by_category": {c: block(v) for c, v in by_category.items()}}


def run(dataset_path: Path, mode: str, top_k: int | None = None) -> dict:
    settings = load_settings()
    dataset = json.loads(dataset_path.read_text())
    retriever = Retriever(settings)
    llm = None
    if mode == "llm":
        from .llm import get_llm

        llm = get_llm(settings)
        if llm is None:
            raise SystemExit("--mode llm needs LLM_PROVIDER and an API key; use --mode extractive or retrieval")
    rows = []
    for q in dataset["questions"]:
        if mode == "retrieval":
            result = retriever.search(q["question"], top_k)
            evidence = [
                {"item_id": e.id, "content_type": e.item["content_type"], "pages": e.item["pages"], "rank": e.rank,
                 "text": e.item["text"]}
                for e in result.evidence
            ]
            rows.append({"id": q["id"], "category": q["category"], "question": q["question"], "status": None,
                         "retrieval": score_retrieval(q, evidence),
                         "evidence": [{k: v for k, v in e.items() if k != "text"} for e in evidence]})
            continue
        answer = answer_question(q["question"], retriever, llm, top_k)
        record = answer.to_dict()
        rows.append(
            {
                "id": q["id"],
                "category": q["category"],
                "question": q["question"],
                "expected_answer": q["expected_answer"],
                "status": answer.status,
                "answer": answer.answer,
                "cited_pages": answer.cited_pages,
                "content_types_used": answer.content_types_used,
                "score": score_answer(q, answer.status, answer.answer),
                "retrieval": score_retrieval(q, record["evidence"]),
                "citations": score_citations(q, answer.status, answer.citations),
                "unsupported_numbers": answer.checks.get("unsupported_numbers", []),
                "failed_calculations": sum(c.get("ok") is False for c in answer.checks.get("calculations", [])),
                "warnings": answer.warnings,
                "seconds": answer.seconds,
                "retrieved": [(e["label"], e["item_id"], e["pages"]) for e in record["evidence"]],
            }
        )
        print(f"{q['id']:4} {'OK ' if rows[-1]['score']['correct'] else 'BAD'} {answer.status:22} {answer.answer[:90]!r}")
    return {
        "dataset": str(dataset_path),
        "mode": mode if mode != "llm" else f"llm:{llm.name}",
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "index_built_at": retriever.status["manifest"]["built_at"],
        "judging": "deterministic key-fact checks (must_include groups, expected numbers, status); not human-graded",
        "summary": summarize(rows, mode),
        "results": rows,
    }


def write_markdown(report: dict, path: Path) -> None:
    s = report["summary"]
    keys = [k for k in s["overall"] if k != "n"]
    lines = [
        f"# Evaluation: {Path(report['dataset']).name} — {report['mode']}",
        "",
        f"Run {report['run_at']}; index built {report['index_built_at']}. Judging: {report['judging']}.",
        "",
        "| Category | n | " + " | ".join(keys) + " |",
        "|---|---|" + "---|" * len(keys),
    ]
    for name, block in [("overall", s["overall"]), *sorted(s["by_category"].items())]:
        lines.append(f"| {name} | {block['n']} | " + " | ".join("" if block[k] is None else str(block[k]) for k in keys) + " |")
    if report["results"] and "score" in report["results"][0]:
        lines += ["", "## Questions", "", "| ID | Correct | Status | Cited pages | Missing facts | Answer |", "|---|---|---|---|---|---|"]
        for r in report["results"]:
            answer = r["answer"].replace("|", "/").replace("\n", " ")
            lines.append(
                f"| {r['id']} | {'yes' if r['score']['correct'] else 'NO'} | {r['status']} | {r['cited_pages']} | "
                f"{r['score']['missing_facts'] or ''} | {answer[:220]} |"
            )
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=str(ROOT / "eval" / "provisional_questions.json"))
    parser.add_argument("--mode", choices=["auto", "llm", "extractive", "retrieval"], default="auto")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--out-dir", default=str(ROOT / "eval" / "results"))
    args = parser.parse_args()
    mode = args.mode
    if mode == "auto":
        mode = "llm" if load_settings().llm_enabled else "extractive"
    report = run(Path(args.dataset), mode, args.top_k)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{Path(args.dataset).stem}__{mode}"
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    write_markdown(report, out / f"{stem}.md")
    print(json.dumps(report["summary"]["overall"], indent=2))
    print(f"Wrote {out / stem}.json and .md")


if __name__ == "__main__":
    main()
