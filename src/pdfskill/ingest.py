"""Ingest orchestration: file -> MinerU -> blocks -> Markdown -> library (+ translation)."""

from __future__ import annotations

import datetime as _dt
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import translate as tr
from .config import Settings
from .convert import convert, render_plain
from .library import Library, doc_id_for, sha256_file
from .llm import LLM, resolve
from .markdown import one_line
from .mineru import MinerUClient, ParseOptions, ResultFiles, locate_result_files
from .normalize import content_blocks, load_result, page_offset_from_ranges
from .search import SearchIndex, chunk_markdown, dump_chunks, line_page_map
from .validate import validate

SCHEMA = "pdfskill.doc/1"
META_FILE = "_pdfskill_meta.json"


class IngestError(RuntimeError):
    pass


@dataclass
class IngestOptions:
    translate: str | None = None
    use_llm: bool = True
    force: bool = False
    reparse: bool = False
    commit: bool = True
    page_ranges: str | None = None
    language: str | None = None
    is_ocr: bool | None = None
    model_version: str | None = None
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    translate_options: dict = field(default_factory=dict)


def _today() -> str:
    return _dt.date.today().isoformat()


def _preview(b: dict, n: int = 100) -> str:
    t = b["type"]
    if t == "equation":
        return b.get("latex", "")[:n]
    if t in {"image", "chart", "table", "code"}:
        return one_line(b.get("caption") or b.get("text") or "")[:n]
    if t == "list":
        return one_line(" ".join(b.get("items", [])))[:n]
    return one_line(b.get("text", ""))[:n]


# -- MinerU ----------------------------------------------------------------------------------------


def parse_with_mineru(
    lib: Library, path: Path, doc_id: str, settings: Settings, opts: IngestOptions, progress
) -> tuple[ResultFiles, dict]:
    raw = lib.raw_cache(doc_id)
    meta_path = raw / META_FILE
    if raw.exists() and not opts.reparse and meta_path.exists():
        files = locate_result_files(raw)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (files.content_list or files.full_md) and meta.get("page_ranges") == opts.page_ranges:
            progress("reusing cached MinerU result")
            return files, meta
    m = settings.mineru
    popts = ParseOptions(
        model_version=opts.model_version or m.model_version,
        language=opts.language or m.language,
        is_ocr=m.is_ocr if opts.is_ocr is None else opts.is_ocr,
        page_ranges=opts.page_ranges,
    )
    tmp = raw.with_name(raw.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with MinerUClient(m.token, api=m.api, progress=progress) as client:
        progress(f"MinerU ({m.api}, {popts.model_version}) parsing {path.name}")
        client.parse_file(path, tmp, popts)
    meta = {
        "api": m.api,
        "model_version": popts.model_version,
        "language": popts.language,
        "is_ocr": popts.is_ocr,
        "page_ranges": opts.page_ranges,
        "parsed": _today(),
    }
    (tmp / META_FILE).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    shutil.rmtree(raw, ignore_errors=True)
    raw.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(raw)
    files = locate_result_files(raw)
    if not files.content_list and not files.full_md:
        raise IngestError(f"MinerU result for {path.name} has no content_list.json or full.md (see {raw})")
    return files, meta


# -- images ----------------------------------------------------------------------------------------


def localize_images(lib: Library, blocks: list[dict], raw_dir: Path, progress) -> dict[str, str]:
    """Copy every referenced image into assets/ (downloading remote/CDN URLs); return {ref: asset}."""
    mapping: dict[str, str] = {}
    for b in blocks:
        for ref in b.get("images", []):
            if ref in mapping:
                continue
            if ref.startswith(("http://", "https://")):
                try:
                    resp = httpx.get(ref, follow_redirects=True, timeout=60)
                    resp.raise_for_status()
                    suffix = Path(ref.split("?", 1)[0]).suffix or ".jpg"
                    mapping[ref] = lib.add_asset_bytes(resp.content, suffix)
                except httpx.HTTPError as exc:
                    progress(f"warning: could not download image {ref.split('?', 1)[0]}: {exc}")
                continue
            src = raw_dir / ref
            if not src.exists():
                cands = list(raw_dir.rglob(Path(ref).name))
                src = cands[0] if cands else src
            if src.exists():
                mapping[ref] = lib.add_asset(src)
            else:
                progress(f"warning: image {ref} missing from MinerU result")
    for b in blocks:
        if b.get("images"):
            b["images"] = [mapping[r] for r in b["images"] if r in mapping]
    return mapping


def image_ref(asset: str) -> str:
    return "../" + asset


# -- doc JSON --------------------------------------------------------------------------------------


def build_doc_json(
    doc_id: str,
    title: str,
    source: dict,
    parser: dict,
    pages: list[dict],
    shaped: list[dict],
    furniture: list[dict],
    anchors: dict[str, list[int]],
    conversion: dict,
) -> dict:
    blocks = []
    order = {b["id"]: i for i, b in enumerate(shaped)}
    for b in sorted(shaped + furniture, key=lambda x: (order.get(x["id"], 10**9), x["id"])):
        if b.get("synthetic"):
            continue
        entry = {"id": b["id"], "page": b["page"], "type": b["type"]}
        if b["type"] == "heading":
            entry["level"] = b["level"]
        if b["type"] == "furniture":
            entry["role"] = b.get("role")
        if b.get("bbox"):
            entry["bbox"] = b["bbox"]
        if b.get("images"):
            entry["images"] = b["images"]
        entry["preview"] = _preview(b)
        if b["id"] in anchors:
            entry["md"] = anchors[b["id"]]
        blocks.append(entry)
    outline = []
    for b in shaped:
        if b["type"] == "heading":
            item = {"level": b["level"], "text": b["text"], "page": b["page"]}
            if b["id"] in anchors:
                item["md"] = anchors[b["id"]]
            outline.append(item)
    assets = sorted({a for b in shaped for a in b.get("images", [])})
    return {
        "schema": SCHEMA,
        "doc_id": doc_id,
        "title": title,
        "source": source,
        "parser": parser,
        "conversion": conversion,
        "pages": pages,
        "outline": outline,
        "assets": assets,
        "translations": {},
        "blocks": blocks,
    }


def restore_shaped(lib: Library, doc_id: str, raw: ResultFiles, meta: dict) -> tuple[list[dict], dict]:
    """Rebuild shaped blocks (headings/levels as stored) from the raw MinerU result."""
    doc = lib.load_doc(doc_id)
    norm = load_result(
        raw.content_list,
        raw.middle,
        page_offset=page_offset_from_ranges(meta.get("page_ranges")),
        markdown_path=raw.full_md,
    )
    stored = {b["id"]: b for b in doc["blocks"]}
    blocks = content_blocks(norm["blocks"])
    localize_images(lib, blocks, raw.root, lambda m: None)
    shaped = []
    if (
        doc["outline"]
        and doc["outline"][0]["level"] == 1
        and not any(b.get("level") == 1 and b["type"] == "heading" for b in doc["blocks"])
    ):
        shaped.append(
            {
                "id": "b0000",
                "type": "heading",
                "level": 1,
                "text": doc["title"],
                "page": blocks[0]["page"] if blocks else 1,
                "synthetic": True,
            }
        )
    for b in blocks:
        s = stored.get(b["id"])
        if s:
            b = dict(b)
            if s["type"] == "heading":
                b["type"], b["level"] = "heading", s["level"]
            elif b["type"] == "heading":
                b["type"] = "paragraph"
                b.pop("level", None)
        shaped.append(b)
    first_h1 = next((i for i, b in enumerate(shaped) if b["type"] == "heading" and b.get("level") == 1), None)
    if first_h1:
        shaped.insert(0, shaped.pop(first_h1))
    return shaped, doc


# -- main entry ------------------------------------------------------------------------------------


def ingest(lib: Library, path: Path, settings: Settings, opts: IngestOptions, progress=lambda m: None) -> dict:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"not a file: {path}")
    lib.require()
    sha = sha256_file(path)
    doc_id = doc_id_for(sha)
    entry = lib.catalog().get(doc_id)
    lang = tr.normalize_target(opts.translate) if opts.translate else None
    need_ingest = opts.force or opts.reparse or entry is None or not lib.doc_md(doc_id).exists()
    need_translate = bool(lang) and (opts.force or lang not in ((entry or {}).get("translations") or {}))
    result: dict = {"doc_id": doc_id, "source": path.name, "status": "exists"}
    if not need_ingest and not need_translate:
        result["title"] = entry.get("title")
        return result
    if lang and path.suffix.lower() != ".pdf":
        raise IngestError("translation needs a PDF input (retain-pdf renders over the original PDF)")
    if lang and not tr.status()["ready"]:
        raise IngestError("translation is not set up; run `pdfskill setup translate` first")

    resolved = resolve(settings.llm) if (opts.use_llm or lang) else None
    if lang and resolved is None:
        raise IngestError("translation needs an LLM; configure one (see `pdfskill doctor`)")
    llm = None
    if opts.use_llm and resolved is not None:
        llm = LLM(
            resolved,
            cache_path=lib.cache_dir / "llm-cache.sqlite",
            timeout=settings.llm.timeout,
            temperature=settings.llm.temperature,
        )

    raw, meta = parse_with_mineru(lib, path, doc_id, settings, opts, progress)
    touched: list[str] = ["catalog.jsonl"]

    if need_ingest:
        norm = load_result(
            raw.content_list,
            raw.middle,
            page_offset=page_offset_from_ranges(opts.page_ranges),
            markdown_path=raw.full_md,
        )
        blocks = content_blocks(norm["blocks"])
        furniture = [b for b in norm["blocks"] if b["type"] == "furniture"]
        localize_images(lib, blocks, raw.root, progress)
        progress(f"converting {len(blocks)} blocks" + (f" with {resolved.label}" if llm else " (no LLM)"))
        res = convert(
            blocks,
            image_ref,
            llm=llm,
            fallback_title=opts.title or path.stem,
            max_chunk_chars=settings.llm.max_chunk_chars,
            concurrency=settings.llm.concurrency,
            progress=progress,
        )
        title = opts.title or res.title
        lib.doc_md(doc_id).parent.mkdir(parents=True, exist_ok=True)
        lib.doc_md(doc_id).write_text(res.markdown, encoding="utf-8")
        parser = {**norm["parser"], "api": meta.get("api"), "model_version": meta.get("model_version")}
        source = {"name": path.name, "sha256": sha, "bytes": path.stat().st_size, "suffix": path.suffix.lower()}
        conversion = {"llm": resolved.label if llm else None, "stats": res.stats.as_dict(), "validation": res.report}
        doc = build_doc_json(
            doc_id, title, source, parser, norm["pages"], res.blocks, furniture, res.anchors, conversion
        )
        old = lib.doc_json(doc_id)
        if old.exists():
            # Re-conversion of the same MinerU result keeps block ids stable: carry translation anchors over.
            old_doc = json.loads(old.read_text(encoding="utf-8"))
            doc["translations"] = old_doc.get("translations", {})
            old_blocks = {b["id"]: b for b in old_doc.get("blocks", [])}
            for b in doc["blocks"]:
                for k, v in old_blocks.get(b["id"], {}).items():
                    if k.startswith("md_"):
                        b[k] = v
        lib.save_doc(doc)
        n_lines = res.markdown.count("\n")
        chunks = chunk_markdown(res.markdown, doc_id, line_page_map(res.anchors, res.blocks, n_lines))
        lib.chunks_file(doc_id).parent.mkdir(parents=True, exist_ok=True)
        lib.chunks_file(doc_id).write_text(dump_chunks(chunks, doc_id, None), encoding="utf-8")
        entry = {
            **(entry or {}),
            "id": doc_id,
            "title": title,
            "source_name": path.name,
            "sha256": sha,
            "bytes": path.stat().st_size,
            "suffix": path.suffix.lower(),
            "pages": len(norm["pages"]),
            "llm": resolved.label if llm else None,
            "valid": res.report["ok"],
            "tags": sorted(set(opts.tags)),
        }
        entry.setdefault("added", _today())
        entry.setdefault("translations", {})
        lib.upsert(entry)
        shaped = res.blocks
        result.update(
            {
                "status": "ingested",
                "title": title,
                "pages": len(norm["pages"]),
                "blocks": len(blocks),
                "chunks": len(chunks),
                "markdown": lib.rel(lib.doc_md(doc_id)),
                "json": lib.rel(lib.doc_json(doc_id)),
                "validation": res.report,
                "conversion": res.stats.as_dict(),
                "llm": llm.stats() if llm else None,
            }
        )
        touched += ["docs", "chunks", "assets"]
    else:
        shaped, doc = restore_shaped(lib, doc_id, raw, meta)
        title = doc["title"]
        result["title"] = title

    if need_translate:
        result["translation"] = run_translation(
            lib, doc_id, path, raw, shaped, resolved, lang, settings, opts, progress
        )
        touched += ["docs", "chunks"]

    SearchIndex(lib.chunks_dir, lib.cache_dir / "bm25").build()
    if opts.commit:
        verb = "ingest" if need_ingest else f"translate({lang})"
        sha_c = lib.commit(f"{verb}: {title} ({doc_id})", sorted(set(touched)))
        result["commit"] = sha_c
    return result


def run_translation(
    lib: Library,
    doc_id: str,
    pdf: Path,
    raw: ResultFiles,
    shaped: list[dict],
    resolved,
    lang: str,
    settings: Settings,
    opts: IngestOptions,
    progress,
) -> dict:
    out = tr.run(
        job=lib.cache_dir / "translate" / doc_id,
        source_pdf=pdf,
        raw_dir=raw.root,
        llm=resolved,
        options=opts.translate_options,
        progress=progress,
    )
    dest_pdf = lib.doc_pdf(doc_id, lang)
    shutil.copyfile(out["pdf"], dest_pdf)
    doc = lib.load_doc(doc_id)
    sizes = {p["page"]: p["size"] for p in doc["pages"] if p.get("size")}
    blocks = [dict(b) for b in shaped]
    match = tr.apply_translations(blocks, out["items"], sizes)
    md, anchors = render_plain(blocks, image_ref, text_key="text_tr")
    final, report = validate(md)
    if final.count("\n") == md.count("\n"):
        md = final
    md_path = lib.doc_md(doc_id, lang)
    md_path.write_text(md, encoding="utf-8")
    n_lines = md.count("\n")
    chunks = chunk_markdown(md, f"{doc_id}.{lang}", line_page_map(anchors, blocks, n_lines))
    lib.chunks_file(doc_id, lang).write_text(dump_chunks(chunks, doc_id, lang), encoding="utf-8")
    key = f"md_{lang}"
    for b in doc["blocks"]:
        if b["id"] in anchors:
            b[key] = anchors[b["id"]]
        else:
            b.pop(key, None)
    info = {
        "pdf": lib.rel(dest_pdf),
        "markdown": lib.rel(md_path),
        "engine": "retain-pdf",
        "llm": resolved.label,
        "match": match,
        "validation": report.as_dict(),
    }
    doc.setdefault("translations", {})[lang] = info
    lib.save_doc(doc)
    entries = lib.catalog()
    e = entries.get(doc_id, {"id": doc_id})
    e.setdefault("translations", {})[lang] = {"pdf": info["pdf"], "markdown": info["markdown"]}
    entries[doc_id] = e
    lib.write_catalog(entries)
    return {**info, "chunks": len(chunks)}


def rechunk(lib: Library, doc_id: str) -> int:
    """Regenerate chunk files from the stored Markdown + JSON (e.g. after hand edits)."""
    doc = lib.load_doc(doc_id)
    total = 0
    for lang in [None, *doc.get("translations", {})]:
        md_path = lib.doc_md(doc_id, lang)
        if not md_path.exists():
            continue
        md = md_path.read_text(encoding="utf-8")
        key = "md" if lang is None else f"md_{lang}"
        anchors = {b["id"]: b[key] for b in doc["blocks"] if key in b}
        chunks = chunk_markdown(
            md, doc_id if lang is None else f"{doc_id}.{lang}", line_page_map(anchors, doc["blocks"], md.count("\n"))
        )
        lib.chunks_file(doc_id, lang).write_text(dump_chunks(chunks, doc_id, lang), encoding="utf-8")
        total += len(chunks)
    return total
