"""Evaluation scoring and dataset validity."""

import json

from src.config import ROOT
from src.evaluate import group_matches, score_answer, score_citations, score_retrieval

REQUIRED = {"id", "category", "question", "expected_answer", "expected_status", "must_include",
            "expected_numbers", "evidence_pages", "acceptable_pages", "content_types"}


def test_provisional_dataset_is_well_formed():
    data = json.loads((ROOT / "eval" / "provisional_questions.json").read_text())
    questions = data["questions"]
    assert len({q["id"] for q in questions}) == len(questions)
    assert {q["category"] for q in questions} == {"text", "table", "chart", "footnote", "multi_page", "calculation", "unanswerable"}
    for q in questions:
        assert REQUIRED <= set(q), q["id"]
        assert all(1 <= p <= 15 for g in q["evidence_pages"] for p in g), q["id"]
        if q["expected_status"] == "insufficient_evidence":
            assert not q["evidence_pages"], q["id"]
        else:
            assert q["evidence_pages"] and q["evidence_phrases"], q["id"]


def test_group_matching_supports_alternatives_and_regex():
    assert group_matches("A Gold rating in the US and Canada", ["United States", "re:\\bUS\\b"])
    assert not group_matches("industrial use", ["re:\\bUS\\b"])
    assert group_matches("Recycled and low–carbon materials", ["recycled and low-carbon materials"])


def test_answer_scoring():
    q = {"expected_status": "answered", "must_include": [["four ports"]], "expected_numbers": [{"value": 0.57}]}
    assert score_answer(q, "answered", "0.57W, iMac (four ports) only")["correct"]
    partial = score_answer(q, "answered", "0.57W")
    assert not partial["correct"] and partial["fact_score"] == 0.5
    assert score_answer(q, "insufficient_evidence", "")["wrong_abstention"]
    unanswerable = {"expected_status": "insufficient_evidence", "must_include": [], "expected_numbers": []}
    assert score_answer(unanswerable, "answered", "It costs 999")["answered_unanswerable"]
    assert score_answer({**q, "expected_status": "either"}, "insufficient_evidence", "")["correct"]
    assert score_answer({"expected_status": "answered", "must_include": [], "expected_numbers": [{"value": 99}]}, "answered", "–99 kg")["correct"]


def test_retrieval_and_citation_scoring():
    q = {"evidence_pages": [[5], [14]], "acceptable_pages": [13], "content_types": ["text", "footnote"],
         "evidence_phrases": ["four ports"]}
    evidence = [
        {"pages": [5, 6], "rank": 2, "content_type": "text", "text": "copper"},
        {"pages": [14], "rank": None, "content_type": "footnote", "text": "applies to iMac (four ports) only"},
    ]
    r = score_retrieval(q, evidence)
    assert r["evidence_recall"] == 1.0 and r["phrase_recall"] == 1.0 and r["mrr"] == 0.5 and r["type_coverage"] == 1.0
    c = score_citations(q, "answered", [{"pages": [5, 6]}, {"pages": [2]}])
    assert c == {"citation_precision": 0.5, "citation_recall": 0.5}
