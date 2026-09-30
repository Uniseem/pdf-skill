from pdfskill.markdown import escape_block_start, html_table_to_gfm, marker, render_blocks, strip_markers
from pdfskill.normalize import content_blocks, normalize, page_offset_from_ranges, tidy_math


def test_normalize_sample(normalized):
    blocks = normalized["blocks"]
    assert [p["page"] for p in normalized["pages"]] == [1, 2]
    assert normalized["pages"][0]["size"] == [595, 841]
    assert normalized["parser"]["engine"] == "mineru"
    types = {b["type"] for b in blocks}
    assert {"heading", "paragraph", "equation", "table", "chart", "furniture"} <= types
    assert blocks[0] == {**blocks[0], "id": "b0001", "type": "heading", "level": 1, "page": 1}
    eq = next(b for b in blocks if b["type"] == "equation")
    assert not eq["latex"].startswith("$$") and "\\tag{1}" in eq["latex"]
    furniture = [b for b in blocks if b["type"] == "furniture"]
    assert {b["role"] for b in furniture} == {"header", "page_number"}
    assert all(b["type"] != "furniture" for b in content_blocks(blocks))


def test_page_offset_and_types():
    cl = [
        {"type": "text", "text": "Hello", "page_idx": 0, "bbox": [1, 2, 3, 4]},
        {"type": "list", "sub_type": "ref_text", "list_items": ["[1] A", " ", "[2] B"], "page_idx": 1},
        {"type": "code", "sub_type": "code", "code_body": "```python\nprint(1)\n```", "page_idx": 1},
        {"type": "text", "text": "   ", "page_idx": 1},
        {"type": "brand_new_type", "text": "kept", "page_idx": 1},
    ]
    out = normalize(cl, page_offset=page_offset_from_ranges("5-9"))
    assert [b["page"] for b in out["blocks"]] == [5, 6, 6, 6]
    assert out["blocks"][1]["items"] == ["[1] A", "[2] B"]
    assert out["blocks"][2] == {**out["blocks"][2], "text": "print(1)", "lang": "python"}
    assert out["blocks"][3]["source_type"] == "brand_new_type"
    assert page_offset_from_ranges("2,4-6") == 0


def test_tidy_math():
    assert tidy_math("see $ x^2 $ here") == "see $x^2$ here"
    assert tidy_math("costs $5 and $10") == "costs $5 and $10"
    assert tidy_math("$ a = b $ and $c$") == "$a = b$ and $c$"


def test_html_table_spans_and_pipes():
    html = (
        '<table><tr><td rowspan="2">Method</td><td colspan="2">Score</td></tr>'
        "<tr><td>a|b</td><td>x<br>y</td></tr></table>"
    )
    md = html_table_to_gfm(html)
    lines = md.splitlines()
    assert lines[0] == "| Method | Score | Score |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| Method | a\\|b | x y |"


def test_escape_block_start():
    assert escape_block_start("# not a heading") == "\\# not a heading"
    assert escape_block_start("1. not a list") == "1\\. not a list"
    assert escape_block_start("- dash") == "\\- dash"
    assert escape_block_start("normal text") == "normal text"


def test_strip_markers_line_ranges():
    md = f"{marker('b0001')}\n\n# Title\n\n{marker(['b0002', 'b0003'])}\n\nPara one\ncontinues\n\n{marker('b0004')}\n\nEnd\n"
    clean, anchors = strip_markers(md)
    assert clean == "# Title\n\nPara one\ncontinues\n\nEnd\n"
    assert anchors == {"b0001": [1, 1], "b0002": [3, 4], "b0003": [3, 4], "b0004": [6, 6]}


def test_render_blocks_roundtrip(normalized):
    md = render_blocks(content_blocks(normalized["blocks"]), lambda p: "../assets/x.jpg")
    clean, anchors = strip_markers(md)
    lines = clean.split("\n")
    a, b = anchors["b0014"]
    assert "| Path | Content | In git |" in "\n".join(lines[a - 1 : b])
    assert lines[anchors["b0001"][0] - 1] == "# Flat Document Libraries for Agents"


def test_markdown_only_result(tmp_path):
    from pdfskill.convert import convert
    from pdfskill.normalize import load_result

    md = tmp_path / "full.md"
    md.write_text(
        "# Page title\n\nIntro with <sup>1</sup> and https://example.org/a.\n\n"
        "![a chart](https://cdn.example.org/x.png)\n\n- one\n- two\n\n$$\nx^2\n$$\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n<table><tr><td>h</td></tr><tr><td>v</td></tr></table>\n\n"
        "```python\nprint(1)\n```\n",
        encoding="utf-8",
    )
    norm = load_result(None, None, markdown_path=md)
    types = [b["type"] for b in norm["blocks"]]
    assert types == ["heading", "paragraph", "image", "list", "equation", "table", "table", "code"]
    assert norm["blocks"][1]["text"] == "Intro with ¹ and <https://example.org/a>."
    assert norm["blocks"][2]["images"] == ["https://cdn.example.org/x.png"]
    res = convert(norm["blocks"], lambda p: p, fallback_title="x")
    assert res.report["ok"], res.report
    assert "| a   | b   |" in res.markdown and "| h   |" in res.markdown
