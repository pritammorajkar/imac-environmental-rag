"""Central configuration. Values come from environment variables (optionally a .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    pdf_path: Path
    artifacts_dir: Path
    embedding_model: str
    model_cache_dir: Path
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_base_url: str | None
    chunk_chars: int
    top_k: int
    min_dense_similarity: float
    max_absent_ratio: float

    @property
    def elements_path(self) -> Path:
        return self.artifacts_dir / "elements.json"

    @property
    def report_json_path(self) -> Path:
        return self.artifacts_dir / "ingestion_report.json"

    @property
    def report_md_path(self) -> Path:
        return self.artifacts_dir / "ingestion_report.md"

    @property
    def vision_cache_path(self) -> Path:
        return self.artifacts_dir / "vision_cache.json"

    @property
    def index_dir(self) -> Path:
        return self.artifacts_dir / "index"

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none"


def load_settings() -> Settings:
    provider = os.getenv("LLM_PROVIDER", "none").strip().lower()
    if provider not in {"none", "gemini", "openai"}:
        raise ValueError(f"LLM_PROVIDER must be none, gemini or openai (got {provider!r})")
    if provider == "gemini":
        model = os.getenv("GEMINI_MODEL") or os.getenv("GEMINI_CHAT_MODEL") or "gemini-3.5-flash"
        key = os.getenv("GEMINI_API_KEY", "")
        base_url = GEMINI_BASE_URL
    elif provider == "openai":
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL") or None
    else:
        model, key, base_url = "", "", None
    if provider != "none" and not key:
        raise ValueError(f"LLM_PROVIDER={provider} but its API key is not set; see .env.example")

    artifacts = _path("ARTIFACTS_DIR", ROOT / "artifacts")
    return Settings(
        pdf_path=_path("PDF_PATH", ROOT / "iMac_PER_Oct2024.pdf"),
        artifacts_dir=artifacts,
        embedding_model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        model_cache_dir=_path("MODEL_CACHE_DIR", artifacts / "models"),
        llm_provider=provider,
        llm_model=model,
        llm_api_key=key,
        llm_base_url=base_url,
        chunk_chars=int(os.getenv("CHUNK_CHARS", "1200")),
        top_k=int(os.getenv("TOP_K", "6")),
        min_dense_similarity=float(os.getenv("MIN_DENSE_SIMILARITY", "0.6")),
        max_absent_ratio=float(os.getenv("MAX_ABSENT_RATIO", "0.35")),
    )
