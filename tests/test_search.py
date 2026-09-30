import json

from pdfskill.search import SearchIndex, analyze, chunk_markdown, dump_chunks, line_page_map, snippet


def test_analyzer_mixed_language():
    toks = analyze("钙钛矿 Solar cells running")
    assert "钛矿" in toks and "钙" in toks and "cell" in toks and "run" in toks
    q = analyze("钛矿", query=True)
    assert q == ["钛矿"]
    assert analyze("锂", query=True) == ["锂"]
    assert "the" not in analyze("the model")


def test_snippet_highlights_original_text():
    s, matched = snippet("钙钛矿太阳能电池的 Efficiency is high.", set(analyze("钛矿 efficiency", query=True)))
    assert "«钛矿»" in s and "«Efficiency»" in s
    assert set(matched) == {"钛矿", "effici"}


MD = """# Doc

## 1 Intro

First paragraph about BM25 ranking.

```python
# not a heading
x = 1
```

## 2 方法

扁平化存储意味着所有文档都放在同一层目录中。

$$
E = mc^2
$$
"""


def test_chunker_heading_paths_and_pages():
    pages = {i: (1 if i < 12 else 2) for i in range(1, 20)}
    chunks = chunk_markdown(MD, "doc1", pages)
    assert [c["heading_path"] for c in chunks] == [["Doc", "1 Intro"], ["Doc", "2 方法"]]
    assert chunks[0]["kinds"] == ["code", "text"]
    assert chunks[0]["pages"] == [1, 1] and chunks[1]["pages"] == [2, 2]
    assert chunks[1]["lines"][0] == 14
    row = json.loads(dump_chunks(chunks, "doc1", "zh").splitlines()[0])
    assert list(row)[:3] == ["id", "doc_id", "lang"]


def test_line_page_map_fills_gaps():
    m = line_page_map({"b1": [1, 2], "b2": [5, 6]}, [{"id": "b1", "page": 1}, {"id": "b2", "page": 3}], 8)
    assert m == {1: 1, 2: 1, 3: 1, 4: 1, 5: 3, 6: 3, 7: 3, 8: 3}


def test_index_build_search_filters(tmp_path):
    cdir = tmp_path / "chunks"
    cdir.mkdir()
    (cdir / "aaaa.jsonl").write_text(dump_chunks(chunk_markdown(MD, "aaaa", {}), "aaaa", None), encoding="utf-8")
    other = "# Other\n\n## Cooking\n\nPasta needs salt and water.\n"
    (cdir / "bbbb.zh.jsonl").write_text(
        dump_chunks(chunk_markdown(other, "bbbb.zh", {}), "bbbb", "zh"), encoding="utf-8"
    )
    idx = SearchIndex(cdir, tmp_path / "idx")
    assert idx.build() is True and idx.build() is False  # fingerprint -> no rebuild
    res = idx.search("扁平 存储")
    assert res["hits"][0]["doc_id"] == "aaaa" and "«扁平»" in res["hits"][0]["snippet"]
    assert idx.search("pasta", lang=None)["hits"] == []  # original-language only
    assert idx.search("pasta", lang="zh")["hits"][0]["lang"] == "zh"
    assert idx.search("pasta", doc_ids=["aaaa"])["hits"] == []
    res = idx.search("zzzunknownzzz")
    assert res["hits"] == [] and res["unknown_terms"] == ["zzzunknownzzz"]
    (cdir / "bbbb.zh.jsonl").unlink()
    assert idx.search("pasta")["rebuilt"] is True
