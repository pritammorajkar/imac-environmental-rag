"""Retrieval against a real index built from the PDF with local embeddings."""

import dataclasses

from src.index import index_status


def _ids(result):
    return [e.id for e in result.hits]


def test_index_persisted_with_all_content_types(settings, built):
    assert index_status(settings)["state"] == "ok"
    assert set(built["manifest"]["item_counts"]) == {"text", "table", "figure", "footnote"}
    assert (settings.index_dir / "chroma").is_dir()


def test_changed_settings_mark_index_stale(settings):
    changed = dataclasses.replace(settings, chunk_chars=settings.chunk_chars + 1)
    status = index_status(changed)
    assert status["state"] == "stale" and "CHUNK_CHARS" in status["reason"]


def test_exact_table_question_ranks_table_first(retriever):
    result = retriever.search("What is iMac's power consumption in sleep mode at 115V?")
    assert _ids(result)[0] == "table-3"


def test_footnote_qualification_is_retrieved(retriever):
    result = retriever.search("Which iMac configuration does the recycled copper in the thermal module apply to?")
    assert "fn-14" in _ids(result)[:3]


def test_cited_endnotes_are_attached_to_hits(retriever):
    result = retriever.search("We also use 100 percent recycled copper in multiple thermal module components")
    evidence = {e.id: e for e in result.evidence}
    assert "fn-14" in evidence
    if evidence["fn-14"].attached_to:
        assert "endnote 14" in evidence["fn-14"].reason


def test_endnote_hit_brings_the_statement_it_qualifies(retriever):
    result = retriever.search("What does endnote 14 say?")
    assert _ids(result)[0] == "fn-14"
    assert any("thermal module components" in e.item["text"] for e in result.evidence if e.id != "fn-14")


def test_chart_question_finds_chart(retriever):
    result = retriever.search("What were the baseline emissions in the progress toward carbon neutral chart?")
    assert "figure-1" in _ids(result)[:2]


def test_absent_terms_signal(retriever):
    signals = retriever.search("What is the price of the iMac?").signals
    assert signals["absent_terms"] == ["price"]
    assert signals["absent_ratio"] == 1.0
    assert retriever.search("What is the sleep power at 115V?").signals["absent_terms"] == []
