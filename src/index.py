"""Build the persistent search index from extracted elements.

Items indexed: section-aware text chunks, whole tables (header kept with every row), figure
descriptions, and endnotes (with the statements that cite them). Embeddings are local
(fastembed / ONNX); vectors are stored in Chroma; BM25 is rebuilt from items.json at load time.

Run:  python -m src.index            (build if missing or stale)
      python -m src.index --rebuild  (re-ingest the PDF and rebuild from scratch)
"""

import argparse
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from .config import Settings, load_settings
from .ingest import SCHEMA_VERSION, file_sha256
from .ingest import run as run_ingest

COLLECTION = "imac_per"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z•])", text) if s]


def _heading_path(section: str, subsection: str | None) -> str:
    return f"{section} › {subsection}" if subsection else section


def chunk_text_elements(elements: list[dict], max_chars: int) -> list[dict]:
    """Pack consecutive text elements of one section into chunks of about max_chars.

    A chunk never crosses an h1 section boundary, prefers to break at h2/h3 headings, and
    repeats the previous short element as overlap so a statement keeps its lead-in.
    """
    chunks: list[dict] = []
    current: list[dict] = []

    def flush():
        if any(not e["heading"] for e in current):
            chunks.append(_make_chunk(current, len(chunks) + 1))

    pieces = []
    for el in elements:
        if len(el["text"]) <= max_chars:
            pieces.append(el)
            continue
        part = []
        for sentence in _sentences(el["text"]):
            if part and len(" ".join(part)) + len(sentence) > max_chars:
                pieces.append({**el, "text": " ".join(part)})
                part = []
            part.append(sentence)
        pieces.append({**el, "text": " ".join(part)})

    for el in pieces:
        size = sum(len(e["text"]) + 1 for e in current)
        new_section = current and el["section"] != current[-1]["section"]
        soft_break = el["heading"] in {"h2", "h3"} and size > 0.4 * max_chars
        if current and (new_section or soft_break or size + len(el["text"]) > max_chars):
            flush()
            last = current[-1]
            current = [last] if not new_section and not last["heading"] and len(last["text"]) <= 200 and not soft_break else []
        current.append(el)
    if current:
        flush()
    return chunks


def _make_chunk(elements: list[dict], number: int) -> dict:
    first = next(e for e in elements if not e["heading"])
    body = []
    for e in elements:
        body.append(f"## {e['text']}" if e["heading"] else e["text"])
    pages = sorted({p for e in elements for p in e["pages"]})
    text = "\n".join(body)
    return {
        "id": f"chunk-{number}",
        "content_type": "text",
        "pages": pages,
        "section": first["section"],
        "subsection": first["subsection"],
        "element_ids": [e["id"] for e in elements],
        "footnote_refs": sorted({n for e in elements for n in e["footnote_refs"]}),
        "text": text,
        "search_text": f"{_heading_path(first['section'], first['subsection'])}\n{text}",
    }


def build_items(elements: list[dict], max_chars: int) -> list[dict]:
    texts = [e for e in elements if e["type"] == "text"]
    items = chunk_text_elements(texts, max_chars)
    for e in elements:
        if e["type"] == "table":
            context = f"Context: {e['context']}\n" if e.get("context") else ""
            items.append(
                {
                    "id": e["id"],
                    "content_type": "table",
                    "pages": e["pages"],
                    "section": e["section"],
                    "subsection": e["subsection"],
                    "element_ids": [e["id"]],
                    "footnote_refs": e["related_footnotes"],
                    "text": f"{context}{e['text']}",
                    "search_text": f"{e['section']}\n{context}{e['text']}\n" + "\n".join(e["row_sentences"]),
                    "table": {"title": e["title"], "columns": e["columns"], "rows": e["rows"], "notes": e["notes"]},
                }
            )
        elif e["type"] == "figure" and e["indexable"]:
            items.append(
                {
                    "id": e["id"],
                    "content_type": "figure",
                    "figure_kind": e["kind"],
                    "pages": e["pages"],
                    "section": e["section"],
                    "subsection": e["subsection"],
                    "element_ids": [e["id"]],
                    "footnote_refs": e["footnote_refs"],
                    "text": e["text"],
                    "search_text": f"{_heading_path(e['section'], e['subsection'])}\n{e['text']}",
                }
            )
        elif e["type"] == "footnote":
            cited = [r for r in e["referenced_by"] if r["context"]]
            cites = "\n".join(f"- p{r['page']}: {r['context']}" for r in cited)
            text = f"Endnote {e['number']}: {e['text']}"
            items.append(
                {
                    "id": e["id"],
                    "content_type": "footnote",
                    "pages": e["pages"],
                    "section": "Endnotes",
                    "subsection": None,
                    "element_ids": [e["id"]],
                    "footnote_refs": [e["number"]],
                    "footnote_number": e["number"],
                    "cited_by_elements": [r["element_id"] for r in e["referenced_by"] if r["element_id"]],
                    "cited_on_pages": sorted({r["page"] for r in e["referenced_by"]}),
                    "related_elements": e["related_elements"],
                    "text": text + (f"\nThis endnote qualifies:\n{cites}" if cites else ""),
                    "search_text": f"Endnote {e['number']} footnote {e['number']}: {e['text']}\nQualifies: {cites}",
                }
            )
    return items


def get_embedder(settings: Settings):
    from fastembed import TextEmbedding

    settings.model_cache_dir.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(settings.embedding_model, cache_dir=str(settings.model_cache_dir))


def embed_documents(embedder, texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in embedder.embed(texts)]


def embed_query(embedder, query: str) -> list[float]:
    return next(iter(embedder.embed([QUERY_PREFIX + query]))).tolist()


def _elements_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def index_status(settings: Settings) -> dict:
    """Is the persisted index present and built from the current PDF and settings?"""
    manifest_path = settings.index_dir / "manifest.json"
    if not manifest_path.exists():
        return {"state": "missing", "reason": "no index built yet"}
    manifest = json.loads(manifest_path.read_text())
    if not settings.pdf_path.exists():
        return {"state": "stale", "reason": f"PDF not found at {settings.pdf_path}", "manifest": manifest}
    reasons = []
    if manifest["source_sha256"] != file_sha256(settings.pdf_path):
        reasons.append("the PDF changed since the index was built")
    if manifest["embedding_model"] != settings.embedding_model:
        reasons.append("EMBEDDING_MODEL changed")
    if manifest["chunk_chars"] != settings.chunk_chars:
        reasons.append("CHUNK_CHARS changed")
    if settings.elements_path.exists() and manifest["elements_sha256"] != _elements_sha(settings.elements_path):
        reasons.append("elements.json was re-ingested")
    if manifest["schema_version"] != SCHEMA_VERSION:
        reasons.append("ingestion schema changed")
    if reasons:
        return {"state": "stale", "reason": "; ".join(reasons), "manifest": manifest}
    return {"state": "ok", "reason": "", "manifest": manifest}


def build_index(settings: Settings, reingest: bool = False) -> dict:
    """Ingest if needed, embed all items, and atomically replace artifacts/index."""
    started = time.time()
    if reingest or not settings.elements_path.exists():
        run_ingest(settings=settings)
    payload = json.loads(settings.elements_path.read_text())
    if payload["source_sha256"] != file_sha256(settings.pdf_path):
        run_ingest(settings=settings)
        payload = json.loads(settings.elements_path.read_text())
    items = build_items(payload["elements"], settings.chunk_chars)

    embedder = get_embedder(settings)
    vectors = embed_documents(embedder, [i["search_text"] for i in items])

    import chromadb
    from chromadb.config import Settings as ChromaSettings

    tmp = settings.index_dir.with_name(settings.index_dir.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    client = chromadb.PersistentClient(path=str(tmp / "chroma"), settings=ChromaSettings(anonymized_telemetry=False))
    collection = client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    collection.add(
        ids=[i["id"] for i in items],
        embeddings=vectors,
        documents=[i["search_text"] for i in items],
        metadatas=[
            {
                "content_type": i["content_type"],
                "pages": ",".join(map(str, i["pages"])),
                "page_start": i["pages"][0],
                "section": i["section"],
                "footnote_refs": ",".join(map(str, i["footnote_refs"])),
            }
            for i in items
        ],
    )
    del collection, client
    (tmp / "items.json").write_text(json.dumps(items, indent=1, ensure_ascii=False))
    counts = {}
    for i in items:
        counts[i["content_type"]] = counts.get(i["content_type"], 0) + 1
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": payload["source_sha256"],
        "elements_sha256": _elements_sha(settings.elements_path),
        "embedding_model": settings.embedding_model,
        "chunk_chars": settings.chunk_chars,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "item_counts": counts,
        "build_seconds": round(time.time() - started, 1),
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))

    old = settings.index_dir.with_name(settings.index_dir.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if settings.index_dir.exists():
        settings.index_dir.rename(old)
    tmp.rename(settings.index_dir)
    shutil.rmtree(old, ignore_errors=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true", help="re-ingest the PDF and rebuild the index")
    args = parser.parse_args()
    settings = load_settings()
    status = index_status(settings)
    if status["state"] == "ok" and not args.rebuild:
        print(f"Index is up to date (built {status['manifest']['built_at']}); use --rebuild to force.")
        return
    if status["state"] == "stale":
        print(f"Index is stale: {status['reason']}. Rebuilding.")
    manifest = build_index(settings, reingest=args.rebuild)  # re-ingests by itself if the PDF changed
    print(f"Indexed {manifest['item_counts']} in {manifest['build_seconds']} s -> {settings.index_dir}")


if __name__ == "__main__":
    main()
