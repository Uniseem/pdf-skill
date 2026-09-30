"""The library repository must be private.

pdfskill stores API keys inside the library (``.pdfskill/config.toml``) and pushes
the library to its git remotes, so every remote must be private. This module
decides whether a remote is safe:

* local remotes (paths, ``file://``) are safe;
* GitHub remotes are checked through the authenticated API when ``gh`` or
  ``GITHUB_TOKEN`` is available, and must report ``visibility == "private"``
  (``internal`` and ``public`` are rejected);
* every remote is also probed anonymously: ``git ls-remote`` with no system or
  global config, no credential helpers and no prompts. If it succeeds, anyone can
  read the repository and it is rejected.

A pure-shell ``pre-push`` hook repeats the anonymous probe, so a manual
``git push`` to a public remote is blocked too.

Results: ``local`` | ``private`` | ``public`` | ``unknown`` (could not verify).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CACHE_TTL = 3600
HOOK_MARKER = "# pdfskill-privacy-hook v2"

HOOK_SCRIPT = f"""#!/bin/sh
{HOOK_MARKER}
# This document library stores API keys. Never push it to a publicly readable remote.
# Installed by pdfskill; it re-installs itself if removed.
refs=$(cat)
url="$2"
case "$url" in
  /*|./*|../*|file://*) public=no ;;
  *)
    probe=$(printf '%s' "$url" | sed -E \\
      -e 's#^ssh://([^@/]+@)?([^/:]+)(:[0-9]+)?/#https://\\2/#' \\
      -e 's#^([^@/:]+@)?([^/:]+):([^/].*)$#https://\\2/\\3#' \\
      -e 's#^(https?://)[^@/]+@#\\1#')
    # anonymous probe, outside this repository and without any credential helper or global config
    if (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
        cd / && env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \\
          GIT_ASKPASS=false SSH_ASKPASS=false \\
          git -c credential.helper= ls-remote --heads "$probe" >/dev/null 2>&1); then
      public=yes
    else
      public=no
    fi ;;
esac
if [ "$public" = yes ]; then
  echo "pdfskill: push refused: $url is publicly readable, and this library contains API keys." >&2
  echo "pdfskill: make the repository private first (GitHub: gh repo edit OWNER/NAME --visibility private)." >&2
  exit 1
fi
chained="$(dirname "$0")/pre-push.pdfskill-chained"
if [ -x "$chained" ]; then
  printf '%s\\n' "$refs" | "$chained" "$@"
  exit $?
fi
exit 0
"""


class PrivacyError(RuntimeError):
    pass


@dataclass
class RemoteStatus:
    name: str
    url: str
    status: str  # local | private | public | unknown
    method: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "url": redact(self.url),
            "status": self.status,
            "method": self.method,
            "detail": self.detail,
        }


def redact(url: str) -> str:
    return re.sub(r"^(https?://)[^@/]+@", r"\1***@", url)


def is_local(url: str) -> bool:
    return url.startswith(("/", "./", "../", "file://", "~")) or bool(re.match(r"^[A-Za-z]:[\\/]", url))


def to_https(url: str) -> str:
    """``git@host:o/r.git`` / ``ssh://git@host/o/r`` / ``https://user:tok@host/o/r`` -> ``https://host/o/r``."""
    u = url.strip()
    m = re.match(r"^ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", u)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    m = re.match(r"^(?:[^@/:]+@)?([^/:]+):(?!//)(.+)$", u)
    if m and not u.startswith(("http://", "https://")):
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"^(https?://)[^@/]+@", r"\1", u)


def github_slug(url: str) -> str | None:
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", to_https(url))
    return f"{m.group(1)}/{m.group(2)}" if m else None


def anonymous_probe(url: str, timeout: float = 25) -> tuple[bool | None, str]:
    """True = readable without credentials, False = not readable, None = could not tell (network)."""
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "false",
        "SSH_ASKPASS": "false",
    }
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        env.pop(k, None)
    try:
        proc = subprocess.run(
            ["git", "-c", "credential.helper=", "ls-remote", "--heads", to_https(url)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=os.path.expanduser("~"),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"probe failed: {exc}"
    if proc.returncode == 0:
        return True, "anonymous git access works"
    err = proc.stderr.lower()
    if any(
        s in err
        for s in (
            "could not resolve host",
            "failed to connect",
            "timed out",
            "connection refused",
            "network is unreachable",
            "ssl",
            "couldn't connect",
        )
    ):
        return None, proc.stderr.strip().splitlines()[-1][:200] if proc.stderr.strip() else "network error"
    return False, "anonymous access denied"


def github_visibility(slug: str) -> tuple[str | None, str]:
    """Authenticated visibility (private/internal/public), or None if no credentials / no access."""
    gh = shutil.which("gh")
    if gh:
        try:
            proc = subprocess.run(
                [gh, "api", f"repos/{slug}", "--jq", ".visibility"], capture_output=True, text=True, timeout=30
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip(), "gh api"
        except (subprocess.TimeoutExpired, OSError):
            pass
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        import httpx

        try:
            r = httpx.get(
                f"https://api.github.com/repos/{slug}",
                timeout=20,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
            if r.status_code == 200:
                return r.json().get("visibility") or ("private" if r.json().get("private") else "public"), "api"
        except Exception:  # noqa: BLE001
            pass
    return None, ""


def check_url(name: str, url: str) -> RemoteStatus:
    if is_local(url):
        return RemoteStatus(name, url, "local", "path")
    slug = github_slug(url)
    if slug:
        vis, how = github_visibility(slug)
        if vis == "private":
            readable, detail = anonymous_probe(url)
            if readable:  # belt and braces: never trust the API over an actual anonymous read
                return RemoteStatus(name, url, "public", "anonymous git", detail)
            return RemoteStatus(name, url, "private", how, "visibility=private")
        if vis in {"public", "internal"}:
            return RemoteStatus(name, url, "public", how, f"visibility={vis}")
    readable, detail = anonymous_probe(url)
    if readable is True:
        return RemoteStatus(name, url, "public", "anonymous git", detail)
    if readable is False:
        return RemoteStatus(name, url, "private", "anonymous git", detail)
    return RemoteStatus(name, url, "unknown", "anonymous git", detail)


def remotes(root: Path) -> list[tuple[str, str]]:
    if not (Path(root) / ".git").exists() or not shutil.which("git"):
        return []
    proc = subprocess.run(["git", "-C", str(root), "remote", "-v"], capture_output=True, text=True)
    seen, out = set(), []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and (parts[0], parts[1]) not in seen:
            seen.add((parts[0], parts[1]))
            out.append((parts[0], parts[1]))
    return out


def check_library(root: Path, *, fresh: bool = False, cache_file: Path | None = None) -> list[RemoteStatus]:
    cache: dict = {}
    if cache_file and cache_file.exists() and not fresh:
        try:
            cache = json.loads(cache_file.read_text())
        except (OSError, ValueError):
            cache = {}
    now = time.time()
    results = []
    for name, url in remotes(root):
        hit = cache.get(url)
        if hit and not fresh and now - hit["t"] < CACHE_TTL and hit["status"] in {"private", "local"}:
            results.append(RemoteStatus(name, url, hit["status"], hit["method"] + " (cached)"))
            continue
        st = check_url(name, url)
        cache[url] = {"status": st.status, "method": st.method, "t": now}
        results.append(st)
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache))
    return results


def enforce(statuses: list[RemoteStatus], *, strict: bool) -> None:
    """Raise if any remote is public; with ``strict`` also if any cannot be verified."""
    public = [s for s in statuses if s.status == "public"]
    if public:
        lines = [f"  {s.name}: {redact(s.url)} ({s.detail or s.method})" for s in public]
        slug = next((github_slug(s.url) for s in public if github_slug(s.url)), None)
        fix = (
            f"gh repo edit {slug} --visibility private --accept-visibility-change-consequences"
            if slug
            else "make the repository private on its hosting service"
        )
        raise PrivacyError(
            "this library stores API keys and must only live in private repositories, but these remotes are "
            "publicly readable:\n" + "\n".join(lines) + f"\nFix: {fix}, or remove the remote (git remote remove NAME). "
            "If keys were already pushed there, rotate them."
        )
    if strict:
        unknown = [s for s in statuses if s.status == "unknown"]
        if unknown:
            lines = [f"  {s.name}: {redact(s.url)} ({s.detail})" for s in unknown]
            raise PrivacyError(
                "cannot verify that these remotes are private (offline?); refusing to write keys "
                "or push:\n" + "\n".join(lines)
            )


def hooks_dir(root: Path) -> Path | None:
    if not (Path(root) / ".git").exists():
        return None
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "hooks"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    p = Path(proc.stdout.strip())
    return p if p.is_absolute() else Path(root) / p


def install_hook(root: Path) -> Path | None:
    d = hooks_dir(root)
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    hook = d / "pre-push"
    if hook.exists():
        current = hook.read_text(errors="replace")
        if "# pdfskill-privacy-hook" in current:
            if current == HOOK_SCRIPT:
                return hook
        else:  # keep the user's own hook and chain to it
            hook.rename(d / "pre-push.pdfskill-chained")
    hook.write_text(HOOK_SCRIPT)
    hook.chmod(0o755)
    return hook
