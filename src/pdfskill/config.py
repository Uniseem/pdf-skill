"""Configuration: environment variables > library config > user config.

Two TOML files share one schema:

* library config ``<library>/.pdfskill/config.toml`` - committed and pushed with
  the library, so API keys follow the library to every machine that clones it.
  pdfskill therefore refuses to run on a library with a publicly readable remote
  (see :mod:`pdfskill.privacy`).
* user config ``~/.config/pdfskill/config.toml`` (``$XDG_CONFIG_HOME`` and
  ``PDFSKILL_CONFIG`` are honoured) - per machine, never pushed.

::

    [library]              # user config only
    path = "~/pdfskill-library"

    [mineru]
    token = "..."          # env MINERU_TOKEN wins
    api = "v4"             # "v4" (token) | "v1" (anonymous, rate limited)
    model_version = "vlm"
    language = "ch"

    [llm]
    provider = "deepseek"  # see `pdfskill providers`
    model = "deepseek-chat"
    api_key = "..."        # env PDFSKILL_LLM_API_KEY wins
    base_url = ""          # any OpenAI-compatible endpoint
    concurrency = 4

    [keys]                 # provider keys by env-var name, used when the env var is unset
    DEEPSEEK_API_KEY = "..."

    [translate]
    target_lang = "zh"

Set values with ``pdfskill config set KEY VALUE`` (``VALUE`` = ``-`` reads stdin).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LIBRARY_CONFIG = Path(".pdfskill") / "config.toml"
SECRET_KEYS = {"mineru.token", "llm.api_key"}
SETTABLE = {
    "mineru.token",
    "mineru.api",
    "mineru.model_version",
    "mineru.language",
    "mineru.is_ocr",
    "llm.provider",
    "llm.model",
    "llm.api_key",
    "llm.base_url",
    "llm.concurrency",
    "llm.temperature",
    "llm.timeout",
    "llm.max_chunk_chars",
    "translate.target_lang",
    "library.path",
}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    if os.environ.get("PDFSKILL_CONFIG"):
        return Path(os.environ["PDFSKILL_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "pdfskill" / "config.toml"


def read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_file() -> dict:
    return read_toml(config_path())


def is_secret(key: str) -> bool:
    return key in SECRET_KEYS or key.startswith("keys.")


def validate_key(key: str, *, library: bool) -> None:
    if key.startswith("keys."):
        if not _ENV_NAME.match(key[5:]):
            raise ConfigError(f"{key!r}: keys.* takes an environment-variable name, e.g. keys.DEEPSEEK_API_KEY")
        return
    if key not in SETTABLE:
        raise ConfigError(f"unknown setting {key!r}; settable: {', '.join(sorted(SETTABLE))}, keys.<ENV_VAR>")
    if library and key == "library.path":
        raise ConfigError("library.path belongs in the user config (use --user)")


def _coerce(key: str, value: str):
    if key in {"llm.concurrency", "llm.max_chunk_chars"}:
        return int(value)
    if key in {"llm.temperature", "llm.timeout"}:
        return float(value)
    if key == "mineru.is_ocr":
        return value.lower() in {"1", "true", "yes", "on"}
    return value


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)


def dump_toml(data: dict, header: str = "") -> str:
    """Minimal TOML writer for pdfskill's flat two-level config."""
    lines = [header.rstrip()] if header else []
    for section in sorted(data):
        table = data[section]
        if not isinstance(table, dict) or not table:
            continue
        lines += ["", f"[{section}]"] if lines else [f"[{section}]"]
        for k in sorted(table):
            key = k if re.match(r"^[A-Za-z0-9_-]+$", k) else json.dumps(k)
            lines.append(f"{key} = {_toml_value(table[k])}")
    return "\n".join(lines).strip() + "\n"


LIBRARY_CONFIG_HEADER = """\
# pdfskill library config. It is committed and pushed with the library and MAY
# contain API keys, so this repository must stay private. pdfskill enforces this:
# it refuses to run or push when a remote is publicly readable.
# Edit with: pdfskill config set KEY VALUE   (VALUE "-" reads stdin)"""


def set_value(path: Path, key: str, value, *, header: str = "") -> None:
    data = read_toml(path)
    section, _, name = key.partition(".")
    data.setdefault(section, {})[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_toml(data, header), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def unset_value(path: Path, key: str, *, header: str = "") -> bool:
    data = read_toml(path)
    section, _, name = key.partition(".")
    if name not in data.get(section, {}):
        return False
    del data[section][name]
    path.write_text(dump_toml(data, header), encoding="utf-8")
    return True


@dataclass
class MinerUSettings:
    token: str | None = None
    api: str = "v4"
    model_version: str = "vlm"
    language: str = "ch"
    is_ocr: bool = False


@dataclass
class LLMSettings:
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    concurrency: int = 4
    temperature: float = 0.1
    timeout: float = 180.0
    max_chunk_chars: int = 6000
    keys: dict[str, str] = field(default_factory=dict)  # provider env-var name -> key (from config files)


@dataclass
class Settings:
    library: Path | None = None
    mineru: MinerUSettings = field(default_factory=MinerUSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    target_lang: str = "zh"
    config_file: Path | None = None
    library_config: Path | None = None
    sources: dict[str, str] = field(default_factory=dict)  # setting -> env | library | user


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def load_settings(library_root: Path | None = None) -> Settings:
    user = load_file()
    lib_path = Path(library_root) / LIBRARY_CONFIG if library_root else None
    libcfg = read_toml(lib_path) if lib_path else {}
    s = Settings(config_file=config_path(), library_config=lib_path)

    def pick(key: str, *envs: str, default=None):
        section, _, name = key.partition(".")
        v = _env(*envs) if envs else None
        if v is not None:
            s.sources[key] = "env"
            return v
        for label, data in (("library", libcfg), ("user", user)):
            v = (data.get(section) or {}).get(name)
            if v not in (None, ""):
                s.sources[key] = label
                return v
        return default

    lib = _env("PDFSKILL_LIBRARY") or (user.get("library") or {}).get("path")
    s.library = Path(lib).expanduser() if lib else None

    explicit_api = pick("mineru.api", "PDFSKILL_MINERU_API")
    s.mineru = MinerUSettings(
        token=pick("mineru.token", "MINERU_TOKEN", "MINERU_API_KEY", "MINERU_API_TOKEN"),
        api=explicit_api or "v4",
        model_version=pick("mineru.model_version", "PDFSKILL_MINERU_MODEL", default="vlm"),
        language=pick("mineru.language", default="ch"),
        is_ocr=bool(pick("mineru.is_ocr", default=False)),
    )
    if not s.mineru.token and not explicit_api:
        s.mineru.api = "v1"  # no token anywhere: anonymous v1 API

    keys: dict[str, str] = {}
    for data in (user, libcfg):  # library keys override user keys
        keys.update({k: str(v) for k, v in (data.get("keys") or {}).items() if v})
    s.llm = LLMSettings(
        provider=pick("llm.provider", "PDFSKILL_LLM_PROVIDER"),
        model=pick("llm.model", "PDFSKILL_LLM_MODEL"),
        api_key=pick("llm.api_key", "PDFSKILL_LLM_API_KEY"),
        base_url=pick("llm.base_url", "PDFSKILL_LLM_BASE_URL"),
        concurrency=int(pick("llm.concurrency", "PDFSKILL_LLM_CONCURRENCY", default=4)),
        temperature=float(pick("llm.temperature", default=0.1)),
        timeout=float(pick("llm.timeout", default=180)),
        max_chunk_chars=int(pick("llm.max_chunk_chars", default=6000)),
        keys=keys,
    )
    s.target_lang = pick("translate.target_lang", "PDFSKILL_TARGET_LANG", default="zh")
    return s
