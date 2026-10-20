"""Streamlit UI for the iMac Product Environmental Report RAG. Run: streamlit run app.py"""

import json
import re
from datetime import datetime

import pymupdf
import streamlit as st

from src.answer import answer_question
from src.config import load_settings
from src.index import build_index, index_status
from src.llm import get_llm
from src.retrieve import Retriever

SAMPLES = {
    "Sleep power at 115V": "What is iMac's power consumption in sleep mode at 115V?",
    "Recycled copper — which model?": "Which iMac configuration does the recycled copper in the thermal module apply to?",
    "Carbon chart: baseline vs final": "In the 'Progress toward carbon neutral' chart, what are the baseline emissions and the final iMac emissions?",
    "Footprint: 4-port vs 2-port": "How much higher is the carbon footprint of iMac (four ports) 512GB than iMac (two ports) 256GB?",
    "Does 15% recycled include packaging?": "Does iMac's 'more than 15 percent recycled content' figure include packaging?",
    "Price (not in the report)": "What is the price of the iMac?",
}
STATUS = {
    "answered": ("Answered from the report", "success", "✅"),
    "insufficient_evidence": ("The report doesn't contain enough information to answer this", "warning", "⚠️"),
    "error": ("Something went wrong", "error", "❌"),
}
TYPE_NAMES = {"text": "Text", "table": "Table", "figure": "Figure", "footnote": "Endnote"}
FIGURE_NAMES = {"chart": "chart", "infographic": "infographic", "missing-graphic": "missing chart", "image": "image"}
SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")

st.set_page_config(page_title="iMac Environmental Report Q&A", page_icon="🌱", layout="wide")
settings = load_settings()


@st.cache_resource(show_spinner="Loading the index and embedding model…")
def load_retriever(built_at: str):
    return Retriever(settings)


@st.cache_resource
def load_llm():
    return get_llm(settings)


@st.cache_data
def page_png(page: int) -> bytes:
    with pymupdf.open(settings.pdf_path) as pdf:
        return pdf[page - 1].get_pixmap(dpi=110).tobytes("png")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def pages_label(pages: list[int]) -> str:
    if len(pages) > 1 and pages == list(range(pages[0], pages[-1] + 1)):
        return f"pp. {pages[0]}–{pages[-1]}"
    return ("pp. " if len(pages) > 1 else "p. ") + ", ".join(map(str, pages))


def footnotes_to_superscript(text: str) -> str:
    return re.sub(r"\[\^(\d+)\]", lambda m: m.group(1).translate(SUPERSCRIPT), text)


def readable_answer(text: str, evidence_pages: dict[str, list[int]]) -> str:
    """Replace evidence tags like [E1, E3] with page references and [^7] with superscripts."""

    def cite(match):
        labels = re.findall(r"E\d+", match.group(1))
        pages = sorted({p for l in labels for p in evidence_pages.get(l, [])})
        return f" *({pages_label(pages)})*" if pages else ""

    text = re.sub(r"\s*\[((?:E\d+[,\s]*)+)\]", cite, text)
    return footnotes_to_superscript(text)


def source_title(item: dict) -> str:
    kind = item["content_type"]
    if kind == "table":
        return item["table"]["title"]
    if kind == "footnote":
        return f"Endnote {item['footnote_number']}"
    if kind == "figure":
        name = FIGURE_NAMES.get(item.get("figure_kind"), "figure")
        title = re.search(r"under '([^']+)'", item["text"].splitlines()[0])
        return f"{title.group(1)} ({name})" if title else name.capitalize()
    return item["section"] + (f" › {item['subsection']}" if item.get("subsection") else "")


def source_label(item: dict) -> str:
    title = footnotes_to_superscript(source_title(item))
    return title if item["content_type"] == "footnote" else f"{TYPE_NAMES[item['content_type']]}: {title}"


def built_label(built_at: str) -> str:
    try:
        return datetime.strptime(built_at, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y, %H:%M")
    except ValueError:
        return built_at


status = index_status(settings)
report = json.loads(settings.report_json_path.read_text()) if settings.report_json_path.exists() else None
llm = load_llm()

with st.sidebar:
    st.subheader("📄 Report")
    if report:
        c = report["counts"]
        a, b = st.columns(2)
        a.metric("Pages", report["page_count"])
        b.metric("Tables", c["tables"])
        a.metric("Endnotes", c["endnotes"])
        b.metric("Charts", c["figures"].get("chart", 0) + c["figures"].get("infographic", 0))
        figures = " · ".join(plural(v, FIGURE_NAMES.get(k, k)) for k, v in c["figures"].items())
        st.caption(f"Figures found: {figures}. {c['footnote_markers_linked']} footnote references linked.")
        with st.expander(f"⚠️ {plural(len(report['warnings']), 'extraction note')}"):
            for w in report["warnings"]:
                st.markdown(f"- {w}")

    st.subheader("🗂️ Search index")
    if status["state"] == "ok":
        m = status["manifest"]
        st.success(f"Ready · built {built_label(m['built_at'])}")
        counts = m["item_counts"]
        st.caption(
            f"{plural(counts.get('text', 0), 'text passage')}, {plural(counts.get('table', 0), 'table')}, "
            f"{plural(counts.get('figure', 0), 'figure')} and {plural(counts.get('footnote', 0), 'endnote')} indexed "
            "with local embeddings."
        )
    elif status["state"] == "stale":
        st.warning(f"Out of date: {status['reason']}.")
    else:
        st.error("Not built yet.")
    if st.button("Rebuild from PDF", width="stretch", help="Re-read the PDF and rebuild the index (about 15 seconds)"):
        with st.spinner("Reading the PDF and rebuilding the index…"):
            build_index(settings, reingest=True)
        st.cache_resource.clear()
        st.rerun()

    st.subheader("⚙️ Answering")
    options = ([f"AI answer ({settings.llm_model})"] if llm else []) + ["Local only (no API)"]
    mode = st.radio("Mode", options, label_visibility="collapsed",
                    help="Local mode makes no API calls; it quotes the best matching passage or table cell and cannot calculate.")
    use_llm = bool(llm) and mode.startswith("AI")
    if use_llm:
        st.caption("Your question and the retrieved report excerpts are sent to Google Gemini.")
    else:
        st.caption("Everything runs on this computer. Answers are quoted passages, without calculations.")
    with st.expander("Advanced"):
        top_k = st.slider("Passages to retrieve", 3, 10, settings.top_k)

st.title("🌱 iMac Environmental Report — Q&A")
st.markdown(
    "Ask anything about Apple's *Product Environmental Report* for iMac (introduced October 28, 2024). "
    "Answers use only the report: text, tables, chart values and endnotes, with page references."
)

if status["state"] == "missing":
    st.info("The search index hasn't been built yet. Click **Rebuild from PDF** in the sidebar.")
    st.stop()

retriever = load_retriever(status["manifest"]["built_at"])

st.session_state.setdefault("question", "")
st.caption("Try an example:")
cols = st.columns(3)
for i, (label, sample) in enumerate(SAMPLES.items()):
    if cols[i % 3].button(label, key=f"sample{i}", width="stretch"):
        st.session_state.question = sample
question = st.text_input("Your question", key="question", placeholder="e.g. What share of iMac's emissions comes from product use?")

if not question:
    st.stop()

with st.spinner("Searching the report…"):
    answer = answer_question(question, retriever, llm if use_llm else None, top_k)

evidence_pages = {e["label"]: e["pages"] for e in answer.evidence}
label, kind, icon = STATUS[answer.status]
source = "AI answer" if use_llm else "Local answer"
getattr(st, kind)(f"{icon} **{label}** · {source} · {answer.seconds:.1f} s")
with st.container(border=True):
    st.markdown(readable_answer(answer.answer, evidence_pages))
for warning in answer.warnings:
    st.warning(warning)

left, right = st.columns([3, 2], gap="large")
with left:
    if answer.citations:
        st.subheader("Sources")
        for c in answer.citations:
            item = retriever.by_id[c["item_id"]]
            st.markdown(f"- **{pages_label(c['pages'])}** — {source_label(item)}")

    calcs = answer.checks.get("calculations") or []
    if calcs:
        st.subheader("Calculations")
        for calc in calcs:
            verdict = {True: "✅ verified", False: f"❌ incorrect — correct value is {calc['computed']}", None: "not checkable"}[calc["ok"]]
            st.markdown(f"- `{calc['expression']} = {calc['claimed']}` · {verdict}")

    st.subheader("Evidence from the report")
    cited = {c["evidence"] for c in answer.citations}
    for e in answer.evidence:
        item = retriever.by_id[e["item_id"]]
        tag = " · cited" if e["label"] in cited else ""
        added = " · added because it is linked by an endnote" if e["attached_to"] else ""
        with st.expander(f"{pages_label(e['pages'])} — {source_label(item)}{tag}"):
            if added:
                st.caption(added.strip(" ·").capitalize())
            body = footnotes_to_superscript(e["text"])
            if e["content_type"] != "table":
                body = re.sub(r"^## (.+)$", r"**\1**", body, flags=re.M).replace("\n", "  \n")
            st.markdown(body)

    with st.expander("Technical details"):
        st.caption("Retrieval scores and abstention signals, for debugging.")
        st.dataframe(
            [{"evidence": e["label"], "item": e["item_id"], "type": e["content_type"], "pages": pages_label(e["pages"]),
              "rank": e["rank"], "dense": e["dense"], "bm25": e["bm25"], "why": e["reason"]} for e in answer.evidence],
            hide_index=True,
        )
        st.json(answer.signals, expanded=False)

with right:
    pages = answer.cited_pages or sorted({p for e in answer.evidence[:3] for p in e["pages"]})
    if pages:
        st.subheader("Page preview")
        page = st.selectbox("Page", pages, format_func=lambda p: f"Page {p}", label_visibility="collapsed")
        st.image(page_png(page), width="stretch")
