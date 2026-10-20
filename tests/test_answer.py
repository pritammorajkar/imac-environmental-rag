"""Answer generation guardrails. Real retrieval; the LLM is replaced by small stubs only to
exercise citation, number and calculation checks deterministically."""

from src.answer import answer_question, unsupported_numbers, verify_calculations


class StubLLM:
    name = "stub"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompt = reply, error, None

    def complete_json(self, system, user):
        self.prompt = user
        if self.error:
            raise self.error
        return self.reply


def test_calculations_are_recomputed():
    checked = verify_calculations([
        {"expression": "391 - 346", "result": "45"},
        {"expression": "391 kg − 346 kg", "result": "55 kg"},
        {"expression": "(445 - 346) / 445 * 100", "result": "22.2%"},
        {"expression": "half of it", "result": "x"},
    ])
    assert [c["ok"] for c in checked] == [True, False, True, None]
    assert checked[1]["computed"] == 45


def test_numbers_must_come_from_evidence_or_verified_calculations():
    calcs = verify_calculations([{"expression": "391 - 346", "result": "45"}])
    evidence = "iMac (four ports) 512GB 391 kg CO2e; total 346 kg CO2e"
    assert unsupported_numbers("It is 45 kg CO2e higher [E1].", evidence, "q", calcs) == []
    assert unsupported_numbers("It is 52 kg higher [E1].", evidence, "q", []) == ["52"]


def test_extractive_mode_answers_table_cell_with_page(retriever):
    answer = answer_question("What is iMac's power consumption in sleep mode at 115V?", retriever)
    assert answer.status == "answered" and answer.mode == "extractive"
    assert "0.57W" in answer.answer
    assert answer.cited_pages == [14] and answer.content_types_used == ["table"]


def test_extractive_mode_abstains_when_report_lacks_the_topic(retriever):
    answer = answer_question("What is the price of the iMac?", retriever)
    assert answer.status == "insufficient_evidence"
    assert answer.citations == []


def test_llm_citations_are_validated_and_pages_come_from_metadata(retriever):
    llm = StubLLM({"status": "answered", "answer": "Sleep uses 0.57W [E1] and 9.99W [E99].", "citations": ["E1", "E99"], "calculations": []})
    answer = answer_question("What is iMac's power consumption in sleep mode at 115V?", retriever, llm)
    assert answer.checks["invalid_citations"] == ["E99"]
    assert [c["item_id"] for c in answer.citations] == ["table-3"]
    assert answer.cited_pages == [14]
    assert answer.checks["unsupported_numbers"] == ["9.99"]
    assert any("9.99" in w for w in answer.warnings)
    assert "[E1] table | page 14" in llm.prompt


def test_llm_wrong_arithmetic_is_flagged(retriever):
    llm = StubLLM({"status": "answered", "answer": "391 - 346 = 55 kg CO2e [E1]", "citations": ["E1"],
                   "calculations": [{"expression": "391 - 346", "result": "55"}]})
    answer = answer_question("How much higher is the footprint of iMac (four ports) 512GB than (two ports) 256GB?", retriever, llm)
    assert answer.checks["calculations"][0]["ok"] is False
    assert any("recomputed 45" in w for w in answer.warnings)


def test_llm_abstention_is_kept(retriever):
    llm = StubLLM({"status": "insufficient_evidence", "answer": "The report does not state the price.", "citations": []})
    answer = answer_question("What is the price of the iMac?", retriever, llm)
    assert answer.status == "insufficient_evidence"
    assert "do not occur anywhere in the report: price" in llm.prompt


def test_llm_failure_is_reported_not_hidden(retriever):
    answer = answer_question("What is the sleep power at 115V?", retriever, StubLLM(error=TimeoutError("timed out")))
    assert answer.status == "error" and "timed out" in answer.answer
    assert answer.evidence  # evidence is still returned for inspection
