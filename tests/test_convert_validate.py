import pytest

from pdfskill.convert import (
    ConvertStats,
    apply_outline,
    check_segments,
    convert,
    fidelity,
    numbering_depth,
    outline_levels,
    parse_segments,
    repair_math,
)
from pdfskill.normalize import content_blocks
from pdfskill.validate import clean_heading_text, katex_error, validate


def ref(p):
    return "../assets/" + p.split("/")[-1]


def test_numbering_depth():
    assert numbering_depth("1 Introduction") == 1
    assert numbering_depth("2.1 Storage layout") == 2
    assert numbering_depth("3.2.1. Details") == 3
    assert numbering_depth("第一章 绪论") == 1
    assert numbering_depth("第二节 方法") == 2
    assert numbering_depth("一、背景") == 1
    assert numbering_depth("（二）数据") == 2
    assert numbering_depth("Appendix A Proofs") == 1
    assert numbering_depth("The model works") is None


def test_heuristic_outline(normalized):
    blocks = content_blocks(normalized["blocks"])
    shaped, title = apply_outline(blocks, outline_levels(blocks), "fallback")
    heads = [(b["text"], b["level"]) for b in shaped if b["type"] == "heading"]
    assert title == "Flat Document Libraries for Agents"
    assert heads[0] == ("Flat Document Libraries for Agents", 1)
    assert ("1.1 Notation", 3) in heads
    assert ("2.1 Storage layout", 3) in heads  # MinerU missed it; numbering promotes it
    assert ("References", 2) in heads
    assert sum(1 for _, lv in heads if lv == 1) == 1


def test_synthetic_title_when_missing():
    blocks = [
        {"id": "b0001", "type": "paragraph", "text": "Body.", "page": 1},
        {"id": "b0002", "type": "heading", "level": 3, "text": "Deep", "page": 1},
    ]
    shaped, title = apply_outline(blocks, {}, "my-file.")
    assert title == "my-file"
    assert shaped[0]["synthetic"] and shaped[0]["level"] == 1
    assert shaped[2]["level"] == 2  # clamped: no jump from H1 to H3


def test_convert_without_llm_is_strictly_valid(normalized):
    res = convert(content_blocks(normalized["blocks"]), ref, fallback_title="x")
    assert res.report["ok"], res.report
    assert res.markdown.startswith("# Flat Document Libraries for Agents\n")
    assert "### 2.1 Storage layout" in res.markdown
    lines = res.markdown.split("\n")
    a, _ = res.anchors["b0008"]
    assert lines[a - 1] == "$$"


def test_validate_catches_problems():
    bad = "## Not H1\n\n#### jump\n\nprice $5 and $x$ ok\n\n$\\frac{a}{b$\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"
    _, rep = validate(bad, lint=False)
    text = "\n".join(rep.errors)
    assert "must start with an H1" in text
    assert "heading jumps" in text
    assert "table row has 3 cells" in text
    assert "math" in text
    good, rep = validate("# Title\n\nSome $x^2$ text and \\$5.\n", lint=True)
    assert rep.ok, rep.errors


def test_katex():
    assert katex_error(r"\frac{a}{b}", False) is None
    assert katex_error(r"\frac{a}{b", False)
    assert katex_error(r"\label{eq}x", True)
    assert katex_error("中文", False, "error")


def test_repair_math_cheap_fix_and_code_fallback():
    stats = ConvertStats()
    md = "Inline $x\\label{a}$ and $\\undefinedmacro{y}$.\n\n$$\n\\hdots\n$$\n"
    out = repair_math(md, None, stats)
    assert "$x$" in out
    assert "`\\undefinedmacro{y}`" in out
    assert "$$\n\\cdots\n$$" in out
    assert stats.math_fixed == 2 and stats.math_as_code == 1


def test_clean_heading_text():
    assert clean_heading_text("Results:") == "Results"
    assert clean_heading_text("结论。") == "结论"


def test_segments_protocol():
    chunk = [
        {"id": "b0001", "type": "paragraph", "text": "We train for 100 epo-", "page": 1},
        {"id": "b0002", "type": "table", "html": "", "page": 1},
        {"id": "b0003", "type": "paragraph", "text": "chs using AdamW.", "page": 2},
        {"id": "b0004", "type": "paragraph", "text": "Second paragraph here.", "page": 2},
    ]
    good = "[[b0001+b0003]]\nWe train for 100 epochs using AdamW.\n\n[[b0004]]\nSecond paragraph here."
    acc, problems = check_segments(chunk, parse_segments(good))
    assert not problems and acc["b0001"].ids == ["b0001", "b0003"]
    invented = "[[b0001+b0003]]\nWe train for 100 epochs using AdamW. We also invented a whole new claim here.\n"
    acc, problems = check_segments(chunk, parse_segments(invented))
    assert "b0001" not in acc
    assert any("fidelity" in p for p in problems) and any("missing" in p for p in problems)
    heading = "[[b0004]]\n# Second paragraph here."
    _, problems = check_segments(chunk, parse_segments(heading))
    assert any("headings" in p for p in problems)
    ratio, length = fidelity("深度学习模型在大规模数据上训练。", "深度学习模型在大规模数据上训练。")
    assert ratio == 100 and length == 1


def test_convert_with_mock_llm(normalized, mock_llm_env):
    from pdfskill.config import load_settings
    from pdfskill.llm import LLM, resolve

    llm = LLM(resolve(load_settings().llm))
    res = convert(content_blocks(normalized["blocks"]), ref, llm=llm, fallback_title="x", max_chunk_chars=1500)
    assert res.report["ok"], res.report
    assert "let $q$ be a query" in res.markdown  # OCR glyph fixed by the model
    assert "\n1. S. Robertson" in res.markdown and "\n2. Example Authors" in res.markdown
    assert "Ada Example" in res.markdown and "## Ada" not in res.markdown
    assert res.stats.llm_segments > 0 and res.stats.fallback_segments == 0


def test_convert_rejects_sabotaged_llm_output(normalized, mock_llm_env):
    import mock_llm

    from pdfskill.config import load_settings
    from pdfskill.llm import LLM, resolve

    mock_llm.State.sabotage_first_prose = True
    try:
        llm = LLM(resolve(load_settings().llm))
        res = convert(content_blocks(normalized["blocks"]), ref, llm=llm, fallback_title="x", max_chunk_chars=1500)
    finally:
        mock_llm.State.sabotage_first_prose = False
    assert "invented by the model" not in res.markdown
    assert res.stats.retries >= 1
    assert res.report["ok"]


@pytest.mark.parametrize("text", ["1 Introduction", "Short title"])
def test_outline_levels_without_candidates_do_not_crash(text):
    blocks = [{"id": "b0001", "type": "paragraph", "text": text + ".", "page": 1}]
    assert outline_levels(blocks) == {}
