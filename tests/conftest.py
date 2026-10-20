"""Shared fixtures. Tests run against the real PDF and real local embeddings, built into a
temporary artifacts directory so the project's own index is never touched."""

import dataclasses
import json

import pytest

from src.config import ROOT, load_settings
from src.index import build_index
from src.retrieve import Retriever


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    base = load_settings()
    if not base.pdf_path.exists():
        pytest.skip(f"report PDF not found at {base.pdf_path}")
    return dataclasses.replace(
        base,
        artifacts_dir=tmp_path_factory.mktemp("artifacts"),
        model_cache_dir=ROOT / "artifacts" / "models",
        llm_provider="none",
        llm_model="",
        llm_api_key="",
    )


@pytest.fixture(scope="session")
def built(settings):
    manifest = build_index(settings, reingest=True)
    payload = json.loads(settings.elements_path.read_text())
    report = json.loads(settings.report_json_path.read_text())
    return {"manifest": manifest, "elements": payload["elements"], "report": report}


@pytest.fixture(scope="session")
def elements(built):
    return built["elements"]


@pytest.fixture(scope="session")
def by_id(elements):
    return {e["id"]: e for e in elements}


@pytest.fixture(scope="session")
def retriever(settings, built):
    return Retriever(settings)
