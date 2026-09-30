"""The on-disk library: a flat, git-friendly directory.

::

    <library>/
    ├── .pdfskill/library.toml   marker + non-secret settings (committed)
    ├── catalog.jsonl            one JSON object per document, sorted by id
    ├── docs/<id>.md             hierarchical Markdown
    ├── docs/<id>.json           layout JSON: pages, blocks, outline, Markdown anchors
    ├── docs/<id>.<lang>.md      translated Markdown
    ├── docs/<id>.<lang>.pdf     translated, layout-preserving PDF
    ├── assets/<sha>.<ext>       images, named by content hash (deduplicated)
    ├── chunks/<id>[.<lang>].jsonl  search chunks (plain text, one per line)
    └── .cache/                  derived data, git-ignored (BM25 index, MinerU raw results)

Original input files are never stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

MARKER_DIR = ".pdfskill"
DEFAULT_LIBRARY = Path("~/pdfskill-library").expanduser()

GITIGNORE = """\
# derived data, rebuilt on demand
.cache/
.DS_Store
"""

GITATTRIBUTES = """\
*.md text eol=lf
*.json text eol=lf
*.jsonl text eol=lf
*.pdf binary
*.png binary
*.jpg binary
*.jpeg binary
*.gif binary
*.webp binary
"""

LIBRARY_TOML = """\
# pdfskill library (https://github.com/Uniseem/pdf-skill)
# Non-secret settings only. API keys go in env vars or ~/.config/pdfskill/config.toml.
version = 1
"""

README = """\
# Document library

Managed by [pdf-skill](https://github.com/Uniseem/pdf-skill). Layout:

- `catalog.jsonl`: one line per document (id, title, pages, translations)
- `docs/<id>.md`: hierarchical Markdown; `docs/<id>.json`: pages, blocks, anchors
- `docs/<id>.<lang>.md|pdf`: translations
- `assets/`: images named by content hash
- `chunks/`: plain-text search chunks (BM25 index is rebuilt into `.cache/`)

Search with `pdfskill search "<query>"`, read with `pdfskill get <id> --page N`.
"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def doc_id_for(sha256: str) -> str:
    return sha256[:16]


class LibraryError(RuntimeError):
    pass


class Library:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    # -- discovery ----------------------------------------------------------------------

    @classmethod
    def find(cls, explicit: Path | None = None, configured: Path | None = None) -> Library:
        """Resolve the library: --library > PDFSKILL_LIBRARY/config > cwd ancestors > default."""
        if explicit:
            return cls(explicit)
        if configured:
            return cls(configured)
        here = Path.cwd().resolve()
        for d in (here, *here.parents):
            if (d / MARKER_DIR / "library.toml").is_file():
                return cls(d)
        return cls(DEFAULT_LIBRARY)

    @property
    def exists(self) -> bool:
        return (self.root / MARKER_DIR / "library.toml").is_file()

    def require(self) -> Library:
        if not self.exists:
            raise LibraryError(f"no library at {self.root}; run `pdfskill init` first (or pass --library)")
        return self

    # -- layout -------------------------------------------------------------------------------

    @property
    def docs_dir(self) -> Path:
        return self.root / "docs"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def chunks_dir(self) -> Path:
        return self.root / "chunks"

    @property
    def cache_dir(self) -> Path:
        return self.root / ".cache"

    @property
    def catalog_path(self) -> Path:
        return self.root / "catalog.jsonl"

    def doc_md(self, doc_id: str, lang: str | None = None) -> Path:
        return self.docs_dir / (f"{doc_id}.{lang}.md" if lang else f"{doc_id}.md")

    def doc_json(self, doc_id: str) -> Path:
        return self.docs_dir / f"{doc_id}.json"

    def doc_pdf(self, doc_id: str, lang: str) -> Path:
        return self.docs_dir / f"{doc_id}.{lang}.pdf"

    def chunks_file(self, doc_id: str, lang: str | None = None) -> Path:
        return self.chunks_dir / (f"{doc_id}.{lang}.jsonl" if lang else f"{doc_id}.jsonl")

    def raw_cache(self, doc_id: str) -> Path:
        return self.cache_dir / "mineru" / doc_id

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()

    # -- init ----------------------------------------------------------------------------------

    def init(self, *, git: bool = True) -> list[str]:
        created: list[str] = []
        for d in (self.root / MARKER_DIR, self.docs_dir, self.assets_dir, self.chunks_dir):
            if not d.exists():
                d.mkdir(parents=True)
                created.append(self.rel(d) + "/")
        for name, content in (
            (f"{MARKER_DIR}/library.toml", LIBRARY_TOML),
            (".gitignore", GITIGNORE),
            (".gitattributes", GITATTRIBUTES),
            ("README.md", README),
        ):
            p = self.root / name
            if not p.exists():
                p.write_text(content, encoding="utf-8")
                created.append(name)
        for d in (self.docs_dir, self.assets_dir, self.chunks_dir):
            keep = d / ".gitkeep"
            if not any(d.iterdir()):
                keep.touch()
        if not self.catalog_path.exists():
            self.catalog_path.write_text("", encoding="utf-8")
            created.append("catalog.jsonl")
        if git and shutil.which("git") and not (self.root / ".git").exists():
            self._git("init", "-q")
            created.append(".git/")
        if git and self.is_git and created:
            self.commit("init pdfskill library", ["."])
        return created

    # -- catalog -------------------------------------------------------------------------------

    def catalog(self) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        if self.catalog_path.exists():
            for line in self.catalog_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = json.loads(line)
                    entries[e["id"]] = e
        return entries

    def write_catalog(self, entries: dict[str, dict]) -> None:
        lines = [json.dumps(entries[k], ensure_ascii=False, sort_keys=True) for k in sorted(entries)]
        _atomic_write(self.catalog_path, "\n".join(lines) + ("\n" if lines else ""))

    def upsert(self, entry: dict) -> None:
        entries = self.catalog()
        entries[entry["id"]] = {**entries.get(entry["id"], {}), **entry}
        self.write_catalog(entries)

    def resolve_id(self, ref: str) -> str:
        """Accept a full id, a unique id prefix, or an exact source file name / title."""
        entries = self.catalog()
        if ref in entries:
            return ref
        hits = [k for k in entries if k.startswith(ref)] if len(ref) >= 4 else []
        if not hits:
            low = ref.lower()
            hits = [
                k
                for k, e in entries.items()
                if low in {str(e.get("source_name", "")).lower(), str(e.get("title", "")).lower()}
            ]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise LibraryError(f"no document matches {ref!r}")
        raise LibraryError(f"{ref!r} is ambiguous: {', '.join(sorted(hits)[:5])}")

    # -- documents -------------------------------------------------------------------------

    def load_doc(self, doc_id: str) -> dict:
        path = self.doc_json(doc_id)
        if not path.exists():
            raise LibraryError(f"document {doc_id} has no {self.rel(path)}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_doc(self, doc: dict) -> Path:
        path = self.doc_json(doc["doc_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        return path

    def add_asset(self, src: Path) -> str:
        """Copy an image into assets/ under its content hash; return 'assets/<name>'."""
        data = Path(src).read_bytes()
        return self.add_asset_bytes(data, Path(src).suffix)

    def add_asset_bytes(self, data: bytes, suffix: str) -> str:
        suffix = (suffix or ".bin").lower()
        if suffix == ".jpeg":
            suffix = ".jpg"
        name = hashlib.sha256(data).hexdigest()[:24] + suffix
        dest = self.assets_dir / name
        if not dest.exists():
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return f"assets/{name}"

    def doc_files(self, doc_id: str) -> list[Path]:
        files = sorted(self.docs_dir.glob(f"{doc_id}.*")) + sorted(self.chunks_dir.glob(f"{doc_id}.*"))
        return [p for p in files if p.is_file()]

    def remove(self, doc_id: str) -> list[str]:
        doc = self.load_doc(doc_id) if self.doc_json(doc_id).exists() else {}
        removed = []
        for p in self.doc_files(doc_id):
            removed.append(self.rel(p))
            p.unlink()
        still_used: set[str] = set()
        for other in self.docs_dir.glob("*.json"):
            try:
                still_used.update(json.loads(other.read_text(encoding="utf-8")).get("assets", []))
            except json.JSONDecodeError:
                continue
        for asset in doc.get("assets", []):
            p = self.root / asset
            if asset not in still_used and p.exists():
                removed.append(asset)
                p.unlink()
        entries = self.catalog()
        entries.pop(doc_id, None)
        self.write_catalog(entries)
        shutil.rmtree(self.raw_cache(doc_id), ignore_errors=True)
        return removed

    # -- git -------------------------------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        return (self.root / ".git").exists() and shutil.which("git") is not None

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], check=check, capture_output=True, text=True)

    def commit(self, message: str, paths: list[str]) -> str | None:
        """Stage ``paths`` (relative to the root) and commit; return the short sha or None."""
        if not self.is_git:
            return None
        self._git("add", "-A", "--", *paths)
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        env_ok = self._git("config", "user.email", check=False).stdout.strip()
        args = ["commit", "-q", "-m", message]
        if not env_ok:
            args = ["-c", "user.name=pdfskill", "-c", "user.email=pdfskill@localhost", *args]
        self._git(*args)
        return self._git("rev-parse", "--short", "HEAD").stdout.strip()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
