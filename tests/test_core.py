from __future__ import annotations

import json
from pathlib import Path

import pytest

from context_pager.core.storage import Library
from context_pager.core.tools import (
    add_document,
    commit_to_long_term_memory,
    compress_document,
    reindex_document,
    remove_document,
    search_documents,
)
from tests.fakes import FakeEmbedder, FakeModels


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from context_pager.config import BridgeSettings

    s = BridgeSettings(
        PAGER_ROOT=str(tmp_path / "docs"),
        PAGER_DB=str(tmp_path / "pager.db"),
        PAGER_TELEMETRY_DB=str(tmp_path / "telemetry.db"),
        PAGER_CHUNK_TOKENS=512,
        PAGER_MAX_RETURN_TOKENS=2048,
        PAGER_BRIDGE_KEY="test-bridge-key",
    )
    monkeypatch.setenv("PAGER_ROOT", str(tmp_path / "docs"))
    monkeypatch.setenv("PAGER_DB", str(tmp_path / "pager.db"))
    return s


@pytest.fixture
def models():
    return FakeModels(embedder_=FakeEmbedder())


def _write_fixture(tmp_path: Path, name: str, text: str) -> str:
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return str(f)


async def test_library_roundtrip(settings, tmp_path):
    lib = Library(settings.db_path, 1024, chunks_per_page=4)
    chunks = ["alpha beta gamma", "delta epsilon"]
    emb = [[1.0] + [0.0] * 1023, [0.0, 1.0] + [0.0] * 1022]
    doc_id = lib.add_document("t", str(tmp_path / "x.txt"), "text", chunks, emb)
    doc = lib.get_document(doc_id)
    assert doc["chunks"] == 2
    assert lib.pages_total(doc_id) == 1
    lib.close()


async def test_search_finds_relevant_doc(settings, models, tmp_path):
    file = _write_fixture(tmp_path, "report.txt", "Revenue grew 12% this quarter across all regions.")
    doc_id = await add_document(file, settings=settings, models=models)
    env = json.loads(await search_documents("revenue growth", settings=settings, models=models))
    assert env["tool"] == "search_documents"
    ids = [r["doc_id"] for r in env["results"]]
    assert doc_id in ids
    r = [r for r in env["results"] if r["doc_id"] == doc_id][0]
    assert r["best_page"] >= 1
    assert r["title"] == "report"
    assert "snippet" in r


async def test_compress_small_doc_short_circuit(settings, models, tmp_path):
    file = _write_fixture(tmp_path, "small.txt", "Short document that fits in one page.")
    doc_id = await add_document(file, settings=settings, models=models)
    env = json.loads(await compress_document(doc_id, settings=settings, models=models))
    assert env["tool"] == "compress_document"
    assert env["page"] == 1
    assert env["pages_total"] == 1
    assert env["next_page"] is None
    assert env["metadata"]["skipped_compression"] is True
    assert env["content"] == "Short document that fits in one page."


async def test_compress_pagination(settings, models, tmp_path):
    # Long doc: many chunks -> multiple pages
    text = "Sentence number one. " * 1200  # ~4800 tokens -> ~10 chunks -> 3 pages
    file = _write_fixture(tmp_path, "long.txt", text)
    doc_id = await add_document(file, settings=settings, models=models)

    env = json.loads(await compress_document(doc_id, page=1, settings=settings, models=models))
    assert env["pages_total"] >= 2
    assert env["next_page"] == 2

    env2 = json.loads(await compress_document(doc_id, page=env["pages_total"], settings=settings, models=models))
    assert env2["next_page"] is None

    out = json.loads(await compress_document(doc_id, page=env["pages_total"] + 1, settings=settings, models=models))
    assert out["error"]


async def test_compress_cache_hit(settings, models, tmp_path):
    text = "Sentence one here. " * 300
    file = _write_fixture(tmp_path, "cached.txt", text)
    doc_id = await add_document(file, settings=settings, models=models)
    await compress_document(doc_id, page=1, settings=settings, models=models)
    env = json.loads(await compress_document(doc_id, page=1, settings=settings, models=models))
    assert env["metadata"]["cache_hit"] is True


async def test_focus_area_reranks_pages(settings, models, tmp_path):
    text = (
        "Page about revenue and sales growth. " * 200
        + "Page about marketing and brand. " * 200
        + "Page about engineering and infrastructure. " * 200
    )
    file = _write_fixture(tmp_path, "mixed.txt", text)
    doc_id = await add_document(file, settings=settings, models=models)
    env = json.loads(
        await compress_document(doc_id, page=1, focus_area="engineering infrastructure", settings=settings, models=models)
    )
    assert env["metadata"].get("focus_applied") is True
    assert "engineering" in env["content"] or "infrastructure" in env["content"]


async def test_commit_and_recall_memory(settings, models):
    await commit_to_long_term_memory("acme_q3", "Acme revenue hit 42M in Q3.", settings=settings, models=models)
    env = json.loads(await search_documents("acme q3 revenue", settings=settings, models=models))
    assert any("acme_q3" in i for i in env["recalled_insights"])


async def test_reindex_keeps_doc_id(settings, models, tmp_path):
    file = _write_fixture(tmp_path, "r.txt", "Original content version one.")
    doc_id = await add_document(file, settings=settings, models=models)
    await reindex_document(doc_id, settings=settings, models=models)
    env = json.loads(await compress_document(doc_id, settings=settings, models=models))
    assert env["doc_id"] == doc_id


async def test_remove_document(settings, models, tmp_path):
    file = _write_fixture(tmp_path, "rm.txt", "Will be removed.")
    doc_id = await add_document(file, settings=settings, models=models)
    remove_document(doc_id, settings=settings)
    out = json.loads(await compress_document(doc_id, settings=settings, models=models))
    assert "document not found" in out["error"]


def test_add_unsupported_type(tmp_path, settings, models):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ValueError, match="unsupported file type"):
        import asyncio

        asyncio.run(add_document(str(f), settings=settings, models=models))
