"""Hybrid retrieval (dense + BM25, fused with reciprocal rank fusion) and evidence expansion.

Expansion makes sure qualifying context travels with a hit: endnotes cited by a retrieved item
are attached to it, a retrieved endnote brings the statement that cites it, and a table brings
the endnote it sits inside.
"""

import json
import re
from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi

from .config import Settings, load_settings
from .index import COLLECTION, embed_query, get_embedder, index_status

RRF_K = 60
CANDIDATES = 20
STOPWORDS = set(
    "a an and are as at be by does did do for from has have how in is it its of on or that the this "
    "to was were what when where which who why with according report imac apple's apple much many more less "
    "higher lower greater larger smaller difference compare compared between than there their they any "
    "about also does figure value values amount give show shown state stated say says".split()
)
NOTE_RE = re.compile(r"\b(?:foot|end)\s?notes?\s*#?\s*(\d{1,2})\b", re.I)


def tokenize(text: str) -> list[str]:
    text = text.lower().replace("–", "-").replace("—", " ")
    return re.findall(r"[a-z]+|\d+(?:\.\d+)?[a-z%]*", text)


def content_terms(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 2]


def stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s", "ly"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            if len(token) > 3 and token[-1] == token[-2] and token[-1] not in "aeious":
                token = token[:-1]  # modelling -> model, labelled -> label
            break
    return token


@dataclass
class Evidence:
    item: dict
    rank: int | None = None
    score: float = 0.0
    dense: float | None = None
    bm25: float | None = None
    attached_to: str | None = None
    reason: str = "retrieved"

    @property
    def id(self) -> str:
        return self.item["id"]


@dataclass
class RetrievalResult:
    query: str
    hits: list[Evidence]
    attached: list[Evidence]
    signals: dict = field(default_factory=dict)

    @property
    def evidence(self) -> list[Evidence]:
        return self.hits + self.attached


class Retriever:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        status = index_status(self.settings)
        if status["state"] == "missing":
            raise RuntimeError("No index found. Run: python -m src.ingest && python -m src.index")
        self.status = status
        index_dir = self.settings.index_dir
        self.items = json.loads((index_dir / "items.json").read_text())
        self.by_id = {i["id"]: i for i in self.items}
        self.item_of_element = {e: i["id"] for i in self.items for e in i["element_ids"]}
        self.bm25 = BM25Okapi([tokenize(i["search_text"]) for i in self.items])
        self.vocabulary = {stem(t) for i in self.items for t in tokenize(i["search_text"])}

        import chromadb
        from chromadb.config import Settings as ChromaSettings

        client = chromadb.PersistentClient(path=str(index_dir / "chroma"), settings=ChromaSettings(anonymized_telemetry=False))
        self.collection = client.get_collection(COLLECTION)
        self.embedder = get_embedder(self.settings)

    def dense(self, query: str, n: int) -> list[tuple[str, float]]:
        res = self.collection.query(query_embeddings=[embed_query(self.embedder, query)], n_results=n)
        return [(i, 1 - d) for i, d in zip(res["ids"][0], res["distances"][0])]

    def keyword(self, query: str, n: int) -> list[tuple[str, float]]:
        scores = self.bm25.get_scores(tokenize(query))
        ranked = sorted(zip((i["id"] for i in self.items), scores), key=lambda x: -x[1])
        return [(i, float(s)) for i, s in ranked[:n] if s > 0]

    def search(self, query: str, top_k: int | None = None, max_attached: int = 6) -> RetrievalResult:
        top_k = top_k or self.settings.top_k
        dense = self.dense(query, CANDIDATES)
        keyword = self.keyword(query, CANDIDATES)
        fused: dict[str, Evidence] = {}
        for ranking, kind in ((dense, "dense"), (keyword, "bm25")):
            for rank, (item_id, score) in enumerate(ranking, start=1):
                ev = fused.setdefault(item_id, Evidence(self.by_id[item_id]))
                ev.score += 1 / (RRF_K + rank)
                setattr(ev, kind, score)
        for number in NOTE_RE.findall(query):
            note = fused.setdefault(f"fn-{number}", Evidence(self.by_id[f"fn-{number}"])) if f"fn-{number}" in self.by_id else None
            if note:
                note.score += 1.0
                note.reason = "named in question"
        hits = sorted(fused.values(), key=lambda e: -e.score)[:top_k]
        for rank, ev in enumerate(hits, start=1):
            ev.rank = rank

        attached = self._expand(hits, max_attached, {i: e.score for i, e in fused.items()})
        terms = set(content_terms(query))
        top_text = " ".join(e.item["search_text"] for e in hits[:3]).lower()
        top_tokens = set(tokenize(top_text))
        signals = {
            "max_dense": round(max((s for _, s in dense), default=0.0), 4),
            "max_bm25": round(max((s for _, s in keyword), default=0.0), 4),
            "query_terms": sorted(terms),
            "term_coverage": round(sum(t in top_tokens for t in terms) / len(terms), 3) if terms else 0.0,
            "missing_terms": sorted(t for t in terms if t not in top_tokens),
            "absent_terms": sorted(t for t in terms if stem(t) not in self.vocabulary),
        }
        signals["absent_ratio"] = round(len(signals["absent_terms"]) / len(terms), 3) if terms else 0.0
        return RetrievalResult(query, hits, attached, signals)

    def _expand(self, hits: list[Evidence], limit: int, relevance: dict[str, float]) -> list[Evidence]:
        """Attach endnotes cited by hits, statements citing endnote hits, and tables inside
        endnotes. Candidates are ordered by their own fused relevance to the query, then by the
        rank of the hit that pulled them in, and capped at `limit`."""
        have = {e.id for e in hits}
        candidates: dict[str, tuple[float, int, Evidence, str]] = {}

        def add(item_id: str, parent: Evidence, reason: str):
            if item_id in have or item_id not in self.by_id or item_id in candidates:
                return
            candidates[item_id] = (relevance.get(item_id, 0.0), parent.rank, parent, reason)

        for ev in hits:
            item = ev.item
            if item["content_type"] == "footnote":
                for element_id in item.get("cited_by_elements", [])[:2]:
                    add(self.item_of_element.get(element_id, ""), ev, f"cites endnote {item['footnote_number']}")
                for element_id in item.get("related_elements", []):
                    add(self.item_of_element.get(element_id, ""), ev, f"inside endnote {item['footnote_number']}")
            else:
                for number in item["footnote_refs"]:
                    add(f"fn-{number}", ev, f"endnote {number} cited by {ev.id}")
        ordered = sorted(candidates.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:limit]
        return [Evidence(self.by_id[i], attached_to=parent.id, reason=reason) for i, (_, _, parent, reason) in ordered]
