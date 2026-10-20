"""Optional end-to-end check with the real LLM (costs a few API calls).

Run with: RUN_LIVE_LLM_TESTS=1 LLM_PROVIDER=gemini python -m pytest -m live_llm
"""

import dataclasses
import os

import pytest

from src.answer import answer_question
from src.config import load_settings
from src.llm import get_llm

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(os.getenv("RUN_LIVE_LLM_TESTS") != "1", reason="set RUN_LIVE_LLM_TESTS=1 to call the LLM API"),
]


@pytest.fixture(scope="module")
def llm():
    live = load_settings()
    if not live.llm_enabled:
        pytest.skip("LLM_PROVIDER is none")
    return get_llm(live)


def test_live_numeric_answer_with_citation(retriever, llm):
    answer = answer_question("What is the power adapter efficiency at 230V?", retriever, llm)
    assert answer.status == "answered"
    assert "91.9" in answer.answer and 14 in answer.cited_pages
    assert not answer.warnings


def test_live_footnote_qualification(retriever, llm):
    answer = answer_question("Which iMac configuration does the recycled copper in the thermal module apply to?", retriever, llm)
    assert answer.status == "answered" and "four" in answer.answer.lower()


def test_live_abstains_on_missing_information(retriever, llm):
    answer = answer_question("How much does the iMac weigh?", retriever, llm)
    assert answer.status == "insufficient_evidence"
