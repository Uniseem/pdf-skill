"""Coarse-to-fine lookups.

* :func:`locate` - coarse: scan the layout JSON files (outline, captions,
  block previews) for a phrase; returns pages, bboxes and Markdown line ranges.
* :func:`get` - fine: read the exact Markdown for a page range, a line range, a
  heading's section, or a search chunk.
"""

from __future__ import annotations

import json
import re
import unicodedata

from .library import Library, LibraryError

TYPE_ALIASES = {
    "figure": {"image", "chart"},
    "image": {"image", "chart"},
    "chart": {"chart"},
    "table": {"table"},
    "equation": {"equation"},
    "formula": {"equation"},
    "heading": {"heading"},
    "code": {"code"},
    "list": {"list"},
    "paragraph": {"paragraph"},
}


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").lower()


def _parse_range(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*(?:[-:]\s*(\d+))?\s*", str(spec))
    if not m:
        raise LibraryError(f"bad range {spec!r}; use N or N-M")
    a = int(m.group(1))
    b = int(m.group(2) or a)
    return (a, b) if a <= b else (b, a)


def _anchor_key(lang: str | None) -> str:
    return "md" if not lang else f"md_{lang}"


def locate(
    lib: Library, query: str, *, doc_ids: list[str] | None = None, kind: str | None = None, limit: int = 20
) -> list[dict]:
    """Case-insensitive phrase search over the JSON layer (no Markdown reads)."""
    q = _fold(query)
    terms = [t for t in re.split(r"\s+", q) if t]
    types = TYPE_ALIASES.get(kind or "", None) if kind else None
    if kind and types is None:
        raise LibraryError(f"unknown type {kind!r}; use one of {', '.join(TYPE_ALIASES)}")
    catalog = lib.catalog()
    hits: list[dict] = []
    for doc_id in sorted(doc_ids or catalog):
        path = lib.doc_json(doc_id)
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for b in doc["blocks"]:
            if b["type"] == "furniture" or (types and b["type"] not in types):
                continue
            text = _fold(b.get("preview", ""))
            if not text:
                continue
            exact = q in text
            if not exact and not all(t in text for t in terms):
                continue
            score = (2 if exact else 1) + (1 if text.startswith(q) else 0) + (1 if b["type"] == "heading" else 0)
            hits.append(
                {
                    "doc_id": doc_id,
                    "title": doc.get("title"),
                    "block": b["id"],
                    "type": b["type"],
                    "page": b["page"],
                    "bbox": b.get("bbox"),
                    "lines": b.get("md"),
                    "preview": b.get("preview"),
                    "score": score,
                }
            )
    hits.sort(key=lambda h: (-h["score"], h["doc_id"], h["page"]))
    return hits[:limit]


def outline(lib: Library, doc_id: str) -> dict:
    doc = lib.load_doc(doc_id)
    return {
        "doc_id": doc_id,
        "title": doc.get("title"),
        "pages": len(doc.get("pages", [])),
        "outline": doc.get("outline", []),
        "translations": sorted(doc.get("translations", {})),
    }


def _section_range(doc: dict, heading: str, key: str, total: int) -> tuple[int, int, dict]:
    h = _fold(heading)
    entries = [o for o in doc.get("outline", []) if o.get("md")]
    if key != "md":
        by_line = {tuple(b["md"]): b for b in doc["blocks"] if b.get("md") and b["type"] == "heading"}
        entries = []
        for o in doc.get("outline", []):
            b = by_line.get(tuple(o.get("md") or []))
            if b and b.get(key):
                entries.append({**o, "md": b[key]})
    idx = next((i for i, o in enumerate(entries) if _fold(o["text"]) == h), None)
    if idx is None:
        idx = next((i for i, o in enumerate(entries) if h in _fold(o["text"])), None)
    if idx is None:
        raise LibraryError(f"no heading matching {heading!r}")
    start = entries[idx]["md"][0]
    level = entries[idx]["level"]
    end = total
    for o in entries[idx + 1 :]:
        if o["level"] <= level:
            end = o["md"][0] - 1
            break
    return start, end, entries[idx]


def get(
    lib: Library,
    doc_id: str,
    *,
    pages: str | None = None,
    lines: str | None = None,
    heading: str | None = None,
    chunk: str | None = None,
    lang: str | None = None,
    max_chars: int = 20000,
) -> dict:
    doc = lib.load_doc(doc_id)
    md_path = lib.doc_md(doc_id, lang)
    if not md_path.exists():
        raise LibraryError(f"{doc_id} has no {'translation ' + lang if lang else 'Markdown'}")
    md_lines = md_path.read_text(encoding="utf-8").split("\n")
    total = len(md_lines)
    key = _anchor_key(lang)
    meta: dict = {}
    if chunk:
        prefix = chunk.split("#", 1)[0]
        cf = lib.chunks_dir / f"{prefix}.jsonl"
        row = None
        if cf.exists():
            for line in cf.read_text(encoding="utf-8").splitlines():
                if line.startswith('{"id":"' + chunk + '"'):
                    row = json.loads(line)
                    break
        if not row:
            raise LibraryError(f"no chunk {chunk}")
        a, b = row["lines"]
        meta = {"chunk": chunk, "heading": " > ".join(row.get("heading_path") or [])}
    elif heading:
        a, b, entry = _section_range(doc, heading, key, total)
        meta = {"heading": entry["text"], "level": entry["level"]}
    elif pages:
        pr = _parse_range(pages)
        spans = [blk[key] for blk in doc["blocks"] if blk.get(key) and pr[0] <= blk["page"] <= pr[1]]
        if not spans:
            raise LibraryError(f"no Markdown content on page(s) {pages}")
        a, b = min(s[0] for s in spans), max(s[1] for s in spans)
        meta = {"pages": list(pr)}
    elif lines:
        a, b = _parse_range(lines)
    else:
        a, b = 1, total
    a, b = max(1, a), min(total, b)
    text = "\n".join(md_lines[a - 1 : b]).strip("\n")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
        cut = text.rfind("\n")
        text = text[:cut] if cut > max_chars * 0.5 else text
        b = a + text.count("\n")
    page_hits = sorted({blk["page"] for blk in doc["blocks"] if blk.get(key) and blk[key][0] <= b and blk[key][1] >= a})
    return {
        "doc_id": doc_id,
        "title": doc.get("title"),
        "lang": lang,
        "file": lib.rel(md_path),
        "lines": [a, b],
        "pages": [page_hits[0], page_hits[-1]] if page_hits else None,
        "truncated": truncated,
        **meta,
        "text": text,
    }
