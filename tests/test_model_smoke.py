"""Opt-in real-model smoke tests.

Run with `RUN_MODEL_TESTS=1` (not run in CI). Exercises the *real* pipeline
end-to-end: bge embedder + Presidio PII + truncation (lite mode) or BGE-m3 +
LLMLingua-2 (set `PAGER_MODEL_SMOKE_FULL=1` for the heavy models).

First run downloads the spaCy model and HuggingFace weights (hundreds of MB).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from context_pager.config import BridgeSettings
from context_pager.core import tools
from context_pager.core.models import Models

RUN_MODEL_TESTS = os.getenv("RUN_MODEL_TESTS") == "1"
FULL = os.getenv("PAGER_MODEL_SMOKE_FULL") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_MODEL_TESTS,
    reason="set RUN_MODEL_TESTS=1 to run the real-model smoke tests",
)

EMAIL = "john.smith@example.com"
PHONE = "+1-415-555-0132"
BODY = (
    "Q3 revenue target is 42M. Reach out to John Smith at "
    f"{EMAIL} or {PHONE} before Thursday.\n\n"
) * 120  # ~13k chars -> several chunks -> multiple pages


@pytest.fixture(scope="module", autouse=True)
def ensure_spacy_model():
    import spacy

    try:
        spacy.load("en_core_web_sm")
    except OSError:
        subprocess.run(
            [sys.executable, "-m", "spacy", "download", "en_core_web_sm"], check=True
        )


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("model_smoke")
    settings = BridgeSettings(
        PAGER_ROOT=str(base / "docs"),
        PAGER_DB=str(base / "pager.db"),
        PAGER_TELEMETRY_DB=str(base / "telemetry.db"),
        PAGER_LITE=not FULL,
    )
    return settings, Models(settings)


@pytest.fixture(scope="module")
async def doc_id(tmp_path_factory, env):
    settings, _models = env
    src = tmp_path_factory.mktemp("model_smoke_src") / "q3.md"
    src.write_text(BODY, encoding="utf-8")
    return await tools.add_document(str(src), kind="markdown", settings=settings, models=_models)


async def test_search_returns_doc(doc_id, env):
    settings, models = env
    result = json.loads(
        await tools.search_documents("Q3 revenue target", settings=settings, models=models)
    )
    assert result["tool"] == "search_documents"
    assert any(r["doc_id"] == doc_id for r in result["results"])


async def test_compress_returns_masked_page(doc_id, env):
    settings, models = env
    env_result = json.loads(
        await tools.compress_document(doc_id, page=1, settings=settings, models=models)
    )
    assert env_result["tool"] == "compress_document"
    assert env_result["content"]
    assert env_result["metadata"]["original_tokens"] > env_result["metadata"]["compressed_tokens"] or (
        env_result["metadata"]["skipped_compression"]
    )
    assert EMAIL not in env_result["content"]
    assert PHONE not in env_result["content"]
    assert env_result["metadata"]["pii_redacted"].get("EMAIL_ADDRESS")


async def test_focus_reranks_pages(doc_id, env):
    settings, models = env
    env_result = json.loads(
        await tools.compress_document(
            doc_id, page=1, focus_area="phone number", settings=settings, models=models
        )
    )
    assert env_result["metadata"].get("focus_applied") is True


async def test_memory_commit_recall(doc_id, env):
    settings, models = env
    await tools.commit_to_long_term_memory(
        "acme_q3", "Q3 revenue target is 42M.", settings=settings, models=models
    )
    result = json.loads(
        await tools.search_documents("Q3 revenue target", settings=settings, models=models)
    )
    assert any("acme_q3" in r for r in result["recalled_insights"])
