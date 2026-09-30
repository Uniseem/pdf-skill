"""End-to-end translation through the real retain-pdf pipeline (mock LLM, no MinerU call).

Slow (~20 s) and needs `pdfskill setup translate`; runs only when
PDFSKILL_E2E_HOME points at a PDFSKILL_HOME where setup has been done.
"""

import json
import os

import pytest
from conftest import SAMPLE_PDF

from pdfskill.config import load_settings
from pdfskill.ingest import IngestOptions, ingest

E2E_HOME = os.environ.get("PDFSKILL_E2E_HOME")


@pytest.mark.skipif(not E2E_HOME, reason="set PDFSKILL_E2E_HOME to a PDFSKILL_HOME with `setup translate` done")
def test_ingest_and_translate(library, mock_llm_env, monkeypatch):
    monkeypatch.setenv("PDFSKILL_HOME", E2E_HOME)
    res = ingest(library, SAMPLE_PDF, load_settings(), IngestOptions(translate="zh"))
    doc_id = res["doc_id"]
    t = res["translation"]
    assert (library.root / t["pdf"]).stat().st_size > 10_000
    zh = (library.root / t["markdown"]).read_text()
    assert zh.startswith("# ") and "模拟译文" in zh and "../assets/" in zh
    assert t["match"]["matched_blocks"] >= 10
    doc = json.loads(library.doc_json(doc_id).read_text())
    assert doc["translations"]["zh"]["pdf"] == f"docs/{doc_id}.zh.pdf"
    assert any("md_zh" in b for b in doc["blocks"])
    assert library.catalog()[doc_id]["translations"]["zh"]["markdown"] == f"docs/{doc_id}.zh.md"
    assert library.chunks_file(doc_id, "zh").exists()
    # translating again is a no-op
    assert ingest(library, SAMPLE_PDF, load_settings(), IngestOptions(translate="zh"))["status"] == "exists"
