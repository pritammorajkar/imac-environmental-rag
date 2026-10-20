# iMac Environmental Report — Multimodal RAG

Question answering over Apple's *Product Environmental Report: iMac* (introduced October 28, 2024; 15-page PDF). It answers from narrative text, reconstructed tables, chart labels and values, and endnotes. Every answer cites real PDF pages, and the system says so when the report lacks the answer.

## What is special about this PDF

This copy is a Google Docs re-render of Apple's report. The ingestion report flags each of these issues automatically:

| Issue | Pages | Effect |
|---|---|---|
| Tables have no ruling lines, and their header rows are **white text** (the dark header band was lost) | 12, 14 | Generic table finders find nothing (PyMuPDF `find_tables`: unusable; Docling: 0 tables) |
| The carbon "waterfall" chart has **labels and printed totals only**; its bars are gone. It is split across a page break | 2–3 | Values come from the text layer only; segment sizes cannot be recovered |
| The "Energy consumption of ENERGY STAR–rated products" chart is **missing entirely** (title plus an empty region) | 9 | Recorded as a missing-graphic placeholder so questions about it get "not in this document" |
| Endnote marker 6 is not attached to any text (probably a lost EPEAT badge) | 2 | Kept as an orphan reference |
| No vector graphics exist anywhere; 7 raster images (iMac photo, 6 material photos) | 1, 5 | Catalogued; none carry data |
| Source inconsistency: p2/p12 attribute 346 kg CO2e to iMac (two ports) **256GB**, while endnote 10 says the LCA used (two ports) **512GB** | 2, 12, 13 | Answers surface both statements |

The text layer is complete on every page, so OCR is not needed. Citations use **physical PDF page numbers** (1–15). The printed footers ("Report 2") sit on different physical pages in this render.

## Architecture

```
PDF ─ PyMuPDF spans (font, size, colour, superscript, bbox)
   ├─ layout.py         headings, running footers, reading order
   ├─ extract_tables.py white-header tables → typed cells (value, unit, qualifier)
   ├─ extract_figures.py numeric-callout charts/infographics (cross-page), images,
   │                     empty regions → missing-graphic placeholders, optional vision captions
   ├─ footnotes.py      endnotes parsed in sequence; superscript markers → [^n] links both ways
   └─ ingest.py         every line claimed by exactly one element → elements.json + report
index.py   section-aware text chunks + whole tables + figure descriptions + endnotes
           → local embeddings (fastembed BAAI/bge-small-en-v1.5, ONNX) → Chroma (persistent)
retrieve.py dense + BM25 fused by reciprocal rank; attaches endnotes cited by hits,
           the statement an endnote qualifies, and tables that sit inside an endnote
answer.py  LLM (Gemini/OpenAI-compatible) or local extractive mode; citation, number
           and arithmetic checks
evaluate.py retrieval / citation / answer metrics on any question set
app.py     Streamlit UI
```

Key decisions:

- **PyMuPDF over Docling.** Docling only ran on this Intel Mac after pinning `transformers<4.50` and `torchvision==0.17.2` (torch 2.2.2 is the last x86_64 macOS wheel). Even then it found 0 tables, merged the power table into endnote 19, dropped the white header and flattened footnote markers to plain digits. It also took 120 s versus 0.4 s.
- **Local-first.** Parsing, embeddings, the index and extractive answers need no API key. An LLM is optional and sits behind `src/llm.py`.
- **No double indexing.** Each PDF line belongs to one element. Table, chart and endnote lines never also appear in narrative chunks (a test enforces this).
- **Grounded numbers.**
  - Chart values carry `source: text_layer` and are never estimated.
  - Table cells are parsed into number, unit and qualifier (`<1%`, `↓>20%`).
  - LLM calculations are recomputed in Python.
  - Any number in an answer that isn't in the cited evidence (or a verified calculation) is flagged.
- **Citations from metadata.** The LLM cites evidence IDs (E1…); pages come from the index. Unknown IDs are dropped and reported.

## Install

```bash
cd imac-environmental-rag
python3.12 -m venv .venv && source .venv/bin/activate   # or: uv venv --python 3.12
pip install -r requirements.txt
cp .env.example .env
```

Put the report at `iMac_PER_Oct2024.pdf` in the project root, or set `PDF_PATH`. The original file is only read, never modified. The first index build downloads the embedding model (~130 MB) to `artifacts/models/`.

## Configure (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `none` | `none` = local extractive answers; `gemini` or `openai` to generate answers |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | `gemini-3.5-flash` | Tested. Gemini is called through its OpenAI-compatible endpoint |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | `gpt-4o-mini` | Same client; **not tested** (no key available) |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local |
| `CHUNK_CHARS`, `TOP_K` | 1200, 6 | Chosen by a retrieval sweep on the provisional set |
| `MIN_DENSE_SIMILARITY`, `MAX_ABSENT_RATIO` | 0.6, 0.35 | Extractive-mode abstention thresholds (calibrated on the provisional set) |

**Data sent externally** (only with an LLM provider): the question plus about 10 retrieved report excerpts per question. With `ingest --vision`, image crops are sent too. Cost with Gemini 3.5 Flash is a fraction of a cent per question; the 47-question evaluation is about 47 calls. `.env` is git-ignored; keys are never logged.

## Run

```bash
python -m src.ingest                 # PDF -> artifacts/elements.json + ingestion_report.md (0.4 s)
python -m src.ingest --vision        # optional: caption images with the LLM (cached)
python -m src.index                  # build if missing/stale; --rebuild to force (≈10 s)
python -m src.answer "What is the sleep power at 115V?"          # add --extractive or --json
streamlit run app.py                 # http://localhost:8501
python -m pytest                     # 39 tests on the real PDF; live LLM tests opt-in:
RUN_LIVE_LLM_TESTS=1 python -m pytest -m live_llm
python -m src.evaluate --mode retrieval|extractive|llm   # results in eval/results/
```

The index lives in `artifacts/index/` and persists across restarts. `manifest.json` records the PDF hash, the elements hash, the embedding model, the chunk size and the schema version. If any of these changes, the CLI and the UI report the index as **stale**, and `python -m src.index` rebuilds it. Rebuilds write to a temporary directory and swap atomically, so a failed build leaves the old index intact.

## Evaluation

`eval/provisional_questions.json` holds 47 questions written from this PDF while the official questions are unavailable. Every expected answer was checked against the PDF text. The categories are:

- text (8)
- table (7)
- chart (6)
- footnote (8)
- multi-page (5)
- calculation/units (6)
- unanswerable (7)

Each question records:

- expected status
- `must_include` key facts (with alternatives)
- expected numbers (with tolerance)
- evidence page groups
- acceptable extra pages
- verbatim `evidence_phrases`
- expected content types

Metrics reported per category:

- **Retrieval:** evidence-page recall, phrase recall (the stricter check), hit@k, MRR, content-type coverage.
- **Citations:** item-level precision and page-group recall.
- **Answers:** key-fact correctness and numeric accuracy.
- **Errors:** wrong abstentions, unanswerable questions answered, unsupported numbers, failed calculations.

Judging is deterministic key-fact matching. It is not human or LLM grading, so read the per-question table in `eval/results/*.md`.

Results on the final configuration are in `eval/results/` (run date in each file). The final summary is in the project report below.

### Adding the official sample questions

1. Save them as `eval/official_questions.json` using the same schema. Only `id`, `category`, `question`, `expected_answer` and `expected_status` are essential; add `evidence_pages`, `must_include` and `expected_numbers` to get the automated scores.
2. Run `python -m src.evaluate --dataset eval/official_questions.json --mode llm` (and `--mode retrieval`).
3. Re-check `MAX_ABSENT_RATIO`, `CHUNK_CHARS` and `TOP_K` against those results. They were tuned on the provisional set only.

No pipeline code depends on the question files.

## Known limitations

- **Charts.** The carbon chart's segment values and the whole ENERGY STAR chart are absent from this PDF. The system answers with printed values only, or says the data is unavailable. A better copy of the PDF (Apple's original) would need re-ingestion and probably new figure heuristics.
- **Heuristics are tuned to this report's layout**: white bold table headers, numeric callouts, endnotes in sequence. They are generic in form, and each detection is reported, but a different PDF needs its ingestion report reviewed. Table rows whose labels contain digits would not parse in the flattened-row parser; this is reported as "unparsed table text", never silently dropped.
- **Vision captions** are unverified. They are indexed only for images the model marks as data-bearing; none here are.
- **Extractive mode** (no LLM) cannot synthesise or calculate, and it abstains with simple lexical and similarity rules. On the provisional set it is a baseline, not the main mode.
- **Footnote marker 6** has no anchor text in this PDF. Chart category labels are listed, not assigned to individual bars, because the bars do not exist in the file.
- The **OpenAI** provider path shares code with Gemini but was not run.
