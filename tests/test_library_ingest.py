import json
import subprocess

import pytest
from conftest import SAMPLE_PDF

from pdfskill.config import load_settings
from pdfskill.ingest import IngestOptions, ingest
from pdfskill.library import Library, LibraryError
from pdfskill.query import get, locate, outline


def _git_log(lib):
    return subprocess.run(
        ["git", "-C", str(lib.root), "log", "--format=%s"], capture_output=True, text=True
    ).stdout.splitlines()


def test_init_layout(tmp_path):
    lib = Library(tmp_path / "l")
    created = lib.init()
    assert {"catalog.jsonl", ".gitignore", ".gitattributes", ".pdfskill/library.toml"} <= set(created)
    assert ".cache/" in (lib.root / ".gitignore").read_text()
    assert _git_log(lib) == ["init pdfskill library"]
    assert lib.init() == []


def test_ingest_offline_no_llm(library):
    res = ingest(library, SAMPLE_PDF, load_settings(), IngestOptions(use_llm=False))
    doc_id = res["doc_id"]
    assert res["status"] == "ingested" and res["validation"]["ok"]
    assert res["title"] == "Flat Document Libraries for Agents"
    md = library.doc_md(doc_id).read_text()
    assert "../assets/" in md and md.startswith("# Flat Document Libraries for Agents")
    doc = json.loads(library.doc_json(doc_id).read_text())
    assert doc["schema"] == "pdfskill.doc/1" and doc["source"]["name"] == "sample.pdf"
    assert all((library.root / a).exists() for a in doc["assets"])
    assert any(b["type"] == "furniture" and "md" not in b for b in doc["blocks"])
    assert doc["outline"][0]["level"] == 1
    entry = library.catalog()[doc_id]
    assert entry["pages"] == 2 and entry["valid"] is True
    assert _git_log(library)[0].startswith("ingest: Flat Document Libraries")
    # original file is never stored
    assert not list(library.root.glob("**/sample.pdf"))
    # idempotent
    assert ingest(library, SAMPLE_PDF, load_settings(), IngestOptions(use_llm=False))["status"] == "exists"


def test_queries_after_ingest(library):
    doc_id = ingest(library, SAMPLE_PDF, load_settings(), IngestOptions(use_llm=False))["doc_id"]
    hits = locate(library, "table 1")
    assert hits[0]["type"] == "table" and hits[0]["page"] == 1
    assert locate(library, "retrieval", kind="figure")[0]["page"] == 2
    o = outline(library, doc_id)
    assert [x["text"] for x in o["outline"]][:3] == [
        "Flat Document Libraries for Agents",
        "1 Introduction",
        "1.1 Notation",
    ]
    page2 = get(library, doc_id, pages="2")
    assert page2["text"].startswith("### 2.2 Retrieval pipeline") and page2["pages"] == [2, 2]
    sec = get(library, doc_id, heading="notation")
    assert sec["text"].startswith("### 1.1 Notation") and "$$" in sec["text"] and "1.2 Design" not in sec["text"]
    lines = get(library, doc_id, lines="1-1")
    assert lines["text"] == "# Flat Document Libraries for Agents"
    chunk = get(library, doc_id, chunk=f"{doc_id}#0001")
    assert chunk["heading"]
    assert library.resolve_id(doc_id[:6]) == doc_id
    assert library.resolve_id("sample.pdf") == doc_id
    with pytest.raises(LibraryError):
        get(library, doc_id, pages="9")


def test_remove(library):
    doc_id = ingest(library, SAMPLE_PDF, load_settings(), IngestOptions(use_llm=False))["doc_id"]
    removed = library.remove(doc_id)
    assert f"docs/{doc_id}.md" in removed and any(r.startswith("assets/") for r in removed)
    assert doc_id not in library.catalog()


def test_ingest_with_mock_llm(library, mock_llm_env):
    res = ingest(library, SAMPLE_PDF, load_settings(), IngestOptions())
    assert res["conversion"]["llm_segments"] > 0
    assert res["llm"]["model"] == "custom/mock-model"
    md = library.doc_md(res["doc_id"]).read_text()
    assert "let $q$ be a query" in md


def test_translation_requires_setup(library, mock_llm_env):
    from pdfskill.ingest import IngestError

    with pytest.raises(IngestError, match="setup translate"):
        ingest(library, SAMPLE_PDF, load_settings(), IngestOptions(translate="zh"))
