from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
SAMPLE_PDF = FIXTURES / "sample.pdf"
SAMPLE_RESULT = FIXTURES / "mineru_sample"
sys.path.insert(0, str(HERE))


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Never read the developer's config or keys."""
    monkeypatch.setenv("PDFSKILL_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("PDFSKILL_HOME", str(tmp_path / "home"))
    for var in (
        "PDFSKILL_LIBRARY",
        "PDFSKILL_LLM_PROVIDER",
        "PDFSKILL_LLM_MODEL",
        "PDFSKILL_LLM_BASE_URL",
        "PDFSKILL_LLM_API_KEY",
        "MINERU_TOKEN",
        "MINERU_API_KEY",
        "MINERU_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    from pdfskill.llm import PROVIDERS

    for p in PROVIDERS.values():
        for k in p.key_env:
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def sample_result() -> Path:
    return SAMPLE_RESULT


@pytest.fixture
def normalized():
    from pdfskill.normalize import load_result

    return load_result(SAMPLE_RESULT / "content_list.json", SAMPLE_RESULT / "layout.json")


@pytest.fixture
def library(tmp_path):
    """An initialised library whose MinerU cache already holds the sample result (no network)."""
    from pdfskill.ingest import META_FILE
    from pdfskill.library import Library, doc_id_for, sha256_file

    lib = Library(tmp_path / "lib")
    lib.init()
    doc_id = doc_id_for(sha256_file(SAMPLE_PDF))
    raw = lib.raw_cache(doc_id)
    shutil.copytree(SAMPLE_RESULT, raw)
    (raw / "x_content_list.json").write_bytes((raw / "content_list.json").read_bytes())
    (raw / "content_list.json").unlink()
    (raw / META_FILE).write_text(json.dumps({"api": "v1", "model_version": "vlm", "page_ranges": None}))
    return lib


@pytest.fixture(scope="session")
def mock_llm_url():
    import mock_llm

    server, port = mock_llm.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()


@pytest.fixture
def mock_llm_env(monkeypatch, mock_llm_url):
    monkeypatch.setenv("PDFSKILL_LLM_BASE_URL", mock_llm_url)
    monkeypatch.setenv("PDFSKILL_LLM_MODEL", "mock-model")
    monkeypatch.setenv("PDFSKILL_LLM_API_KEY", "test-key")
    import mock_llm

    mock_llm.State.sabotage_first_prose = False
    return mock_llm_url
