"""Configuration: environment variables first, then the user config file.

Secrets never live in the library repository. They come from environment
variables or from the per-user file ``~/.config/pdfskill/config.toml``
(``$XDG_CONFIG_HOME`` and ``PDFSKILL_CONFIG`` are honoured)::

    [library]
    path = "~/pdfskill-library"

    [mineru]
    token = "..."          # or env MINERU_TOKEN
    api = "v4"             # "v4" (token) | "v1" (anonymous, rate limited)
    model_version = "vlm"
    language = "ch"

    [llm]
    provider = "deepseek"  # see pdfskill.llm.PROVIDERS
    model = "deepseek-chat"
    api_key = "..."        # or the provider's env var, or PDFSKILL_LLM_API_KEY
    base_url = ""          # override for any OpenAI-compatible endpoint
    concurrency = 4

    [translate]
    target_lang = "zh"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_path() -> Path:
    if os.environ.get("PDFSKILL_CONFIG"):
        return Path(os.environ["PDFSKILL_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "pdfskill" / "config.toml"


def load_file() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


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


@dataclass
class Settings:
    library: Path | None = None
    mineru: MinerUSettings = field(default_factory=MinerUSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    target_lang: str = "zh"
    config_file: Path | None = None


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def load_settings() -> Settings:
    data = load_file()
    s = Settings(config_file=config_path())
    lib = _env("PDFSKILL_LIBRARY") or (data.get("library") or {}).get("path")
    s.library = Path(lib).expanduser() if lib else None

    m = data.get("mineru") or {}
    s.mineru = MinerUSettings(
        token=_env("MINERU_TOKEN", "MINERU_API_KEY", "MINERU_API_TOKEN") or m.get("token"),
        api=_env("PDFSKILL_MINERU_API") or m.get("api") or "v4",
        model_version=_env("PDFSKILL_MINERU_MODEL") or m.get("model_version") or "vlm",
        language=m.get("language") or "ch",
        is_ocr=bool(m.get("is_ocr", False)),
    )
    if not s.mineru.token and s.mineru.api == "v4" and not (_env("PDFSKILL_MINERU_API") or m.get("api")):
        # No token configured anywhere: fall back to the anonymous v1 API.
        s.mineru.api = "v1"

    llm = data.get("llm") or {}
    s.llm = LLMSettings(
        provider=_env("PDFSKILL_LLM_PROVIDER") or llm.get("provider"),
        model=_env("PDFSKILL_LLM_MODEL") or llm.get("model"),
        api_key=_env("PDFSKILL_LLM_API_KEY") or llm.get("api_key"),
        base_url=_env("PDFSKILL_LLM_BASE_URL") or llm.get("base_url"),
        concurrency=int(_env("PDFSKILL_LLM_CONCURRENCY") or llm.get("concurrency") or 4),
        temperature=float(llm.get("temperature", 0.1)),
        timeout=float(llm.get("timeout", 180)),
        max_chunk_chars=int(llm.get("max_chunk_chars", 6000)),
    )
    s.target_lang = _env("PDFSKILL_TARGET_LANG") or (data.get("translate") or {}).get("target_lang") or "zh"
    return s
