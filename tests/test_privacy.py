"""The library must only ever live in private repositories (it stores API keys)."""

import json
import os
import subprocess

import pytest
from conftest import SAMPLE_PDF

from pdfskill import cli, privacy
from pdfskill.config import load_settings
from pdfskill.ingest import IngestOptions, ingest
from pdfskill.library import Library
from pdfskill.privacy import PrivacyError, RemoteStatus

NETWORK = os.environ.get("PDFSKILL_NETWORK_TESTS") == "1"


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check)


def fake_status(monkeypatch, status: str):
    calls = []

    def check_url(name, url):
        calls.append(url)
        if privacy.is_local(url):
            return RemoteStatus(name, url, "local", "path")
        return RemoteStatus(name, url, status, "test", "faked")

    monkeypatch.setattr(privacy, "check_url", check_url)
    return calls


def test_url_helpers():
    assert privacy.to_https("git@github.com:o/r.git") == "https://github.com/o/r.git"
    assert privacy.to_https("ssh://git@host:2222/t/r.git") == "https://host/t/r.git"
    assert privacy.to_https("https://u:tok@github.com/o/r") == "https://github.com/o/r"
    assert privacy.github_slug("git@github.com:Uniseem/lib.git") == "Uniseem/lib"
    assert privacy.github_slug("https://gitlab.com/o/r.git") is None
    assert privacy.is_local("/srv/lib.git") and privacy.is_local("file:///x") and not privacy.is_local("git@h:o/r")
    assert privacy.redact("https://u:tok@h/o/r") == "https://***@h/o/r"


def test_enforce():
    ok = [RemoteStatus("origin", "https://h/o/r", "private", "t"), RemoteStatus("b", "/x", "local", "path")]
    privacy.enforce(ok, strict=True)
    with pytest.raises(PrivacyError, match="gh repo edit o/r --visibility private"):
        privacy.enforce([RemoteStatus("origin", "https://github.com/o/r.git", "public", "t")], strict=False)
    unknown = [RemoteStatus("origin", "https://h/o/r", "unknown", "t", "offline")]
    privacy.enforce(unknown, strict=False)
    with pytest.raises(PrivacyError, match="cannot verify"):
        privacy.enforce(unknown, strict=True)


def test_hook_installed_and_chains_existing_hook(tmp_path):
    lib = Library(tmp_path / "lib")
    hooks = tmp_path / "lib" / ".git" / "hooks"
    lib.root.mkdir()
    git(lib.root, "init", "-q")
    hooks.mkdir(parents=True, exist_ok=True)
    mine = hooks / "pre-push"
    mine.write_text(f"#!/bin/sh\ncat > {tmp_path}/stdin.txt\nexit 0\n")
    mine.chmod(0o755)
    lib.init()
    hook = hooks / "pre-push"
    assert privacy.HOOK_MARKER in hook.read_text() and os.access(hook, os.X_OK)
    assert (hooks / "pre-push.pdfskill-chained").exists()
    # local remote: allowed, and the chained hook receives git's ref lines on stdin
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(lib.root, "remote", "add", "origin", str(bare))
    assert git(lib.root, "push", "-q", "origin", "HEAD", check=False).returncode == 0
    assert "refs/heads/" in (tmp_path / "stdin.txt").read_text()
    # the hook re-installs itself if deleted
    hook.unlink()
    lib.guard()
    assert hook.exists()


def test_public_remote_blocks_every_command(library, monkeypatch, capsys):
    fake_status(monkeypatch, "public")
    git(library.root, "remote", "add", "origin", "https://github.com/someone/lib.git")
    assert cli.main(["-L", str(library.root), "search", "x"]) == 1
    err = capsys.readouterr().err
    assert "publicly readable" in err and "gh repo edit someone/lib --visibility private" in err
    assert cli.main(["-L", str(library.root), "ingest", str(SAMPLE_PDF), "--no-llm", "-q"]) == 1
    assert cli.main(["-L", str(library.root), "config", "set", "keys.DEEPSEEK_API_KEY", "sk-secret"]) == 1
    assert not (library.root / ".pdfskill" / "config.toml").exists()
    # doctor still works and explains the problem
    assert cli.main(["-L", str(library.root), "doctor", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert any("PUBLIC" in p for p in doc["problems"])


def test_unknown_remote_allows_reads_but_not_key_writes(library, monkeypatch, capsys):
    fake_status(monkeypatch, "unknown")
    git(library.root, "remote", "add", "origin", "https://git.example.com/me/lib.git")
    assert cli.main(["-L", str(library.root), "list"]) == 0
    assert "could not verify" in capsys.readouterr().err
    assert cli.main(["-L", str(library.root), "config", "set", "mineru.token", "tok"]) == 1
    assert "cannot verify" in capsys.readouterr().err


def test_init_refuses_existing_public_repo(tmp_path, monkeypatch):
    fake_status(monkeypatch, "public")
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", "https://github.com/me/public-project.git")
    with pytest.raises(PrivacyError):
        Library(root).init()
    assert not (root / ".pdfskill").exists()


def test_keys_in_library_config_are_committed_pushed_and_used(library, monkeypatch, capsys, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    assert cli.main(["-L", str(library.root), "remote", "add", str(bare)]) == 0
    assert "pushed to origin" in capsys.readouterr().out
    assert cli.main(["-L", str(library.root), "config", "set", "keys.DEEPSEEK_API_KEY", "sk-test-1234567890"]) == 0
    out = capsys.readouterr().out
    assert "sk-test-1234567890" not in out and "sk-t" in out and "pushed to origin" in out
    cfg = (library.root / ".pdfskill" / "config.toml").read_text()
    assert 'DEEPSEEK_API_KEY = "sk-test-1234567890"' in cfg and "must stay private" in cfg
    log = git(bare, "log", "--format=%s", "main").stdout
    assert "config: set keys.DEEPSEEK_API_KEY" in log and "sk-test" not in log
    # settings: library config is used; env beats library
    s = load_settings(library.root)
    assert s.llm.keys["DEEPSEEK_API_KEY"] == "sk-test-1234567890"
    from pdfskill.llm import resolve

    assert resolve(s.llm).provider == "deepseek" and resolve(s.llm).api_key == "sk-test-1234567890"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    assert resolve(load_settings(library.root).llm).api_key == "sk-from-env"
    # stdin form and unset
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("tok-from-stdin\n"))
    assert cli.main(["-L", str(library.root), "config", "set", "mineru.token", "-", "--json"]) == 0
    assert load_settings(library.root).mineru.token == "tok-from-stdin"
    assert load_settings(library.root).sources["mineru.token"] == "library"
    assert cli.main(["-L", str(library.root), "config", "unset", "mineru.token"]) == 0
    assert load_settings(library.root).mineru.token is None
    # ingest commits and pushes too
    capsys.readouterr()
    res = ingest(library, SAMPLE_PDF, load_settings(library.root), IngestOptions(use_llm=False))
    assert res["push"]["pushed"] is True
    assert git(bare, "log", "-1", "--format=%s", "main").stdout.startswith("ingest:")


def test_config_rejects_unknown_keys(library, capsys):
    assert cli.main(["-L", str(library.root), "config", "set", "llm.apikey", "x"]) == 1
    assert "unknown setting" in capsys.readouterr().err
    assert cli.main(["-L", str(library.root), "config", "set", "keys.lower", "x"]) == 1


@pytest.mark.skipif(not NETWORK, reason="set PDFSKILL_NETWORK_TESTS=1")
def test_real_visibility_checks():
    assert privacy.check_url("o", "https://github.com/octocat/Hello-World.git").status == "public"
    assert privacy.check_url("o", "git@github.com:octocat/Hello-World.git").status == "public"
    # not anonymously readable (does not exist) -> treated as private
    assert privacy.check_url("o", "https://github.com/octocat/pdfskill-no-such-repo-xyz.git").status == "private"


@pytest.mark.skipif(not NETWORK, reason="set PDFSKILL_NETWORK_TESTS=1")
def test_real_hook_blocks_public_push(library):
    """--dry-run: the hook runs, nothing is ever sent."""
    git(library.root, "remote", "add", "pub", "https://github.com/Uniseem/pdf-skill.git")
    proc = git(library.root, "push", "--dry-run", "pub", "HEAD:refs/heads/pdfskill-hook-test", check=False)
    assert proc.returncode != 0 and "push refused" in proc.stderr
