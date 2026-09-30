"""Turn a MinerU result into pdfskill's block model.

The block model is what ``docs/<id>.json`` stores and what every later stage
(LLM conversion, translation Markdown, search, ``get``) reads. Blocks keep the
reading order of MinerU's ``content_list.json``; pages are 1-based; ``bbox`` is
MinerU's 0-1000 normalised ``[x0, y0, x1, y1]`` with a top-left origin.

Block types::

    heading    text, level (1-6)
    paragraph  text (inline math as $...$)
    equation   latex (display math, without $$ fences)
    image      images, caption, footnote
    chart      images, caption, footnote, text (MinerU's markdown rendition, may be "")
    table      html, images, caption, footnote
    code       text, lang
    list       items
    footnote   text (page footnotes)
    furniture  text, role  (header/footer/page_number/aside_text: kept for geometry, never rendered)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FURNITURE = {"header", "footer", "page_number", "aside_text", "phonetic"}


_INLINE_MATH = re.compile(r"(?<![\\$])\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)")
_TEXISH = re.compile(r"[\\^_{}=]")


def tidy_math(text: str) -> str:
    """Trim spaces just inside inline math (``$ x^2 $`` -> ``$x^2$``) so strict parsers accept it."""

    def sub(m: re.Match) -> str:
        inner = m.group(1)
        if inner != inner.strip() and _TEXISH.search(inner):
            return f"${inner.strip()}$"
        return m.group(0)

    return _INLINE_MATH.sub(sub, text) if "$" in text else text


_SUP = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ", strict=True))
_SUB = dict(zip("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", strict=True))
_SUPSUB_RE = re.compile(r"<(sup|sub)>(.*?)</\1>", re.I | re.S)
_TAG_MAP = [
    (re.compile(r"</?(?:b|strong)>", re.I), "**"),
    (re.compile(r"</?(?:i|em)>", re.I), "*"),
    (re.compile(r"<br\s*/?>", re.I), " "),
    (re.compile(r"</?(?:u|span|font|mark|small|big|s|del|ins|a|p|div)(?:\s[^<>]*)?>", re.I), ""),
]
_URL_RE = re.compile(r"(?<!\]\()(?<![<\[A-Za-z0-9_/=\"'])(https?://[^\s<>()\[\]$`\"]+)")
_MATH_SYMBOLS = {"†": "\\dagger", "‡": "\\ddagger", "§": "\\S", "¶": "\\P", "∗": "*", "′": "\\prime"}


def _supsub(m: re.Match) -> str:
    kind, inner = m.group(1).lower(), re.sub(r"<[^<>]+>", "", m.group(2)).strip()
    if not inner:
        return ""
    table = _SUP if kind == "sup" else _SUB
    if all(c in table for c in inner):
        return "".join(table[c] for c in inner)
    op = "^" if kind == "sup" else "_"
    if inner.startswith("$") and inner.endswith("$"):
        return f"${op}{{{inner.strip('$')}}}$"
    mapped = "".join(_MATH_SYMBOLS.get(c, c) for c in inner)
    if re.fullmatch(r"(?:[A-Za-z0-9,.*+\-]|\\[A-Za-z]+)+", mapped):
        return f"${op}{{{mapped}}}$"
    safe = inner.replace("\\", "").replace("{", "").replace("}", "").replace("$", "")
    return f"${op}{{\\text{{{safe}}}}}$"


def _autolink(text: str) -> str:
    def sub(m: re.Match) -> str:
        url = m.group(1)
        trail = ""
        while url and url[-1] in ".,;:!?'\"":
            trail = url[-1] + trail
            url = url[:-1]
        return f"<{url}>{trail}" if len(url) > 10 else m.group(0)

    return _URL_RE.sub(sub, text) if "http" in text else text


def clean_inline(text: str) -> str:
    """Make MinerU text strict-Markdown friendly: no inline HTML, no bare URLs, tidy math."""
    if "<" in text:
        text = _SUPSUB_RE.sub(_supsub, text)
        for rx, repl in _TAG_MAP:
            text = rx.sub(repl, text)
    return _autolink(tidy_math(text))


def _join(parts: object) -> str:
    if isinstance(parts, list):
        return clean_inline(" ".join(str(p).strip() for p in parts if str(p).strip()))
    return clean_inline(str(parts or "").strip())


def _strip_display_fences(tex: str) -> str:
    tex = tex.strip()
    for a, b in (("$$", "$$"), ("\\[", "\\]")):
        if tex.startswith(a) and tex.endswith(b) and len(tex) >= len(a) + len(b):
            tex = tex[len(a) : len(tex) - len(b)]
            break
    return tex.strip()


_FENCE_RE = re.compile(r"^```([\w+#.-]*)\s*\n(.*?)\n?```\s*$", re.S)


def _code(item: dict) -> tuple[str, str]:
    body = str(item.get("code_body") or item.get("text") or "")
    m = _FENCE_RE.match(body.strip())
    if m:
        return m.group(2), m.group(1)
    return body.strip("\n"), ""


def page_sizes(middle: dict | None) -> dict[int, list[float]]:
    sizes: dict[int, list[float]] = {}
    for page in (middle or {}).get("pdf_info") or []:
        if "page_idx" in page and page.get("page_size"):
            sizes[int(page["page_idx"])] = list(page["page_size"])
    return sizes


def normalize(content_list: list[dict], middle: dict | None = None, *, page_offset: int = 0) -> dict:
    """Return ``{"pages": [...], "blocks": [...], "parser": {...}}``.

    ``page_offset`` is added to MinerU's ``page_idx`` (which restarts at 0 when a
    page range was requested).
    """
    blocks: list[dict] = []
    for item in content_list:
        kind = item.get("type")
        page = int(item.get("page_idx") or 0) + page_offset + 1
        base = {"page": page}
        if item.get("bbox"):
            base["bbox"] = [int(v) for v in item["bbox"]]
        block: dict | None = None
        if kind == "text":
            text = clean_inline(str(item.get("text") or "").strip())
            if not text:
                continue
            if item.get("_gfm_table"):
                block = {
                    "type": "table",
                    "gfm": str(item.get("text") or ""),
                    "images": [],
                    "caption": "",
                    "footnote": "",
                }
            elif item.get("text_level"):
                block = {"type": "heading", "level": max(1, min(6, int(item["text_level"]))), "text": text}
            else:
                block = {"type": "paragraph", "text": text}
        elif kind == "equation":
            tex = _strip_display_fences(str(item.get("text") or ""))
            if tex:
                block = {"type": "equation", "latex": tex}
        elif kind in {"image", "chart", "seal"}:
            block = {
                "type": "chart" if kind == "chart" else "image",
                "images": [item["img_path"]] if item.get("img_path") else [],
                "caption": _join(item.get(f"{kind}_caption") or item.get("image_caption")),
                "footnote": _join(item.get(f"{kind}_footnote") or item.get("image_footnote")),
            }
            extra = str(item.get("content") or item.get("text") or "").strip()
            if extra:
                block["text"] = extra
            if not (block["images"] or block["caption"] or extra):
                continue
        elif kind == "table":
            block = {
                "type": "table",
                "html": str(item.get("table_body") or ""),
                "images": [item["img_path"]] if item.get("img_path") else [],
                "caption": _join(item.get("table_caption")),
                "footnote": _join(item.get("table_footnote")),
            }
        elif kind == "code":
            text, lang = _code(item)
            if item.get("sub_type") == "algorithm" and not lang:
                lang = "text"
            block = {"type": "code", "text": text, "lang": lang, "caption": _join(item.get("code_caption"))}
        elif kind == "list":
            items = [clean_inline(str(x).strip()) for x in item.get("list_items") or [] if str(x).strip()]
            if items:
                block = {"type": "list", "items": items, "role": item.get("sub_type") or "text"}
        elif kind in {"page_footnote", "ref_text"}:
            text = clean_inline(str(item.get("text") or "").strip())
            if text:
                block = {"type": "footnote" if kind == "page_footnote" else "paragraph", "text": text}
        elif kind in FURNITURE:
            text = str(item.get("text") or "").strip()
            if text:
                block = {"type": "furniture", "role": kind, "text": text}
        else:  # unknown future types: keep any text as a paragraph
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                block = {"type": "paragraph", "text": text, "source_type": kind}
        if block is not None:
            blocks.append({**base, **block})

    for i, b in enumerate(blocks, 1):
        b["id"] = f"b{i:04d}"
        # keep "id" first for readable JSON
        blocks[i - 1] = {"id": b.pop("id"), **b}

    sizes = page_sizes(middle)
    n_pages = max([b["page"] for b in blocks] + [len(sizes) + page_offset] + [0])
    pages = []
    for p in range(1, n_pages + 1):
        entry: dict = {"page": p}
        size = sizes.get(p - 1 - page_offset)
        if size:
            entry["size"] = size
        pages.append(entry)
    parser = {"engine": "mineru"}
    if middle:
        for k_src, k_dst in (("_backend", "backend"), ("_version_name", "version")):
            if middle.get(k_src):
                parser[k_dst] = middle[k_src]
    return {"pages": pages, "blocks": blocks, "parser": parser}


def load_result(
    content_list_path: Path | None, middle_path: Path | None, *, page_offset: int = 0, markdown_path: Path | None = None
) -> dict:
    if content_list_path is None:
        if markdown_path is None:
            raise ValueError("MinerU result has neither content_list.json nor full.md")
        return normalize(markdown_to_content_list(Path(markdown_path).read_text(encoding="utf-8")), None)
    content_list = json.loads(Path(content_list_path).read_text(encoding="utf-8"))
    middle = json.loads(Path(middle_path).read_text(encoding="utf-8")) if middle_path else None
    return normalize(content_list, middle, page_offset=page_offset)


def markdown_to_content_list(md: str) -> list[dict]:
    """Build content_list-like items from a Markdown result (MinerU-HTML returns only full.md)."""
    from markdown_it import MarkdownIt
    from mdit_py_plugins.dollarmath import dollarmath_plugin

    mdit = MarkdownIt("commonmark", {"html": True}).enable("table").use(dollarmath_plugin)
    lines = md.split("\n")
    items: list[dict] = []
    tokens = mdit.parse(md)
    i = 0
    while i < len(tokens):
        t = tokens[i]
        src = "\n".join(lines[t.map[0] : t.map[1]]).strip() if t.map else ""
        if t.type == "heading_open":
            items.append({"type": "text", "text": tokens[i + 1].content, "text_level": int(t.tag[1]), "page_idx": 0})
            i += 3
            continue
        if t.type == "paragraph_open" and t.level == 0:
            inline = tokens[i + 1]
            imgs = [c for c in inline.children or [] if c.type == "image"]
            if (
                imgs
                and len(imgs) == len([c for c in inline.children if c.type not in {"softbreak", "text"}])
                and not "".join(c.content for c in inline.children if c.type == "text").strip()
            ):
                for img in imgs:
                    items.append(
                        {
                            "type": "image",
                            "img_path": img.attrs.get("src", ""),
                            "image_caption": [img.content] if img.content else [],
                            "page_idx": 0,
                        }
                    )
            else:
                items.append({"type": "text", "text": inline.content, "page_idx": 0})
            i += 3
            continue
        if t.type in {"bullet_list_open", "ordered_list_open"} and t.level == 0:
            depth, j, texts = 0, i, []
            while j < len(tokens):
                depth += tokens[j].nesting
                if tokens[j].type == "inline":
                    texts.append(tokens[j].content)
                j += 1
                if depth == 0:
                    break
            items.append({"type": "list", "list_items": texts, "page_idx": 0})
            i = j
            continue
        if t.type == "math_block" and t.level == 0:
            items.append({"type": "equation", "text": t.content, "page_idx": 0})
        elif t.type == "fence" and t.level == 0:
            items.append({"type": "code", "code_body": f"```{t.info}\n{t.content}```", "page_idx": 0})
        elif t.type == "html_block" and "<table" in t.content.lower():
            items.append({"type": "table", "table_body": t.content, "page_idx": 0})
        elif t.type == "table_open":
            j = i
            while tokens[j].type != "table_close":
                j += 1
            items.append({"type": "text", "text": src, "page_idx": 0, "_gfm_table": True})
            i = j + 1
            continue
        elif t.type == "blockquote_open":
            depth, j = 0, i
            while j < len(tokens):
                depth += tokens[j].nesting
                j += 1
                if depth == 0:
                    break
            text = " ".join(x.content for x in tokens[i:j] if x.type == "inline")
            items.append({"type": "text", "text": text, "page_idx": 0})
            i = j
            continue
        i += 1
    return items


def page_offset_from_ranges(page_ranges: str | None) -> int:
    """MinerU restarts page_idx at 0 for a range; recover the offset for "N-M" / "N"."""
    if not page_ranges:
        return 0
    m = re.match(r"^\s*(\d+)", page_ranges)
    return int(m.group(1)) - 1 if m and "," not in page_ranges else 0


def content_blocks(blocks: list[dict]) -> list[dict]:
    """Blocks that belong in the Markdown body (everything except page furniture)."""
    return [b for b in blocks if b["type"] != "furniture"]
