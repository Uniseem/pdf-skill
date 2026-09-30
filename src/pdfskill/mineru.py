"""MinerU cloud API client.

Two backends are supported:

* ``v4`` (default, needs a token from https://mineru.net/apiManage/token):
  ``POST /file-urls/batch`` -> PUT bytes -> poll ``/extract-results/batch/{id}``
  -> download ``full_zip_url``.
* ``v1`` (newer API, works anonymously with low rate limits):
  ``POST /v1/uploads`` -> PUT -> complete -> ``POST /v1/parse/jobs`` -> poll ->
  download the ``zip`` output.

Both backends end with the same thing on disk: an extracted result directory
containing ``*_content_list.json``, ``layout.json``/``*_middle.json``,
``full.md`` and ``images/``. :func:`locate_result_files` finds them by suffix
because the file-name prefix is a server-side UUID.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

V4_BASE = "https://mineru.net/api/v4"
V1_BASE = "https://mineru.net/api/v1"

SUPPORTED_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".jp2",
    ".webp",
    ".gif",
    ".bmp",
    ".html",
    ".htm",
}
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_ZIP_BYTES = 4 * 1024**3
MAX_ZIP_ENTRIES = 50_000
RETRY_STATUS = {408, 429, 500, 502, 503, 504}

ProgressFn = Callable[[str], None]


HINTS = {
    "retry_limit_exceeded": "the anonymous API allows only a few parses of the same file; set MINERU_TOKEN "
    "(pdfskill config set mineru.token -) to use the v4 API",
    "-60018": "daily MinerU task limit reached; try again tomorrow",
    "-60005": "file larger than 200 MB",
    "-60006": "too many pages (max 200): split with --pages",
    "A0211": "MinerU token expired: create a new one at https://mineru.net/apiManage/token",
    "A0202": "MinerU token invalid",
}


class MinerUError(RuntimeError):
    def __init__(self, message: str, code: object = None):
        text = f"MinerU error {code}: {message}" if code is not None else message
        hint = HINTS.get(str(code)) or next((h for k, h in HINTS.items() if k in str(message)), None)
        super().__init__(f"{text} ({hint})" if hint else text)
        self.code = code


class MinerUAuthError(MinerUError):
    pass


@dataclass
class ParseOptions:
    model_version: str = "vlm"  # "vlm" | "pipeline" | "MinerU-HTML"
    language: str = "ch"
    is_ocr: bool = False
    enable_formula: bool = True
    enable_table: bool = True
    page_ranges: str | None = None


@dataclass
class ResultFiles:
    root: Path
    content_list: Path | None
    middle: Path | None
    full_md: Path | None
    images_dir: Path | None


def token_expiry(token: str) -> float | None:
    """Return the JWT ``exp`` claim (epoch seconds) if the token is a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


class MinerUClient:
    def __init__(
        self,
        token: str | None,
        *,
        api: str = "v4",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_wait: float = 3600.0,
        progress: ProgressFn | None = None,
        transport: httpx.BaseTransport | None = None,
        poll_interval: float = 2.0,
    ):
        if api not in {"v4", "v1"}:
            raise ValueError(f"unknown MinerU api {api!r}")
        if api == "v4" and not token:
            raise MinerUAuthError(
                "MINERU_TOKEN is not set. Create one at https://mineru.net/apiManage/token "
                "or use --mineru-api v1 for anonymous, rate-limited parsing."
            )
        self.token = token
        self.api = api
        self.base = (base_url or (V4_BASE if api == "v4" else V1_BASE)).rstrip("/")
        self.max_wait = max_wait
        self.progress = progress or (lambda _msg: None)
        self.poll_interval = poll_interval
        self._http = httpx.Client(timeout=timeout, follow_redirects=True, transport=transport)
        exp = token_expiry(token) if token else None
        if exp is not None and exp - time.time() < 3 * 86400:
            left = (exp - time.time()) / 86400
            self.progress(
                "warning: MinerU token expired" if left <= 0 else f"warning: MinerU token expires in {left:.1f} days"
            )

    # -- plumbing -----------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MinerUClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, url: str, *, authed: bool = True, **kw) -> httpx.Response:
        """Send a request with retry on transient failures (honours Retry-After)."""
        headers = kw.pop("headers", None) or (self._auth_headers() if authed else {})
        delay = 2.0
        for attempt in range(6):
            try:
                resp = self._http.request(method, url, headers=headers, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 5:
                    raise MinerUError(f"network error calling {_redact(url)}: {exc}") from exc
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code in RETRY_STATUS and attempt < 5:
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay = min(delay * 2, 30)
                continue
            return resp
        raise AssertionError("unreachable")

    def _json(self, resp: httpx.Response) -> dict:
        if resp.status_code == 401:
            raise MinerUAuthError(f"authentication failed ({resp.text[:200] or 'empty body'})", 401)
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error") or {}
                raise MinerUError(err.get("message") or resp.text[:300], err.get("code") or resp.status_code)
            except (json.JSONDecodeError, AttributeError):
                raise MinerUError(resp.text[:300], resp.status_code) from None
        data = resp.json()
        if self.api == "v4":
            if data.get("code") not in (0, None):
                raise MinerUError(str(data.get("msg")), data.get("code"))
            return data.get("data") or {}
        return data

    # -- public API ---------------------------------------------------------------

    def parse_file(self, path: Path, out_dir: Path, opts: ParseOptions | None = None) -> ResultFiles:
        """Upload ``path``, wait for parsing and extract the result zip into ``out_dir``."""
        opts = opts or ParseOptions()
        path = Path(path)
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise MinerUError(f"unsupported file type {path.suffix!r}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise MinerUError(f"{path.name} is {size / 2**20:.0f} MB; MinerU accepts at most 200 MB")
        if path.suffix.lower() in {".html", ".htm"}:
            opts.model_version = "MinerU-HTML"
        zip_url = self._parse_v4(path, opts) if self.api == "v4" else self._parse_v1(path, opts)
        self.progress("downloading result")
        return self.download_result(zip_url, out_dir)

    def _parse_v4(self, path: Path, opts: ParseOptions) -> str:
        file_entry: dict = {"name": _ascii_name(path), "is_ocr": opts.is_ocr, "data_id": _data_id(path)}
        if opts.page_ranges:
            file_entry["page_ranges"] = opts.page_ranges
        body = {
            "files": [file_entry],
            "model_version": opts.model_version,
            "language": opts.language,
            "enable_formula": opts.enable_formula,
            "enable_table": opts.enable_table,
        }
        data = self._json(self._request("POST", f"{self.base}/file-urls/batch", json=body))
        batch_id, upload_url = data["batch_id"], data["file_urls"][0]
        self.progress(f"uploading {path.name}")
        self._put(upload_url, path)
        return self._poll_v4(batch_id, file_entry["name"])

    def _poll_v4(self, batch_id: str, file_name: str) -> str:
        deadline = time.time() + self.max_wait
        interval, last = self.poll_interval, ""
        while time.time() < deadline:
            data = self._json(self._request("GET", f"{self.base}/extract-results/batch/{batch_id}"))
            rows = data.get("extract_result") or []
            row = next((r for r in rows if r.get("file_name") == file_name), rows[0] if len(rows) == 1 else None)
            if row:
                state = row.get("state")
                if state == "done":
                    return row["full_zip_url"]
                if state == "failed":
                    raise MinerUError(row.get("err_msg") or "parse failed", row.get("err_code"))
                prog = row.get("extract_progress") or {}
                msg = (
                    f"{state} {prog.get('extracted_pages', '?')}/{prog.get('total_pages', '?')}" if prog else str(state)
                )
                if msg != last:
                    self.progress(f"MinerU: {msg}")
                    last = msg
            time.sleep(interval)
            interval = min(interval * 1.5, 30)
        raise MinerUError(f"timed out after {self.max_wait:.0f}s waiting for batch {batch_id}")

    def _parse_v1(self, path: Path, opts: ParseOptions) -> str:
        blob = path.read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        up = self._json(
            self._request(
                "POST",
                f"{self.base}/uploads",
                json={
                    "filename": _ascii_name(path),
                    "bytes": len(blob),
                    "mime_type": mime,
                    "purpose": "parse",
                    "sha256sum": sha,
                },
            )
        )
        if up.get("status") != "completed":
            self.progress(f"uploading {path.name}")
            method = up.get("upload_method") or "PUT"
            headers = up.get("upload_headers") or {}
            resp = self._request(method, up["upload_url"], authed=False, headers=headers, content=blob)
            if resp.status_code >= 400:
                raise MinerUError(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")
            up = self._json(self._request("POST", f"{self.base}/uploads/{up['id']}/complete", json={"sha256sum": sha}))
        file_id = (up.get("file") or {}).get("id") or up.get("file_id")
        source: dict = {"source": {"type": "file_id", "file_id": file_id}}
        if opts.page_ranges:
            source["page_range"] = opts.page_ranges
        job = self._json(
            self._request(
                "POST",
                f"{self.base}/parse/jobs",
                json={
                    "files": [source],
                    "ocr_mode": "ocr" if opts.is_ocr else "auto",
                    "output_formats": ["zip"],
                },
            )
        )
        job_id = job.get("job_id") or job.get("id")
        deadline, interval, last = time.time() + self.max_wait, self.poll_interval, ""
        while time.time() < deadline:
            job = self._json(self._request("GET", f"{self.base}/parse/jobs/{job_id}"))
            status = job.get("status")
            if status != last:
                self.progress(f"MinerU: {status}")
                last = status
            if status in {"completed", "partial", "failed", "canceled"}:
                files = job.get("files") or []
                out = (files[0].get("output_files") or {}).get("zip") if files else None
                if out:
                    # Cache hits sometimes report file_conversion_failed although the zip is
                    # complete; the caller verifies the extracted content.
                    if status != "completed":
                        self.progress(f"MinerU reported {status}; checking the returned zip")
                    return f"{self.base}/files/{out['file_id']}/content"
                err = (files[0].get("error") if files else None) or job.get("error") or {}
                err = err if isinstance(err, dict) else {"message": str(err)}
                raise MinerUError(err.get("message") or f"job {status}", err.get("code"))
            time.sleep(interval)
            interval = min(interval * 1.5, 30)
        raise MinerUError(f"timed out after {self.max_wait:.0f}s waiting for job {job_id}")

    def _put(self, url: str, path: Path) -> None:
        # Pre-signed OSS URL: no Authorization and no Content-Type, or the signature breaks.
        resp = self._request("PUT", url, authed=False, headers={}, content=path.read_bytes())
        if resp.status_code >= 400:
            raise MinerUError(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")

    def download_result(self, url: str, out_dir: Path) -> ResultFiles:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # v1 file-content URLs need auth when a token exists; CDN URLs must not get it.
        authed = self.api == "v1" and url.startswith(self.base) and bool(self.token)
        headers = {"Authorization": f"Bearer {self.token}"} if authed else {}
        zip_path = out_dir / "_result.zip"
        with self._http.stream("GET", url, headers=headers) as resp:
            if resp.status_code >= 400:
                raise MinerUError(f"download failed: HTTP {resp.status_code}")
            with zip_path.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        extract_zip(zip_path, out_dir)
        zip_path.unlink()
        files = locate_result_files(out_dir)
        if files.content_list is None and files.full_md is None:
            raise MinerUError("MinerU returned a result without content_list.json or full.md")
        return files


def extract_zip(zip_path: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise MinerUError("result zip is unreasonably large; refusing to extract")
        root = out_dir.resolve()
        for info in infos:
            target = (out_dir / info.filename).resolve()
            if not str(target).startswith(str(root)):
                raise MinerUError(f"unsafe path in zip: {info.filename}")
        zf.extractall(out_dir)


def locate_result_files(root: Path) -> ResultFiles:
    """Find MinerU outputs by suffix (prefixes are server-generated UUIDs)."""
    files = [p for p in Path(root).rglob("*") if p.is_file()]

    def pick(pred: Callable[[Path], bool]) -> Path | None:
        found = sorted((p for p in files if pred(p)), key=lambda p: len(p.parts))
        return found[0] if found else None

    content_list = pick(lambda p: p.name.endswith("content_list.json") and "_v2" not in p.name)
    middle = pick(lambda p: p.name == "layout.json" or p.name.endswith("_middle.json") or p.name == "middle_json.json")
    full_md = pick(lambda p: p.name in {"full.md", "markdown.md"}) or pick(lambda p: p.suffix == ".md")
    images = next((p for p in sorted(Path(root).rglob("images")) if p.is_dir()), None)
    return ResultFiles(Path(root), content_list, middle, full_md, images)


def _ascii_name(path: Path) -> str:
    """Upload name: keep the real extension (the server sniffs type from it)."""
    stem = "".join(c if c.isascii() and (c.isalnum() or c in "-_.") else "_" for c in path.stem)[:80]
    return f"{stem or 'document'}{path.suffix.lower()}"


def _data_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:32]


def _redact(url: str) -> str:
    return url.split("?", 1)[0]
