"""pdfskill command line. Data goes to stdout (text, or JSON with --json); progress to stderr."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from . import translate as tr
from .config import config_path, load_settings
from .library import Library, LibraryError
from .llm import PROVIDERS, LLMError, resolve

EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3
GUIDE = Path(__file__).parent / "guide.md"

CONFIG_TEMPLATE = """\
# pdfskill user config (secrets allowed here; never put them in the library repo)

[library]
# path = "~/pdfskill-library"

[mineru]
# token = ""            # https://mineru.net/apiManage/token (or env MINERU_TOKEN)
# api = "v4"            # "v4" with a token; "v1" = anonymous, rate limited
# model_version = "vlm" # "vlm" | "pipeline"
# language = "ch"       # OCR language hint: ch, en, japan, korean, latin, ...

[llm]
# provider = "deepseek" # run `pdfskill providers` for the list
# model = "deepseek-chat"
# api_key = ""          # or the provider's env var (DEEPSEEK_API_KEY, DASHSCOPE_API_KEY, ...)
# base_url = ""         # any OpenAI-compatible endpoint
# concurrency = 4

[translate]
# target_lang = "zh"
"""


def _progress(quiet: bool):
    start = time.time()

    def emit(msg: str) -> None:
        if not quiet:
            print(f"[{time.time() - start:6.1f}s] {msg}", file=sys.stderr, flush=True)

    return emit


def _out(args, data, text: str | None = None) -> None:
    if args.json or text is None:
        print(json.dumps(data, ensure_ascii=False, indent=None if args.json else 2))
    else:
        print(text)


def _lib(args, settings) -> Library:
    return Library.find(Path(args.library) if args.library else None, settings.library)


def _mask(v: str | None) -> str | None:
    if not v:
        return None
    return v[:4] + "…" + v[-2:] if len(v) > 10 else "***"


# -- commands ------------------------------------------------------------------------------------------


def cmd_init(args, settings) -> int:
    lib = _lib(args, settings)
    created = lib.init(git=not args.no_git)
    _out(
        args,
        {"library": str(lib.root), "created": created},
        f"library: {lib.root}\ncreated: {', '.join(created) or 'nothing (already initialised)'}",
    )
    return EXIT_OK


def cmd_doctor(args, settings) -> int:
    lib = _lib(args, settings)
    import shutil

    report: dict = {
        "version": __version__,
        "config_file": str(config_path()),
        "config_file_exists": config_path().exists(),
    }
    report["library"] = {
        "path": str(lib.root),
        "initialised": lib.exists,
        "git": lib.is_git,
        "documents": len(lib.catalog()) if lib.exists else 0,
    }
    m = settings.mineru
    mineru = {"api": m.api, "token": bool(m.token), "model_version": m.model_version}
    if m.token:
        from .mineru import token_expiry

        exp = token_expiry(m.token)
        if exp:
            mineru["token_days_left"] = round((exp - time.time()) / 86400, 1)
    if m.api == "v1" and not m.token:
        mineru["note"] = "no MINERU_TOKEN: using the anonymous v1 API (low rate limits)"
    report["mineru"] = mineru
    try:
        r = resolve(settings.llm)
        report["llm"] = (
            {"provider": r.provider, "model": r.model, "base_url": r.base_url, "api_key": bool(r.api_key)}
            if r
            else {
                "configured": False,
                "note": "no LLM: ingest falls back to heuristic Markdown; translation unavailable",
            }
        )
        if r and args.ping:
            from .llm import LLM

            t0 = time.time()
            text, _ = LLM(r, timeout=60).complete(
                [{"role": "user", "content": "Reply with the word: ok"}], max_tokens=16
            )
            report["llm"]["ping"] = {"reply": text.strip()[:40], "seconds": round(time.time() - t0, 2)}
    except LLMError as exc:
        report["llm"] = {"error": str(exc)}
    report["translate"] = tr.status()
    report["tools"] = {"git": bool(shutil.which("git")), "uv": bool(shutil.which("uv"))}
    problems = []
    if not lib.exists:
        problems.append("library not initialised: run `pdfskill init`")
    if not m.token:
        problems.append("set MINERU_TOKEN for the full MinerU API (anonymous v1 is used otherwise)")
    if not report["llm"].get("api_key"):
        problems.append("no LLM configured: set e.g. DEEPSEEK_API_KEY (see `pdfskill providers`)")
    if not report["translate"]["ready"]:
        problems.append("translation not set up: run `pdfskill setup translate`")
    report["problems"] = problems
    if args.json:
        _out(args, report)
    else:
        lines = [
            f"pdfskill {__version__}",
            f"config:    {report['config_file']}" + ("" if report["config_file_exists"] else " (absent)"),
            f"library:   {lib.root} ({'ok' if lib.exists else 'not initialised'}, "
            f"{report['library']['documents']} docs)",
            f"mineru:    api={m.api} token={'yes' if m.token else 'no'}"
            + (f" ({mineru['token_days_left']} days left)" if "token_days_left" in mineru else ""),
            f"llm:       {json.dumps(report['llm'], ensure_ascii=False)}",
            f"translate: {'ready' if report['translate']['ready'] else 'not set up'}",
        ]
        lines += [f"- {p}" for p in problems] or ["all good"]
        print("\n".join(lines))
    return EXIT_OK


def cmd_config(args, settings) -> int:
    path = config_path()
    if args.init:
        if path.exists():
            print(f"{path} already exists", file=sys.stderr)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            os.chmod(path, 0o600)
        _out(args, {"config_file": str(path)}, str(path))
        return EXIT_OK
    r = None
    try:
        r = resolve(settings.llm)
    except LLMError:
        pass
    data = {
        "config_file": str(path),
        "library": str(settings.library) if settings.library else None,
        "mineru": {
            "api": settings.mineru.api,
            "token": _mask(settings.mineru.token),
            "model_version": settings.mineru.model_version,
            "language": settings.mineru.language,
        },
        "llm": {
            "provider": r.provider if r else settings.llm.provider,
            "model": r.model if r else None,
            "base_url": r.base_url if r else None,
            "api_key": _mask(r.api_key) if r else None,
            "concurrency": settings.llm.concurrency,
        },
        "translate": {"target_lang": settings.target_lang, "home": str(tr.data_home())},
    }
    _out(args, data)
    return EXIT_OK


def cmd_providers(args, settings) -> int:
    rows = [
        {
            "name": n,
            "base_url": p.base_url,
            "key_env": list(p.key_env),
            "default_model": p.default_model,
            "key_set": any(os.environ.get(k) for k in p.key_env),
            "note": p.note,
        }
        for n, p in PROVIDERS.items()
    ]
    text = "\n".join(
        f"{r['name']:<17} {'*' if r['key_set'] else ' '} {r['default_model']:<30} {'/'.join(r['key_env'])}"
        for r in rows
    )
    _out(
        args,
        rows,
        "provider          key default model                  env var\n"
        + text
        + "\n(* = key found in environment; any other OpenAI-compatible endpoint: set llm.base_url + llm.model)",
    )
    return EXIT_OK


def cmd_setup(args, settings) -> int:
    if args.what != "translate":
        print("usage: pdfskill setup translate", file=sys.stderr)
        return EXIT_USAGE
    st = tr.setup(fonts=not args.no_fonts, force=args.force, progress=_progress(args.quiet))
    _out(args, st, json.dumps(st, indent=2, ensure_ascii=False))
    return EXIT_OK if st["ready"] else EXIT_ERR


def _ingest_paths(paths: list[str]) -> list[Path]:
    from .mineru import SUPPORTED_SUFFIXES

    out: list[Path] = []
    for p in paths:
        path = Path(p).expanduser()
        if path.is_dir():
            out += sorted(
                q
                for q in path.rglob("*")
                if q.is_file()
                and q.suffix.lower() in SUPPORTED_SUFFIXES
                and not any(part.startswith(".") for part in q.relative_to(path).parts)
            )
        else:
            out.append(path)
    return out


def cmd_ingest(args, settings) -> int:
    from .ingest import IngestOptions, ingest

    lib = _lib(args, settings)
    if not lib.exists and args.init:
        lib.init()
    lib.require()
    lang = args.translate if args.translate is not None else None
    if lang == "":
        lang = settings.target_lang
    opts = IngestOptions(
        translate=lang,
        use_llm=not args.no_llm,
        force=args.force,
        reparse=args.reparse,
        commit=not args.no_commit,
        page_ranges=args.pages,
        language=args.lang,
        is_ocr=True if args.ocr else None,
        model_version=args.model_version,
        title=args.title,
        tags=args.tag or [],
        translate_options={
            k: v
            for k, v in {
                "render_mode": args.render_mode,
                "workers": args.workers,
                "pdf_compress_dpi": args.compress_dpi,
            }.items()
            if v is not None
        },
    )
    progress = _progress(args.quiet)
    results, failed = [], 0
    for path in _ingest_paths(args.paths):
        progress(f"== {path}")
        try:
            results.append(ingest(lib, path, settings, opts, progress))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop a batch
            failed += 1
            results.append({"source": str(path), "status": "failed", "error": str(exc)})
            progress(f"failed: {exc}")
    if args.json:
        _out(args, results if len(results) != 1 else results[0])
    else:
        for r in results:
            if r["status"] == "failed":
                print(f"FAILED  {r['source']}: {r['error']}")
                continue
            line = f"{r['status']:<9} {r['doc_id']}  {r.get('title', '')}"
            if r.get("pages"):
                line += f"  ({r['pages']} pages, {r.get('chunks', 0)} chunks"
                v = r.get("validation") or {}
                line += ", markdown valid)" if v.get("ok") else f", {len(v.get('errors', []))} validation issues)"
            print(line)
            conv = r.get("conversion") or {}
            if conv.get("problems") or conv.get("fallback_segments"):
                print(
                    f"          note: {conv.get('fallback_segments', 0)} text segments kept as parsed "
                    f"({len(conv.get('problems', []))} LLM issues); details: pdfskill show {r['doc_id']}"
                )
            if r.get("translation"):
                t = r["translation"]
                print(f"          translated: {t['pdf']}  {t['markdown']}")
    if failed and failed == len(results):
        return EXIT_ERR
    return EXIT_PARTIAL if failed else EXIT_OK


def cmd_translate(args, settings) -> int:
    args.translate = args.to
    for name in ("no_llm", "reparse", "pages", "lang", "ocr", "model_version", "title", "tag", "init"):
        setattr(args, name, getattr(args, name, None))
    args.no_llm = False
    return cmd_ingest(args, settings)


def cmd_search(args, settings) -> int:
    from .search import SearchIndex

    lib = _lib(args, settings).require()
    doc_ids = [lib.resolve_id(d) for d in args.doc] if args.doc else None
    lang = {"orig": None, "any": "any"}.get(args.in_lang, args.in_lang)
    res = SearchIndex(lib.chunks_dir, lib.cache_dir / "bm25").search(
        args.query, k=max(1, min(args.k, 50)), doc_ids=doc_ids, lang=lang, max_per_doc=args.max_per_doc
    )
    catalog = lib.catalog()
    for h in res["hits"]:
        h["title"] = catalog.get(h["doc_id"], {}).get("title")
    if args.json:
        _out(args, res)
        return EXIT_OK
    if not res["hits"]:
        msg = "no results"
        if res["unknown_terms"]:
            msg += f" (terms not in the index: {', '.join(res['unknown_terms'][:10])})"
        print(msg)
        return EXIT_OK
    for h in res["hits"]:
        pages = h["pages"] or [None, None]
        ptxt = f"p.{pages[0]}" + (f"-{pages[1]}" if pages[1] != pages[0] else "") if pages[0] else "p.?"
        lang_tag = f" [{h['lang']}]" if h.get("lang") else ""
        print(
            f"[{h['rank']}] {h['score']:.2f}  {h['chunk_id']}{lang_tag}  {ptxt}  L{h['lines'][0]}-{h['lines'][1]}"
            f"  {h['title'] or ''}"
        )
        if h["heading"]:
            print(f"     § {h['heading']}")
        print(f"     {h['snippet']}")
    print("\nread more: pdfskill get <doc_id> --chunk <chunk_id>  |  --pages N  |  --lines A-B")
    return EXIT_OK


def cmd_locate(args, settings) -> int:
    from .query import locate

    lib = _lib(args, settings).require()
    doc_ids = [lib.resolve_id(d) for d in args.doc] if args.doc else None
    hits = locate(lib, args.query, doc_ids=doc_ids, kind=args.type, limit=args.n)
    if args.json:
        _out(args, hits)
        return EXIT_OK
    if not hits:
        print("no matches in the layout JSON (try `pdfskill search` for full-text BM25)")
    for h in hits:
        lines = f"L{h['lines'][0]}-{h['lines'][1]}" if h.get("lines") else "L?"
        print(f"{h['doc_id']}  p.{h['page']}  {lines}  {h['type']:<9} {h['preview']}")
    return EXIT_OK


def cmd_get(args, settings) -> int:
    from .query import get

    lib = _lib(args, settings).require()
    chunk = args.chunk or (args.doc if "#" in args.doc else None)
    ref = args.doc.split("#", 1)[0].split(".", 1)[0] if "#" in args.doc else args.doc
    doc_id = lib.resolve_id(ref)
    lang = args.in_lang
    if chunk and "." in chunk.split("#", 1)[0]:
        lang = chunk.split("#", 1)[0].split(".", 1)[1]
    res = get(
        lib,
        doc_id,
        pages=args.pages,
        lines=args.lines,
        heading=args.heading,
        chunk=chunk,
        lang=lang,
        max_chars=args.max_chars,
    )
    if args.json:
        _out(args, res)
    else:
        head = f"<!-- {res['doc_id']} {res['file']} L{res['lines'][0]}-{res['lines'][1]}"
        if res.get("pages"):
            head += f" p.{res['pages'][0]}-{res['pages'][1]}"
        head += " (truncated) -->" if res["truncated"] else " -->"
        print(head)
        print(res["text"])
    return EXIT_OK


def cmd_outline(args, settings) -> int:
    from .query import outline

    lib = _lib(args, settings).require()
    res = outline(lib, lib.resolve_id(args.doc))
    if args.json:
        _out(args, res)
    else:
        print(f"{res['doc_id']}  {res['title']}  ({res['pages']} pages)")
        for o in res["outline"]:
            md = f"L{o['md'][0]}" if o.get("md") else ""
            print(f"{'  ' * (o['level'] - 1)}- {o['text']}  (p.{o['page']} {md})")
    return EXIT_OK


def cmd_list(args, settings) -> int:
    lib = _lib(args, settings).require()
    entries = [e for e in lib.catalog().values() if not args.tag or args.tag in e.get("tags", [])]
    if args.json:
        _out(args, entries)
        return EXIT_OK
    for e in sorted(entries, key=lambda x: x.get("added", "")):
        tr_ = ",".join(sorted(e.get("translations") or {}))
        print(
            f"{e['id']}  {e.get('pages', '?'):>4}p  {e.get('added', ''):<10}  {('[' + tr_ + '] ') if tr_ else ''}"
            f"{e.get('title', '')}"
        )
    if not entries:
        print("library is empty; add documents with `pdfskill ingest <file>`")
    return EXIT_OK


def cmd_show(args, settings) -> int:
    lib = _lib(args, settings).require()
    doc_id = lib.resolve_id(args.doc)
    entry = lib.catalog()[doc_id]
    doc = lib.load_doc(doc_id)
    data = {
        **entry,
        "files": [lib.rel(p) for p in lib.doc_files(doc_id)],
        "parser": doc.get("parser"),
        "conversion": doc.get("conversion"),
        "translations_detail": doc.get("translations"),
        "headings": len(doc.get("outline", [])),
        "assets": len(doc.get("assets", [])),
    }
    _out(args, data)
    return EXIT_OK


def cmd_remove(args, settings) -> int:
    lib = _lib(args, settings).require()
    doc_id = lib.resolve_id(args.doc)
    title = lib.catalog()[doc_id].get("title", "")
    removed = lib.remove(doc_id)
    sha = None if args.no_commit else lib.commit(f"remove: {title} ({doc_id})", ["."])
    _out(args, {"doc_id": doc_id, "removed": removed, "commit": sha}, f"removed {doc_id} ({len(removed)} files)")
    return EXIT_OK


def cmd_reindex(args, settings) -> int:
    from .ingest import rechunk
    from .search import SearchIndex

    lib = _lib(args, settings).require()
    n_chunks = None
    if args.rechunk:
        ids = [lib.resolve_id(d) for d in args.doc] if args.doc else sorted(lib.catalog())
        n_chunks = sum(rechunk(lib, d) for d in ids)
    t0 = time.time()
    rebuilt = SearchIndex(lib.chunks_dir, lib.cache_dir / "bm25").build(force=True)
    man = json.loads((lib.cache_dir / "bm25" / "manifest.json").read_text())
    _out(
        args,
        {"rebuilt": rebuilt, "chunks": man["n_chunks"], "seconds": round(time.time() - t0, 2), "rechunked": n_chunks},
        f"index rebuilt: {man['n_chunks']} chunks in {time.time() - t0:.2f}s",
    )
    return EXIT_OK


def cmd_guide(args, settings) -> int:
    print(GUIDE.read_text(encoding="utf-8"))
    return EXIT_OK


# -- parser ------------------------------------------------------------------------------------------------


def _common(suppress: bool) -> argparse.ArgumentParser:
    """Global options, accepted before or after the subcommand.

    Sub-parsers use SUPPRESS defaults so a value given before the subcommand is not reset.
    """
    kw = {"default": argparse.SUPPRESS} if suppress else {}
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument(
        "--library",
        "-L",
        help="library directory (default: $PDFSKILL_LIBRARY, config, "
        "a parent dir with .pdfskill/, or ~/pdfskill-library)",
        **({"default": None} | kw),
    )
    c.add_argument("--json", action="store_true", help="machine-readable JSON on stdout", **kw)
    c.add_argument("--quiet", "-q", action="store_true", help="no progress on stderr", **kw)
    return c


def build_parser() -> argparse.ArgumentParser:
    common = _common(suppress=True)
    p = argparse.ArgumentParser(
        prog="pdfskill",
        parents=[_common(suppress=False)],
        description="Ingest documents into a flat, git-friendly Markdown library (MinerU + LLM), search it "
        "with BM25, and translate PDFs with their layout preserved (retain-pdf).",
        epilog="Start with `pdfskill doctor`, then `pdfskill init`. Full guide: `pdfskill guide`.",
    )
    p.add_argument("--version", action="version", version=f"pdfskill {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("init", parents=[common], help="create a library (git repo) at --library")
    s.add_argument("--no-git", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", parents=[common], help="check configuration, keys and tools")
    s.add_argument("--ping", action="store_true", help="send a tiny request to the configured LLM")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("config", parents=[common], help="show resolved config (secrets masked)")
    s.add_argument("--init", action="store_true", help="write a commented template config file")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("providers", parents=[common], help="list built-in LLM providers")
    s.set_defaults(func=cmd_providers)

    s = sub.add_parser("setup", parents=[common], help="one-time setup: `setup translate`")
    s.add_argument("what", choices=["translate"])
    s.add_argument("--no-fonts", action="store_true", help="skip downloading Source Han Serif SC (~47 MB)")
    s.add_argument("--force", action="store_true", help="recreate the retain-pdf environment")
    s.set_defaults(func=cmd_setup)

    def ingest_args(s: argparse.ArgumentParser, *, translate_flag: bool) -> None:
        s.add_argument("paths", nargs="+", help="files or directories")
        if translate_flag:
            s.add_argument(
                "--translate",
                nargs="?",
                const="",
                metavar="LANG",
                help="also translate (PDF only; retain-pdf supports zh)",
            )
            s.add_argument("--no-llm", action="store_true", help="heuristic Markdown only (no LLM calls)")
            s.add_argument("--reparse", action="store_true", help="call MinerU again instead of the cache")
            s.add_argument("--pages", help="page range for MinerU, e.g. 1-20")
            s.add_argument("--lang", help="OCR language hint (ch, en, japan, korean, latin, ...)")
            s.add_argument("--ocr", action="store_true", help="force OCR (scanned documents)")
            s.add_argument("--model-version", choices=["vlm", "pipeline"])
            s.add_argument("--title", help="override the document title")
            s.add_argument("--tag", action="append", help="tag (repeatable)")
            s.add_argument("--init", action="store_true", help="initialise the library if missing")
        s.add_argument("--force", action="store_true", help="redo conversion (and translation)")
        s.add_argument("--no-commit", action="store_true", help="do not git-commit the library")
        s.add_argument(
            "--render-mode",
            choices=["auto", "overlay", "typst", "typst_visual", "dual"],
            help="translation render mode (dual = bilingual side by side)",
        )
        s.add_argument("--workers", type=int, help="translation concurrency")
        s.add_argument("--compress-dpi", type=int, help="downsample images in the translated PDF (0 = off)")

    s = sub.add_parser("ingest", parents=[common], help="add files to the library")
    ingest_args(s, translate_flag=True)
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("translate", parents=[common], help="ingest a PDF (if needed) and translate it")
    ingest_args(s, translate_flag=False)
    s.add_argument("--to", default="zh", help="target language (retain-pdf: zh)")
    s.set_defaults(func=cmd_translate)

    s = sub.add_parser("search", parents=[common], help="BM25 full-text search across the library")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=8, help="results (max 50)")
    s.add_argument("--doc", action="append", help="restrict to document id(s)")
    s.add_argument("--in", dest="in_lang", default="any", help="any | orig | zh (translation)")
    s.add_argument("--max-per-doc", type=int, default=None)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("locate", parents=[common], help="coarse lookup in the layout JSON (headings, captions)")
    s.add_argument("query")
    s.add_argument("--doc", action="append")
    s.add_argument("--type", help="heading | table | figure | equation | code | list | paragraph")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_locate)

    s = sub.add_parser("get", parents=[common], help="read Markdown by page, lines, heading or chunk")
    s.add_argument("doc", help="doc id (prefix ok), title, file name, or a chunk id")
    s.add_argument("--pages", help="N or N-M (source PDF pages)")
    s.add_argument("--lines", help="A-B (Markdown lines)")
    s.add_argument("--heading", help="section title (exact or substring)")
    s.add_argument("--chunk", help="chunk id from search")
    s.add_argument("--in", dest="in_lang", default=None, help="read the translation, e.g. zh")
    s.add_argument("--max-chars", type=int, default=20000)
    s.set_defaults(func=cmd_get)

    s = sub.add_parser("outline", parents=[common], help="heading tree with pages and line numbers")
    s.add_argument("doc")
    s.set_defaults(func=cmd_outline)

    s = sub.add_parser("list", parents=[common], help="list documents")
    s.add_argument("--tag")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", parents=[common], help="catalog entry and conversion details")
    s.add_argument("doc")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("remove", parents=[common], help="delete a document and its unused assets")
    s.add_argument("doc")
    s.add_argument("--no-commit", action="store_true")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("reindex", parents=[common], help="rebuild the BM25 index")
    s.add_argument("--rechunk", action="store_true", help="regenerate chunk files from Markdown first")
    s.add_argument("--doc", action="append")
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("guide", parents=[common], help="print the agent usage guide")
    s.set_defaults(func=cmd_guide)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    settings = load_settings()
    try:
        return args.func(args, settings)
    except (LibraryError, LLMError, tr.TranslateError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERR
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("PDFSKILL_DEBUG"):
            raise
        msg = f"{type(exc).__name__}: {exc}"
        if args.json:
            print(json.dumps({"error": msg}, ensure_ascii=False))
        else:
            print(f"error: {msg} (set PDFSKILL_DEBUG=1 for a traceback)", file=sys.stderr)
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
