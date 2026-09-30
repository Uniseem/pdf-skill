"""Deterministic Markdown rendering and block anchors.

Every rendered block is preceded by a marker line ``<!-- @b0001 -->``. The LLM
converter asks the model to keep the same markers, so both paths share
:func:`strip_markers`, which removes the markers and returns a map from block
id to the 1-based line range it produced in the final Markdown. That map is
stored in ``docs/<id>.json`` and is how page-level queries find the matching
Markdown text.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

MARKER_RE = re.compile(r"^<!--\s*@((?:b\d{4,}[\s,]*)+)-->\s*$")
_ID_RE = re.compile(r"b\d{4,}")


def marker(ids: list[str] | str) -> str:
    if isinstance(ids, str):
        ids = [ids]
    return f"<!-- @{' '.join(ids)} -->"


# -- escaping -----------------------------------------------------------------------

_LINE_START_RE = re.compile(r"^(\s{0,3})(#{1,6}(?=\s|$)|>|[-+*](?=\s)|=+\s*$|-{3,}\s*$|(\d{1,9})([.)])(?=\s|$))")


def escape_block_start(text: str) -> str:
    """Stop plain text from being parsed as a heading, list, quote or rule."""

    def fix(m: re.Match) -> str:
        indent, tok = m.group(1), m.group(2)
        if m.group(3):
            return f"{indent}{m.group(3)}\\{m.group(4)}"
        return f"{indent}\\{tok}"

    return _LINE_START_RE.sub(fix, text, count=1)


def one_line(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ", text or "").strip()


def escape_alt(text: str) -> str:
    return one_line(text).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


# -- HTML tables -> GFM ------------------------------------------------------------------


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int, bool]]] = []
        self._cell: list[str] | None = None
        self._span = (1, 1)
        self._header = False
        self.nested = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and self.rows or tag == "table" and self._cell is not None:
            self.nested += 1
        if tag == "tr":
            self.rows.append([])
        elif tag in {"td", "th"}:
            if not self.rows:
                self.rows.append([])
            self._cell = []
            self._header = tag == "th"
            try:
                self._span = (max(1, int(a.get("rowspan") or 1)), max(1, int(a.get("colspan") or 1)))
            except ValueError:
                self._span = (1, 1)
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None:
            text = one_line("".join(self._cell))
            self.rows[-1].append((text, self._span[0], self._span[1], self._header))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_grid(table_html: str) -> list[list[str]]:
    """Parse an HTML table into a rectangular grid, duplicating merged cells."""
    p = _TableParser()
    p.feed(table_html or "")
    grid: dict[tuple[int, int], str] = {}
    for r, row in enumerate(p.rows):
        c = 0
        for text, rowspan, colspan, _ in row:
            while (r, c) in grid:
                c += 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    grid[(r + dr, c + dc)] = text
            c += colspan
    if not grid:
        return []
    n_rows = max(r for r, _ in grid) + 1
    n_cols = max(c for _, c in grid) + 1
    return [[grid.get((r, c), "") for c in range(n_cols)] for r in range(n_rows)]


_CELL_MATH = re.compile(r"(\$\$.+?\$\$|\$[^$]+?\$|\\\(.+?\\\))", re.S)


def _cell(text: str) -> str:
    """One GFM cell: escape pipes in text; inside math use \\vert/\\Vert (a table pipe would split the cell)."""
    out = []
    for i, part in enumerate(_CELL_MATH.split(one_line(text))):
        if i % 2:
            body = part.strip("$") if part.startswith("$") else part[2:-2]
            body = body.replace("\\|", "\\Vert ").replace("|", "\\vert ").strip()
            out.append(f"${body}$")
        else:
            out.append(part.replace("|", "\\|"))
    return "".join(out).strip() or " "


def grid_to_gfm(grid: list[list[str]]) -> str:
    if not grid:
        return ""
    header, *body = grid
    lines = ["| " + " | ".join(_cell(c) for c in header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in body]
    return "\n".join(lines)


def html_table_to_gfm(table_html: str) -> str:
    return grid_to_gfm(html_table_to_grid(table_html))


# -- block rendering ------------------------------------------------------------------------


def _fence(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def render_block(block: dict, image_ref, *, text_key: str = "text") -> str:
    """Render one block to Markdown (without its marker).

    ``image_ref`` maps a stored image path to the link written in Markdown.
    ``text_key`` lets the translation renderer substitute translated text.
    """
    t = block["type"]
    text = block.get(text_key) if block.get(text_key) is not None else block.get("text", "")
    if t == "heading":
        return f"{'#' * block.get('level', 2)} {one_line(text)}"
    if t in {"paragraph", "footnote"}:
        return escape_block_start(one_line(text))
    if t == "equation":
        return f"$$\n{block['latex'].strip()}\n$$"
    if t in {"image", "chart", "table"}:
        caption = block.get(f"caption_{text_key}") if text_key != "text" else None
        caption = one_line(caption or block.get("caption", ""))
        footnote = block.get(f"footnote_{text_key}") if text_key != "text" else None
        footnote = one_line(footnote or block.get("footnote", ""))
        parts: list[str] = []
        if t == "table":
            if caption:
                parts.append(escape_block_start(caption))
            table = block.get("gfm") or html_table_to_gfm(block.get("html", ""))
            if table:
                parts.append(table)
            else:
                parts += [f"![{escape_alt(caption) or 'table'}]({image_ref(p)})" for p in block.get("images", [])]
        else:
            parts += [f"![{escape_alt(caption) or t}]({image_ref(p)})" for p in block.get("images", [])]
            if caption:
                parts.append(escape_block_start(caption))
            if t == "chart" and block.get("text"):
                parts.append(block["text"].strip())
        if footnote:
            parts.append(escape_block_start(footnote))
        return "\n\n".join(parts)
    if t == "code":
        body = _fence(block.get("text", ""), block.get("lang", ""))
        cap = one_line(block.get("caption", ""))
        return f"{escape_block_start(cap)}\n\n{body}" if cap else body
    if t == "list":
        items = block.get(f"items_{text_key}") if text_key != "text" else None
        return "\n".join(f"- {escape_block_start(one_line(i))}" for i in (items or block.get("items", [])))
    return escape_block_start(one_line(text))


def render_blocks(blocks: list[dict], image_ref, *, text_key: str = "text") -> str:
    """Render blocks with markers, separated by blank lines."""
    out: list[str] = []
    for b in blocks:
        body = render_block(b, image_ref, text_key=text_key)
        if body.strip():
            out.append(f"{marker(b['id'])}\n\n{body}")
    return "\n\n".join(out) + "\n"


# -- markers -> anchors ------------------------------------------------------------------------


def strip_markers(md: str) -> tuple[str, dict[str, list[int]]]:
    """Remove marker lines; return clean Markdown and ``{block_id: [first, last]}``.

    Line numbers are 1-based and refer to the returned Markdown. A block's range
    runs from the first line after its marker to the last non-blank line before
    the next marker (or the end of the document). Ids that share one marker
    (because the LLM merged their content) share the range.
    """
    lines = md.split("\n")
    out: list[str] = []
    spans: list[tuple[list[str], int]] = []
    skip_blank = False
    for line in lines:
        m = MARKER_RE.match(line.strip())
        if m:
            spans.append((_ID_RE.findall(m.group(1)), len(out) + 1))
            skip_blank = True
            continue
        if skip_blank and not line.strip():
            continue
        skip_blank = False
        out.append(line)
    # collapse runs of blank lines left behind by removed markers
    clean: list[str] = []
    remap: dict[int, int] = {}
    for i, line in enumerate(out, 1):
        if not line.strip() and clean and not clean[-1].strip():
            remap[i] = len(clean)
            continue
        clean.append(line)
        remap[i] = len(clean)
    while clean and not clean[-1].strip():
        clean.pop()
    total = len(clean)
    anchors: dict[str, list[int]] = {}
    for k, (ids, start) in enumerate(spans):
        end_raw = spans[k + 1][1] - 1 if k + 1 < len(spans) else len(out)
        first = remap.get(start, total + 1)
        last = remap.get(end_raw, total) if end_raw >= start else first - 1
        while last >= first and last <= total and not clean[last - 1].strip():
            last -= 1
        if last < first:
            continue
        for bid in ids:
            anchors.setdefault(bid, [first, min(last, total)])
    return "\n".join(clean) + "\n", anchors


def unescape_html(text: str) -> str:
    return html.unescape(text)
