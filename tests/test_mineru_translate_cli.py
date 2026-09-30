import base64
import io
import json
import re
import zipfile
from pathlib import Path

import httpx
import pytest
from conftest import SAMPLE_PDF, SAMPLE_RESULT

from pdfskill import cli
from pdfskill.mineru import MinerUClient, MinerUError, ParseOptions, extract_zip, token_expiry
from pdfskill.translate import TranslateError, apply_translations, normalize_target


def _result_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in SAMPLE_RESULT.rglob("*"):
            if p.is_file():
                name = p.relative_to(SAMPLE_RESULT).as_posix()
                zf.write(p, "abc_content_list.json" if name == "content_list.json" else name)
    return buf.getvalue()


def test_v4_upload_flow(tmp_path):
    calls = []
    polls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url), dict(req.headers)))
        url = str(req.url)
        if url.endswith("/file-urls/batch"):
            body = json.loads(req.content)
            assert body["files"][0]["name"] == "sample.pdf" and body["model_version"] == "vlm"
            return httpx.Response(
                200, json={"code": 0, "data": {"batch_id": "B1", "file_urls": ["https://oss.example/up?sig=1"]}}
            )
        if url.startswith("https://oss.example/up"):
            assert "authorization" not in req.headers and "content-type" not in req.headers
            return httpx.Response(200)
        if "/extract-results/batch/B1" in url:
            polls["n"] += 1
            state = "running" if polls["n"] == 1 else "done"
            row = {
                "file_name": "sample.pdf",
                "state": state,
                "extract_progress": {"extracted_pages": 1, "total_pages": 2},
            }
            if state == "done":
                row["full_zip_url"] = "https://cdn.example/r.zip"
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [row]}})
        if url == "https://cdn.example/r.zip":
            assert "authorization" not in req.headers
            return httpx.Response(200, content=_result_zip())
        return httpx.Response(404)

    msgs = []
    client = MinerUClient("tok", transport=httpx.MockTransport(handler), poll_interval=0.01, progress=msgs.append)
    files = client.parse_file(SAMPLE_PDF, tmp_path / "out", ParseOptions())
    assert files.content_list.name == "abc_content_list.json" and files.middle.name == "layout.json"
    assert files.images_dir is not None
    assert any("running 1/2" in m for m in msgs)
    assert calls[0][2]["authorization"] == "Bearer tok"


def test_v4_errors():
    def handler(req):
        if str(req.url).endswith("/file-urls/batch"):
            return httpx.Response(401, json={"msgCode": "A0202", "msg": "user authenticate failed"})
        return httpx.Response(404)

    client = MinerUClient("bad", transport=httpx.MockTransport(handler))
    with pytest.raises(MinerUError, match="authentication failed"):
        client.parse_file(SAMPLE_PDF, Path("/nonexistent"), ParseOptions())
    with pytest.raises(MinerUError, match="MINERU_TOKEN"):
        MinerUClient(None, api="v4")


def test_token_expiry():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 2000000000}).encode()).decode().rstrip("=")
    assert token_expiry(f"h.{payload}.s") == 2000000000
    assert token_expiry("not-a-jwt") is None


def test_zip_slip_rejected(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(MinerUError, match="unsafe path"):
        extract_zip(z, tmp_path / "out")


def test_apply_translations_units():
    sizes = {1: [1000, 1000]}
    blocks = [
        {"id": "b1", "type": "heading", "level": 2, "text": "1 Intro", "page": 1, "bbox": [50, 50, 300, 70]},
        {"id": "b2", "type": "paragraph", "text": "First half of a long", "page": 1, "bbox": [50, 100, 950, 200]},
        {"id": "b3", "type": "paragraph", "text": "sentence that continues.", "page": 1, "bbox": [50, 600, 950, 700]},
        {
            "id": "b4",
            "type": "table",
            "caption": "Table 1: Results",
            "html": "",
            "page": 1,
            "bbox": [50, 800, 950, 900],
        },
    ]
    items = [
        {
            "id": "p1",
            "page": 1,
            "bbox": [50, 50, 300, 70],
            "source": "1 Intro",
            "text": "1 引言。",
            "unit_text": "",
            "unit": "p1",
            "kind": "single",
        },
        {
            "id": "p2",
            "page": 1,
            "bbox": [50, 100, 950, 200],
            "source": "First half of a long",
            "text": "前半",
            "unit_text": "完整的一句话。",
            "unit": "g1",
            "kind": "group",
        },
        {
            "id": "p3",
            "page": 1,
            "bbox": [50, 600, 950, 700],
            "source": "sentence that continues.",
            "text": "后半",
            "unit_text": "完整的一句话。",
            "unit": "g1",
            "kind": "group",
        },
        {
            "id": "p4",
            "page": 1,
            "bbox": [50, 780, 950, 795],
            "source": "Table 1: Results",
            "text": "表 1：结果",
            "unit_text": "",
            "unit": "p4",
            "kind": "single",
        },
    ]
    stats = apply_translations(blocks, items, sizes)
    assert blocks[0]["text_tr"] == "1 引言"  # heading punctuation cleaned
    assert blocks[1]["text_tr"] == "完整的一句话。"  # whole unit on the first member
    assert blocks[2]["text_tr"] == ""  # continuation member emptied
    assert blocks[3]["caption_text_tr"] == "表 1：结果"
    assert stats["matched_blocks"] == 3


def test_translate_target_language():
    assert normalize_target("zh-CN") == "zh"
    with pytest.raises(TranslateError, match="Simplified Chinese only"):
        normalize_target("fr")


def test_cli_global_options_before_or_after(tmp_path, capsys):
    lib = tmp_path / "L"
    assert cli.main(["-L", str(lib), "init"]) == 0
    assert (lib / ".pdfskill" / "library.toml").exists()
    assert cli.main(["list", "--library", str(lib), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == []
    assert cli.main(["--json", "-L", str(lib), "search", "anything"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hits"] == []


def test_cli_providers_and_guide(capsys):
    assert cli.main(["providers", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"deepseek", "dashscope", "zhipu", "moonshot", "openrouter", "ollama"} <= {r["name"] for r in rows}
    assert cli.main(["guide"]) == 0
    assert "pdfskill search" in capsys.readouterr().out


def test_cli_end_to_end_offline(library, capsys):
    assert cli.main(["-L", str(library.root), "ingest", str(SAMPLE_PDF), "--no-llm", "-q"]) == 0
    capsys.readouterr()
    assert cli.main(["-L", str(library.root), "search", "扁平化存储", "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert hit["pages"] == [2, 2] and hit["title"] == "Flat Document Libraries for Agents"
    assert cli.main(["-L", str(library.root), "get", hit["chunk_id"]]) == 0
    out = capsys.readouterr().out
    assert out.startswith("<!-- ") and "扁平化存储" in out
    assert cli.main(["-L", str(library.root), "doctor", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["library"]["documents"] == 1 and doc["mineru"]["api"] == "v1"


def test_skill_frontmatter_and_guide_in_sync():
    root = Path(__file__).parent.parent
    skill = (root / "skills" / "pdf-skill" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", skill, re.S)
    assert m, "SKILL.md needs YAML frontmatter"
    front, body = m.groups()
    keys = [line.split(":", 1)[0] for line in front.splitlines() if line and not line.startswith(" ")]
    assert set(keys) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert re.search(r"^name: pdf-skill$", front, re.M)
    desc = re.search(r'^description: "(.*)"$', front, re.M).group(1)
    assert 50 < len(desc) <= 1024 and "<" not in desc and ">" not in desc
    guide = (root / "src" / "pdfskill" / "guide.md").read_text(encoding="utf-8")
    assert body.strip() == guide.strip(), "SKILL.md body and src/pdfskill/guide.md must match"
    assert len(body.splitlines()) < 500
