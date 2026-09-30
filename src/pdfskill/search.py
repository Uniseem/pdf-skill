"""Plain-text BM25 search over ``chunks/*.jsonl``.

* Chunk files are committed: deterministic JSONL, one chunk per line, one file
  per document (and per translation).
* The BM25 index is a derived cache in ``.cache/bm25`` (git-ignored). It is
  rebuilt automatically whenever the chunk files, the analyzer or the scoring
  parameters change (fingerprint check), and swapped in atomically.
* Tokenizer: dictionary-free and mixed-language. Han runs become unigrams +
  bigrams (queries use bigrams, so ``钛矿`` finds ``钙钛矿``); Latin words are
  NFKC-normalised, lower-cased, stop-worded and Snowball-stemmed.

Adapted from a design benchmarked against rank-bm25, SQLite FTS5 and tantivy;
bm25s (MIT) does the scoring.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ANALYZER_VERSION = "zh-uni+bi/en-snowball/v1"
CHUNKER_VERSION = "md-heading/v1"
INDEX_RECIPE = "heading_path+body/v1"
PARAMS = {"method": "lucene", "k1": 1.2, "b": 0.75}
logging.getLogger("bm25s").setLevel(logging.WARNING)

_HAN = "㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f"
_TOKEN_RE = re.compile(rf"[{_HAN}]+|[^\W_{_HAN}]+")
_HAN_RE = re.compile(rf"[{_HAN}]")
EN_STOP = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such that the "
    "their then there these they this to was will with".split()
)

try:
    import Stemmer

    _stem = Stemmer.Stemmer("english").stemWord
except ImportError:  # pragma: no cover

    def _stem(w: str) -> str:
        return w


# -- analyzer -----------------------------------------------------------------------------------


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    out, idx = [], []
    for i, ch in enumerate(text):
        n = unicodedata.normalize("NFKC", ch).lower()
        out.append(n)
        idx.extend([i] * len(n))
    return "".join(out), idx


def analyze(text: str, *, query: bool = False, spans: bool = False):
    """Tokens for BM25. With ``spans=True`` return ``(token, start, end)`` into the original text."""
    if spans:
        s, m = _normalize_with_map(text)
    else:
        s, m = unicodedata.normalize("NFKC", text).lower(), None
    toks: list[tuple[str, int, int]] = []
    for mt in _TOKEN_RE.finditer(s):
        w, a = mt.group(), mt.start()
        if _HAN_RE.match(w):
            if len(w) == 1 or not query:
                toks.extend((c, a + i, a + i + 1) for i, c in enumerate(w))
            toks.extend((w[i : i + 2], a + i, a + i + 2) for i in range(len(w) - 1))
        elif w not in EN_STOP:
            toks.append((_stem(w) if w.isascii() else w, a, mt.end()))
    if not spans:
        return [t for t, _, _ in toks]
    return [(t, m[a], m[b - 1] + 1) for t, a, b in toks]


# -- chunker ---------------------------------------------------------------------------------------

_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HANC = re.compile(rf"[{_HAN}]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_SENT = re.compile(r"(?<=[。！？；.!?;])\s*")
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


@dataclass
class MdBlock:
    kind: str  # heading | text | code | math | table | image
    text: str
    line_start: int
    line_end: int
    level: int = 0


def est_tokens(s: str) -> int:
    return len(_HANC.findall(s)) + int(1.3 * len(_WORD.findall(s)))


def md_blocks(md: str):
    buf: list[str] = []
    start, fence, kind, n = 0, None, "text", 0

    def flush(end: int):
        nonlocal buf
        if buf and "".join(buf).strip():
            k = kind
            if k == "text" and all(x.lstrip().startswith("|") for x in buf):
                k = "table"
            elif k == "text" and all(_IMG.fullmatch(x.strip()) for x in buf):
                k = "image"
            yield MdBlock(k, "\n".join(buf), start, end)
        buf = []

    for n, line in enumerate(md.splitlines(), 1):
        if fence:
            buf.append(line)
            if line.strip().startswith(fence) and (fence != "$$" or len(buf) > 1):
                fence = None
                yield from flush(n)
                kind = "text"
            continue
        m = _FENCE.match(line)
        if m or line.strip() == "$$":
            yield from flush(n - 1)
            fence, kind, start, buf = (m.group(1)[:3] if m else "$$"), ("code" if m else "math"), n, [line]
            continue
        m = _ATX.match(line)
        if m:
            yield from flush(n - 1)
            yield MdBlock("heading", m.group(2), n, n, len(m.group(1)))
            continue
        if not line.strip():
            yield from flush(n - 1)
            continue
        if not buf:
            start = n
        buf.append(line)
    yield from flush(n)


def _split_long(b: MdBlock, max_tok: int) -> list[MdBlock]:
    if est_tokens(b.text) <= max_tok or b.kind in {"code", "table", "math"}:
        return [b]
    parts, cur = [], ""
    for sent in _SENT.split(b.text):
        if cur and est_tokens(cur + sent) > max_tok:
            parts.append(cur)
            cur = ""
        cur += sent
    parts.append(cur)
    return [MdBlock(b.kind, p, b.line_start, b.line_end) for p in parts if p.strip()]


def chunk_markdown(
    md: str, chunk_prefix: str, line_pages: dict[int, int] | None = None, *, target: int = 400, max_tok: int = 800
) -> list[dict]:
    """Heading-aware chunks with heading path, page range and Markdown line range."""
    line_pages = line_pages or {}
    stack: list[tuple[int, str]] = []
    cur: list[MdBlock] = []
    out: list[dict] = []

    def page_of(line: int) -> int | None:
        return line_pages.get(line)

    def emit() -> None:
        if not cur:
            return
        text = "\n\n".join(b.text for b in cur).strip()
        pages = [p for b in cur for p in (page_of(b.line_start), page_of(b.line_end)) if p]
        seq = len(out)
        out.append(
            {
                "id": f"{chunk_prefix}#{seq:04d}",
                "seq": seq,
                "heading_path": [h for _, h in stack],
                "pages": [min(pages), max(pages)] if pages else None,
                "lines": [cur[0].line_start, cur[-1].line_end],
                "kinds": sorted({b.kind for b in cur}),
                "sha1": hashlib.sha1(text.encode()).hexdigest()[:12],
                "text": text,
            }
        )
        cur.clear()

    for b in md_blocks(md):
        if b.kind == "heading":
            emit()
            while stack and stack[-1][0] >= b.level:
                stack.pop()
            stack.append((b.level, _IMG.sub(r"\1", b.text)))
            continue
        for piece in _split_long(b, max_tok):
            if cur and est_tokens("".join(x.text for x in cur) + piece.text) > target:
                emit()
            cur.append(piece)
    emit()
    return out


def line_page_map(anchors: dict[str, list[int]], blocks: list[dict], total_lines: int) -> dict[int, int]:
    """Map each Markdown line to a source page, filling gaps with the previous page."""
    page_of_block = {b["id"]: b.get("page") for b in blocks}
    direct: dict[int, int] = {}
    for bid, (a, b) in anchors.items():
        p = page_of_block.get(bid)
        if p:
            for ln in range(a, b + 1):
                direct.setdefault(ln, p)
    result: dict[int, int] = {}
    last = min(direct.values()) if direct else None
    for ln in range(1, total_lines + 1):
        last = direct.get(ln, last)
        if last:
            result[ln] = last
    return result


def dump_chunks(chunks: list[dict], doc_id: str, lang: str | None) -> str:
    lines = []
    for c in chunks:
        row = {
            "id": c["id"],
            "doc_id": doc_id,
            **({"lang": lang} if lang else {}),
            **{k: c[k] for k in ("seq", "heading_path", "pages", "lines", "kinds", "sha1", "text")},
        }
        lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + ("\n" if lines else "")


# -- index -----------------------------------------------------------------------------------------


def _index_text(c: dict) -> str:
    hp = c.get("heading_path") or []
    return "\n".join([" > ".join(hp[1:] if len(hp) > 1 else hp), c["text"]])


class SearchIndex:
    def __init__(self, chunks_dir: Path, index_dir: Path):
        self.chunks_dir = Path(chunks_dir)
        self.index_dir = Path(index_dir)

    def _files(self) -> list[Path]:
        return sorted(self.chunks_dir.glob("*.jsonl"))

    def fingerprint(self) -> str:
        h = hashlib.sha256(json.dumps([ANALYZER_VERSION, INDEX_RECIPE, PARAMS]).encode())
        for p in self._files():
            h.update(p.name.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
        return h.hexdigest()

    def is_fresh(self) -> bool:
        man = self.index_dir / "manifest.json"
        try:
            return json.loads(man.read_text())["fingerprint"] == self.fingerprint()
        except (OSError, ValueError, KeyError):
            return False

    def build(self, force: bool = False) -> bool:
        """Rebuild if stale; return True when a rebuild happened."""
        import bm25s

        fp = self.fingerprint()
        if not force and self.is_fresh():
            return False
        ids, toks, locs = [], [], []
        for p in self._files():
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                c = json.loads(line)
                ids.append(c["id"])
                locs.append([p.name, i])
                toks.append(analyze(_index_text(c)) or ["∅"])
        tmp = self.index_dir.with_name(self.index_dir.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        if ids:
            retriever = bm25s.BM25(**PARAMS)
            retriever.index(toks, show_progress=False)
            retriever.save(tmp)
        (tmp / "ids.json").write_text(json.dumps({"ids": ids, "locs": locs}, ensure_ascii=False))
        (tmp / "manifest.json").write_text(
            json.dumps({"fingerprint": fp, "analyzer": ANALYZER_VERSION, "params": PARAMS, "n_chunks": len(ids)})
        )
        shutil.rmtree(self.index_dir, ignore_errors=True)
        tmp.rename(self.index_dir)
        return True

    def search(
        self,
        query: str,
        *,
        k: int = 8,
        doc_ids: list[str] | None = None,
        lang: str | None = "any",
        max_per_doc: int | None = None,
    ) -> dict:
        import bm25s
        import numpy as np

        rebuilt = self.build()
        meta = json.loads((self.index_dir / "ids.json").read_text())
        ids, locs = meta["ids"], meta["locs"]
        q_all = analyze(query, query=True)
        if not ids:
            return {"query_tokens": q_all, "unknown_terms": q_all, "rebuilt": rebuilt, "hits": []}
        retriever = bm25s.BM25.load(self.index_dir, mmap=True)
        vocab = retriever.vocab_dict
        q = [t for t in q_all if t in vocab]
        unknown = sorted({t for t in q_all if t not in vocab})
        if not q:
            return {"query_tokens": q_all, "unknown_terms": unknown, "rebuilt": rebuilt, "hits": []}
        mask = np.ones(len(ids), dtype=np.float32)
        for i, cid in enumerate(ids):
            prefix = cid.split("#", 1)[0]
            did, _, clang = prefix.partition(".")
            if doc_ids and did not in doc_ids:
                mask[i] = 0
            if lang != "any" and (clang or None) != lang:
                mask[i] = 0
        want = min(len(ids), max(k * 4, k) if max_per_doc else k)
        res, sc = retriever.retrieve([q], k=want, show_progress=False, weight_mask=mask)
        qset, per_doc, hits = set(q), {}, []
        cache: dict[str, list[str]] = {}
        for row, score in zip(res[0], sc[0], strict=True):
            if score <= 0 or len(hits) >= k:
                break
            fname, lineno = locs[row]
            if fname not in cache:
                cache[fname] = (self.chunks_dir / fname).read_text(encoding="utf-8").splitlines()
            c = json.loads(cache[fname][lineno])
            if max_per_doc and per_doc.get(c["doc_id"], 0) >= max_per_doc:
                continue
            per_doc[c["doc_id"]] = per_doc.get(c["doc_id"], 0) + 1
            snip, matched = snippet(c["text"], qset)
            hits.append(
                {
                    "rank": len(hits) + 1,
                    "score": round(float(score), 3),
                    "chunk_id": c["id"],
                    "doc_id": c["doc_id"],
                    "lang": c.get("lang"),
                    "heading": " > ".join((c.get("heading_path") or [])[1:] or (c.get("heading_path") or [])),
                    "pages": c.get("pages"),
                    "lines": c.get("lines"),
                    "kinds": c.get("kinds"),
                    "matched": matched,
                    "snippet": snip,
                }
            )
        return {"query_tokens": q, "unknown_terms": unknown, "rebuilt": rebuilt, "hits": hits}


def snippet(text: str, qtoks: set[str], width: int = 200, mark=("«", "»")) -> tuple[str, list[str]]:
    text = _IMG.sub(lambda m: f"[image: {m.group(1)}]" if m.group(1) else "[image]", text)
    spans = sorted((a, b, t) for t, a, b in analyze(text, spans=True) if t in qtoks)
    merged: list[list[int]] = []
    for a, b, _ in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if not merged:
        return text[:width].replace("\n", " "), []
    best = max(range(len(merged)), key=lambda i: sum(1 for a, _ in merged[i:] if a < merged[i][0] + width))
    lo = max(0, merged[best][0] - width // 4)
    hi = min(len(text), lo + width)
    out, pos = [], lo
    for a, b in merged:
        if a >= lo and b <= hi:
            out += [text[pos:a], mark[0], text[a:b], mark[1]]
            pos = b
    out.append(text[pos:hi])
    s = ("…" if lo else "") + "".join(out).replace("\n", " ") + ("…" if hi < len(text) else "")
    return s, sorted({t for _, _, t in spans})
