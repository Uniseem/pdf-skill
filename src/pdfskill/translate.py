"""Layout-preserving PDF translation via the vendored retain-pdf pipeline.

retain-pdf (https://github.com/wxyhgk/retain-pdf, MIT) runs in its own virtual
environment (it pins PyMuPDF/Pillow/pikepdf versions) and is driven one stage
per subprocess:

    normalize-ocr  (reads the MinerU result ingest already downloaded)
    translate-only (OpenAI-compatible LLM: the same provider/model as ingest)
    render-only    (typst; writes the translated PDF)

Afterwards the translated items are matched back onto pdfskill's blocks (same
page, bbox overlap + text similarity) to build ``docs/<id>.<lang>.md`` with the
same heading structure and image assets as the original Markdown.

retain-pdf translates into Simplified Chinese only.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx

from .validate import clean_heading_text

VENDOR_DIR = Path(__file__).parent / "_vendor" / "retainpdf-pipeline"
TYPST_VERSION = "0.15.1"
RETAIN_COMMIT = "857868407aeace94ccf5d8cf0bcf90aecae3b7bf"
FONT_BASE = f"https://raw.githubusercontent.com/wxyhgk/retain-pdf/{RETAIN_COMMIT}/resources/fonts"
FONT_FILES = ("SourceHanSerifSC-Regular.otf", "SourceHanSerifSC-Bold.otf", "LICENSE-OFL-1.1.txt")
SUPPORTED_TARGETS = {"zh", "zh-cn", "zh-hans", "chs"}


class TranslateError(RuntimeError):
    pass


def data_home() -> Path:
    if os.environ.get("PDFSKILL_HOME"):
        return Path(os.environ["PDFSKILL_HOME"]).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "~")).expanduser() / "pdfskill"
    return Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "pdfskill"


def venv_dir() -> Path:
    return data_home() / "retainpdf-venv"


def pipeline_exe() -> Path:
    sub = "Scripts" if sys.platform == "win32" else "bin"
    name = "retainpdf-pipeline.exe" if sys.platform == "win32" else "retainpdf-pipeline"
    return venv_dir() / sub / name


def typst_path() -> Path | None:
    if os.environ.get("TYPST_BIN") and Path(os.environ["TYPST_BIN"]).exists():
        return Path(os.environ["TYPST_BIN"])
    local = data_home() / "bin" / ("typst.exe" if sys.platform == "win32" else "typst")
    if local.exists():
        return local
    found = shutil.which("typst")
    return Path(found) if found else None


def fonts_dir() -> Path:
    return data_home() / "fonts"


def status() -> dict:
    fonts = [f for f in FONT_FILES[:2] if (fonts_dir() / f).exists()]
    exe = pipeline_exe()
    return {
        "ready": exe.exists() and typst_path() is not None,
        "pipeline": str(exe) if exe.exists() else None,
        "typst": str(typst_path()) if typst_path() else None,
        "fonts": str(fonts_dir()) if len(fonts) == 2 else None,
        "home": str(data_home()),
        "target_languages": ["zh"],
    }


# -- setup -----------------------------------------------------------------------------------------------


def _typst_asset() -> str:
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    if arch is None:
        raise TranslateError(f"no typst build for {machine}; install typst {TYPST_VERSION} and set TYPST_BIN")
    if sys.platform == "darwin":
        return f"typst-{arch}-apple-darwin.tar.xz"
    if sys.platform == "win32":
        return f"typst-{arch}-pc-windows-msvc.zip"
    return f"typst-{arch}-unknown-linux-musl.tar.xz"


def _download(url: str, dest: Path, progress) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
        if resp.status_code >= 400:
            raise TranslateError(f"download failed ({resp.status_code}): {url}")
        total = int(resp.headers.get("content-length") or 0)
        got, last = 0, -1
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 16):
                fh.write(chunk)
                got += len(chunk)
                pct = min(100, int(got * 100 / total)) if total > (1 << 20) else -1
                if pct >= 0 and pct // 20 != last:
                    last = pct // 20
                    progress(f"  {dest.name}: {pct}%")
    tmp.replace(dest)


def install_typst(progress) -> Path:
    asset = _typst_asset()
    url = f"https://github.com/typst/typst/releases/download/v{TYPST_VERSION}/{asset}"
    work = data_home() / "downloads"
    archive = work / asset
    progress(f"downloading typst {TYPST_VERSION}")
    _download(url, archive, progress)
    bin_dir = data_home() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe_name = "typst.exe" if sys.platform == "win32" else "typst"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = next(n for n in zf.namelist() if n.endswith("/" + exe_name) or n == exe_name)
            (bin_dir / exe_name).write_bytes(zf.read(member))
    else:
        with tarfile.open(archive, "r:xz") as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/" + exe_name))
            fh = tf.extractfile(member)
            (bin_dir / exe_name).write_bytes(fh.read() if fh else b"")
    (bin_dir / exe_name).chmod(0o755)
    archive.unlink(missing_ok=True)
    return bin_dir / exe_name


def setup(*, fonts: bool = True, force: bool = False, progress=lambda m: None) -> dict:
    """Create the retain-pdf environment, fetch typst and (optionally) the CJK fonts."""
    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    venv = venv_dir()
    if force and venv.exists():
        shutil.rmtree(venv)
    if not pipeline_exe().exists():
        progress(f"creating {venv}")
        if uv:
            subprocess.run([uv, "venv", "-q", "--python", "3.12", str(venv)], check=True)
            py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            progress("installing retain-pdf pipeline (PyMuPDF, pikepdf, Pillow)")
            subprocess.run([uv, "pip", "install", "-q", "--python", str(py), str(VENDOR_DIR)], check=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            subprocess.run([str(py), "-m", "pip", "install", "-q", str(VENDOR_DIR)], check=True)
    if typst_path() is None:
        install_typst(progress)
    if fonts:
        for name in FONT_FILES:
            dest = fonts_dir() / name
            if not dest.exists():
                progress(f"downloading font {name}")
                _download(f"{FONT_BASE}/{name}", dest, progress)
    return status()


# -- running ---------------------------------------------------------------------------------------------


def _job_dirs(job: Path) -> None:
    for d in ("source", "ocr", "translated", "rendered", "artifacts", "logs", "specs"):
        (job / d).mkdir(parents=True, exist_ok=True)


def _link_tree(src: Path, dst: Path) -> None:
    """Copy the MinerU result; hard-link images (read-only), copy JSON (normalize rewrites it)."""
    if dst.exists():
        shutil.rmtree(dst)

    def copy(s: str, d: str) -> None:
        if s.endswith(".json"):
            shutil.copy2(s, d)
            return
        try:
            os.link(s, d)
        except OSError:
            shutil.copy2(s, d)

    shutil.copytree(src, dst, copy_function=copy)


def _run_stage(cmd: str, spec: Path, env: dict, log: Path, progress) -> None:
    progress(f"retain-pdf: {cmd}")
    with log.open("a", encoding="utf-8") as fh:
        proc = subprocess.run(
            [str(pipeline_exe()), cmd, "--spec", str(spec)],
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            cwd=str(spec.parent),
        )
    if proc.returncode != 0:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        raise TranslateError(f"retain-pdf {cmd} failed (exit {proc.returncode}); log {log}:\n" + "\n".join(tail))


def run(
    *, job: Path, source_pdf: Path, raw_dir: Path, llm, options: dict | None = None, progress=lambda m: None
) -> dict:
    """Run normalize -> translate -> render; return paths of the outputs.

    ``llm`` is a :class:`pdfskill.llm.ResolvedLLM`. ``options`` may contain
    ``workers``, ``render_mode``, ``pdf_compress_dpi``, ``glossary`` (list of
    {source, target}), ``rule_profile`` and ``custom_rules``.
    """
    opts = options or {}
    st = status()
    if not st["ready"]:
        raise TranslateError("translation is not set up; run `pdfskill setup translate` first")
    if not llm or not llm.api_key:
        raise TranslateError("translation needs an LLM: configure a provider and API key (see `pdfskill doctor`)")
    job = Path(job)
    if job.exists():
        shutil.rmtree(job)
    _job_dirs(job)
    src = job / "source" / "source.pdf"
    shutil.copyfile(source_pdf, src)
    unpacked = job / "ocr" / "unpacked"
    _link_tree(raw_dir, unpacked)
    j = {"job_id": job.name, "job_root": str(job), "workflow": "book"}
    specs = {
        "normalize": {
            "schema_version": "normalize.stage.v1",
            "stage": "normalize",
            "job": j,
            "inputs": {
                "provider": "mineru",
                "source_json": str(unpacked / "layout.json"),
                "source_pdf": str(src),
                "provider_version": "vlm",
                "provider_raw_dir": str(unpacked),
            },
        },
        "translate": {
            "schema_version": "translate.stage.v1",
            "stage": "translate",
            "job": j,
            "inputs": {"source_json": str(job / "ocr/normalized/document.v1.json"), "source_pdf": str(src)},
            "params": {
                "model": llm.model,
                "base_url": llm.base_url,
                "credential_ref": "env:RETAIN_TRANSLATION_API_KEY",
                "workers": int(opts.get("workers", 8)),
                "mode": "sci",
                "math_mode": "direct_typst",
                "rule_profile_name": opts.get("rule_profile", "general_sci"),
                "custom_rules_text": opts.get("custom_rules", ""),
                "glossary_entries": opts.get("glossary", []),
                "context_mode": "needed",
                "glossary_mode": "matched",
                "memory_mode": "matched",
                "start_page": 0,
                "end_page": -1,
            },
        },
        "render": {
            "schema_version": "render.stage.v1",
            "stage": "render",
            "job": j,
            "inputs": {
                "source_pdf": str(src),
                "translations_dir": str(job / "translated"),
                "translation_manifest": str(job / "translated/translation-manifest.json"),
            },
            "params": {
                "render_mode": opts.get("render_mode", "auto"),
                "pdf_compress_dpi": int(opts.get("pdf_compress_dpi", 150)),
                "translated_pdf_name": "translated.pdf",
                "model": llm.model,
                "base_url": llm.base_url,
                "credential_ref": "env:RETAIN_TRANSLATION_API_KEY",
            },
        },
    }
    paths = {}
    for name, spec in specs.items():
        p = job / "specs" / f"{name}.spec.json"
        p.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        paths[name] = p

    env = {k: v for k, v in os.environ.items() if k not in {"DEEPSEEK_API_KEY", "RETAIN_PDF_ENV_DIR"}}
    empty_env_dir = job / "artifacts" / "env"
    empty_env_dir.mkdir(exist_ok=True)
    env.update(
        {
            "RETAIN_TRANSLATION_API_KEY": llm.api_key,
            "TYPST_BIN": st["typst"],
            "TYPST_PACKAGE_CACHE_PATH": str(data_home() / "typst-packages"),
            "OUTPUT_ROOT": str(job.parent / "_retainpdf_cache"),
            "RETAIN_PDF_ENV_DIR": str(empty_env_dir),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    if st["fonts"]:
        env["RETAIN_PDF_FONTS_DIR"] = st["fonts"]
    log = job / "logs" / "pdfskill-stages.log"
    for name, cmd in (("normalize", "normalize-ocr"), ("translate", "translate-only"), ("render", "render-only")):
        _run_stage(cmd, paths[name], env, log, progress)
    pdf = job / "rendered" / "translated.pdf"
    if not pdf.exists():
        found = sorted((job / "rendered").glob("*.pdf"))
        if not found:
            raise TranslateError(f"render produced no PDF; see {log}")
        pdf = found[0]
    return {"pdf": pdf, "job": job, "items": load_items(job)}


# -- translated items -> blocks ---------------------------------------------------------------------------


def load_items(job: Path) -> list[dict]:
    manifest = json.loads((job / "translated" / "translation-manifest.json").read_text(encoding="utf-8"))
    items: list[dict] = []
    for page in manifest.get("pages", []):
        path = Path(page["path"])
        if not path.is_absolute():
            path = job / "translated" / path.name
        for it in json.loads(path.read_text(encoding="utf-8")):
            kind = it.get("translation_unit_kind") or "single"
            unit_text = it.get("translation_unit_translated_text") or it.get("group_translated_text") or ""
            items.append(
                {
                    "id": it.get("item_id"),
                    "page": int(it.get("page_idx") or 0) + 1,
                    "bbox": it.get("bbox") or [],
                    "source": it.get("source_text") or "",
                    "text": (it.get("translated_text") or "").strip(),
                    "unit_text": unit_text.strip(),
                    "unit": it.get("translation_unit_id") or it.get("item_id"),
                    "kind": kind,
                    "status": it.get("final_status"),
                    "role": it.get("structure_role") or it.get("layout_role") or "",
                }
            )
    return items


def _iou(a: list[float], b: list[float]) -> float:
    if len(a) != 4 or len(b) != 4:
        return 0.0
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _contains(outer: list[float], inner: list[float], slack: float = 15) -> bool:
    return (
        len(outer) == 4
        and len(inner) == 4
        and inner[0] >= outer[0] - slack
        and inner[1] >= outer[1] - slack
        and inner[2] <= outer[2] + slack
        and inner[3] <= outer[3] + slack
    )


def apply_translations(
    blocks: list[dict], items: list[dict], page_sizes: dict[int, list[float]], key: str = "text_tr"
) -> dict:
    """Attach translated text to blocks (``text_tr``, ``caption_text_tr``, ``items_text_tr``)."""
    from rapidfuzz import fuzz

    by_page: dict[int, list[dict]] = {}
    for it in items:
        size = page_sizes.get(it["page"])
        bb = it["bbox"]
        if size and len(bb) == 4:
            w, h = size
            it["nbbox"] = [bb[0] * 1000 / w, bb[1] * 1000 / h, bb[2] * 1000 / w, bb[3] * 1000 / h]
        by_page.setdefault(it["page"], []).append(it)
    used: set[str] = set()

    def best_for(b: dict, text: str) -> dict | None:
        best, score = None, 0.0
        for it in by_page.get(b["page"], []):
            if it["id"] in used or not (it.get("text") or it.get("unit_text")):
                continue
            sim = fuzz.ratio(text[:400], it["source"][:400]) / 100 if text else 0.0
            geo = _iou(b.get("bbox", []), it.get("nbbox", []))
            s = 0.6 * sim + 0.4 * geo
            if s > score:
                best, score = it, s
        return best if score >= 0.45 else None

    # pass 1: match text-like blocks to items
    matches: dict[str, dict] = {}
    for b in blocks:
        if b["type"] in {"heading", "paragraph", "footnote"} and b.get("text") and not b.get("synthetic"):
            it = best_for(b, b["text"])
            if it:
                used.add(it["id"])
                matches[b["id"]] = it

    # pass 2: retain-pdf translates a paragraph broken across blocks (columns, pages) as one unit and
    # splits it back proportionally. For prose, put the whole unit on its first block and leave the
    # rest empty; if a unit touches a heading, keep the per-member split.
    by_unit: dict[str, list[dict]] = {}
    for b in blocks:
        it = matches.get(b["id"])
        if it and it["kind"] != "single":
            by_unit.setdefault(it["unit"], []).append(b)
    whole_unit: dict[str, str] = {}
    for members in by_unit.values():
        if len(members) > 1 and all(m["type"] != "heading" for m in members):
            text = matches[members[0]["id"]]["unit_text"]
            if text:
                whole_unit[members[0]["id"]] = text
                for m in members[1:]:
                    whole_unit[m["id"]] = ""
    matched = 0
    for b in blocks:
        it = matches.get(b["id"])
        if not it:
            continue
        text = whole_unit[b["id"]] if b["id"] in whole_unit else (it["text"] or it["unit_text"])
        if not text and b["id"] not in whole_unit:
            continue
        b[key] = clean_heading_text(text) if b["type"] == "heading" else text
        matched += 1

    for b in blocks:
        t = b["type"]
        if t == "list":
            inside = [
                it
                for it in by_page.get(b["page"], [])
                if it["id"] not in used and it.get("text") and _contains(b.get("bbox", []), it.get("nbbox", []))
            ]
            if inside:
                inside.sort(key=lambda it: (it["nbbox"][1], it["nbbox"][0]))
                used.update(it["id"] for it in inside)
                b["items_" + key] = [it["text"] for it in inside]
                matched += 1
        for field in ("caption", "footnote"):
            if t in {"image", "chart", "table", "code"} and b.get(field):
                it = best_for(b, b[field])
                if it and it.get("text") and fuzz.ratio(b[field][:300], it["source"][:300]) >= 60:
                    used.add(it["id"])
                    b[f"{field}_{key}"] = it["text"]
    translatable = sum(1 for b in blocks if b["type"] in {"heading", "paragraph", "footnote", "list"})
    return {
        "matched_blocks": matched,
        "translatable_blocks": translatable,
        "items": len(items),
        "items_used": len(used),
    }


def normalize_target(lang: str | None) -> str:
    lang = (lang or "zh").lower()
    if lang not in SUPPORTED_TARGETS:
        raise TranslateError(f"retain-pdf translates into Simplified Chinese only (got {lang!r}); use --to zh")
    return "zh"
