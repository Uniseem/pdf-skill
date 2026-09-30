"""Multi-provider LLM access over the OpenAI-compatible chat-completions protocol.

One small profile table covers the per-provider quirks (base URL, key env var,
temperature range, JSON mode, thinking switches). Any other OpenAI-compatible
endpoint works through ``base_url``. The same settings drive the ingest
conversion and the retain-pdf translation.

Responses are cached in SQLite (keyed by endpoint + request body), so a long
conversion that fails half-way resumes without paying twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Provider:
    base_url: str
    key_env: tuple[str, ...]
    default_model: str
    json_mode: bool = True
    temp_range: tuple[float, float] = (0.0, 2.0)
    max_tokens_param: str = "max_tokens"
    extra_body: dict[str, Any] = field(default_factory=dict)
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider("https://api.deepseek.com", ("DEEPSEEK_API_KEY",), "deepseek-chat"),
    "dashscope": Provider(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ("DASHSCOPE_API_KEY",),
        "qwen-plus",
        extra_body={"enable_thinking": False},
        note="Qwen (Alibaba Cloud, China)",
    ),
    "dashscope-intl": Provider(
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ("DASHSCOPE_API_KEY",),
        "qwen-plus",
        extra_body={"enable_thinking": False},
        note="Qwen (Singapore)",
    ),
    "zhipu": Provider(
        "https://open.bigmodel.cn/api/paas/v4/",
        ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY"),
        "glm-4.5-flash",
        temp_range=(0.01, 0.99),
        extra_body={"thinking": {"type": "disabled"}},
        note="GLM (Zhipu, China)",
    ),
    "zai": Provider(
        "https://api.z.ai/api/paas/v4/",
        ("ZAI_API_KEY",),
        "glm-4.5-flash",
        temp_range=(0.01, 0.99),
        extra_body={"thinking": {"type": "disabled"}},
        note="GLM (Z.ai)",
    ),
    "moonshot": Provider(
        "https://api.moonshot.cn/v1",
        ("MOONSHOT_API_KEY",),
        "kimi-k2-turbo-preview",
        temp_range=(0.0, 1.0),
        note="Kimi (China)",
    ),
    "moonshot-intl": Provider(
        "https://api.moonshot.ai/v1",
        ("MOONSHOT_API_KEY",),
        "kimi-k2-turbo-preview",
        temp_range=(0.0, 1.0),
        note="Kimi (international)",
    ),
    "siliconflow": Provider("https://api.siliconflow.cn/v1", ("SILICONFLOW_API_KEY",), "deepseek-ai/DeepSeek-V3"),
    "siliconflow-intl": Provider("https://api.siliconflow.com/v1", ("SILICONFLOW_API_KEY",), "deepseek-ai/DeepSeek-V3"),
    "volcengine": Provider(
        "https://ark.cn-beijing.volces.com/api/v3",
        ("ARK_API_KEY", "VOLCENGINE_API_KEY"),
        "doubao-seed-1-6-flash-250828",
        note="Doubao (Volcengine Ark); model = endpoint id",
    ),
    "minimax": Provider("https://api.minimaxi.com/v1", ("MINIMAX_API_KEY",), "MiniMax-M2"),
    "openrouter": Provider("https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",), "deepseek/deepseek-chat"),
    "gemini": Provider(
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "gemini-2.5-flash",
    ),
    "anthropic": Provider(
        "https://api.anthropic.com/v1/",
        ("ANTHROPIC_API_KEY",),
        "claude-haiku-4-5",
        json_mode=False,
        temp_range=(0.0, 1.0),
    ),
    "openai": Provider(
        "https://api.openai.com/v1",
        ("OPENAI_API_KEY",),
        "gpt-5-mini",
        max_tokens_param="max_completion_tokens",
        temp_range=(1.0, 1.0),
    ),
    "groq": Provider("https://api.groq.com/openai/v1", ("GROQ_API_KEY",), "llama-3.3-70b-versatile"),
    "mistral": Provider(
        "https://api.mistral.ai/v1", ("MISTRAL_API_KEY",), "mistral-small-latest", temp_range=(0.0, 1.0)
    ),
    "together": Provider("https://api.together.xyz/v1", ("TOGETHER_API_KEY",), "deepseek-ai/DeepSeek-V3"),
    "xai": Provider("https://api.x.ai/v1", ("XAI_API_KEY",), "grok-3-mini"),
    "ollama": Provider("http://localhost:11434/v1", ("OLLAMA_API_KEY",), "qwen2.5:7b", note="local; any key"),
    "lmstudio": Provider("http://localhost:1234/v1", ("LMSTUDIO_API_KEY",), "local-model", note="local"),
    "vllm": Provider("http://localhost:8000/v1", ("VLLM_API_KEY",), "local-model", note="local"),
}
LOCAL_PROVIDERS = {"ollama", "lmstudio", "vllm"}
GENERIC = Provider("", ("PDFSKILL_LLM_API_KEY",), "")

_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.S)


class LLMError(RuntimeError):
    pass


@dataclass
class ResolvedLLM:
    provider: str
    profile: Provider
    model: str
    base_url: str
    api_key: str | None

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def detect_provider() -> str | None:
    """Pick the first provider whose API key is present in the environment."""
    for name, p in PROVIDERS.items():
        if name in LOCAL_PROVIDERS or name.endswith("-intl") or name == "zai":
            continue
        if any(os.environ.get(k) for k in p.key_env):
            return name
    return None


def resolve(settings) -> ResolvedLLM | None:
    """Resolve LLM settings (``pdfskill.config.LLMSettings``); None when nothing is configured."""
    name = settings.provider or (None if settings.base_url else detect_provider())
    if name is None and not settings.base_url:
        return None
    profile = PROVIDERS.get(name or "", GENERIC)
    if name and name not in PROVIDERS and not settings.base_url:
        raise LLMError(f"unknown provider {name!r}; known: {', '.join(PROVIDERS)} (or set base_url)")
    key = settings.api_key or next((os.environ[k] for k in profile.key_env if os.environ.get(k)), None)
    if not key and (name in LOCAL_PROVIDERS):
        key = "local"
    model = settings.model or profile.default_model
    if not model:
        raise LLMError("set llm.model (PDFSKILL_LLM_MODEL) when using a custom base_url")
    base_url = (settings.base_url or profile.base_url).rstrip("/")
    return ResolvedLLM(name or "custom", profile, model, base_url, key)


class LLM:
    def __init__(
        self, resolved: ResolvedLLM, *, cache_path: Path | None = None, timeout: float = 180.0, temperature: float = 0.1
    ):
        from openai import OpenAI

        if not resolved.api_key:
            envs = " or ".join(resolved.profile.key_env) or "PDFSKILL_LLM_API_KEY"
            raise LLMError(f"no API key for {resolved.provider}: set {envs} (or llm.api_key in the config file)")
        self.r = resolved
        self.temperature = temperature
        self.client = OpenAI(base_url=resolved.base_url, api_key=resolved.api_key, max_retries=5, timeout=timeout)
        self._lock = threading.Lock()
        self.db = None
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(cache_path), check_same_thread=False)
            self.db.execute("create table if not exists c (k text primary key, v text)")
        self.calls = 0
        self.cached = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def _key(self, body: dict) -> str:
        blob = json.dumps([self.r.base_url, body], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def complete(
        self, messages: list[dict], *, temperature: float | None = None, max_tokens: int = 8192, json_mode: bool = False
    ) -> tuple[str, str]:
        """Return ``(text, finish_reason)``."""
        p = self.r.profile
        lo, hi = p.temp_range
        t = self.temperature if temperature is None else temperature
        body: dict[str, Any] = {
            "model": self.r.model,
            "messages": messages,
            "temperature": min(max(t, lo), hi),
            p.max_tokens_param: max_tokens,
        }
        if json_mode and p.json_mode:
            body["response_format"] = {"type": "json_object"}
        key = self._key(body)
        if self.db is not None:
            with self._lock:
                row = self.db.execute("select v from c where k=?", (key,)).fetchone()
            if row:
                self.cached += 1
                return row[0], "stop"
        try:
            resp = self.client.chat.completions.create(**body, extra_body=p.extra_body or None)
        except Exception as exc:  # openai.APIError and friends
            status = getattr(exc, "status_code", None)
            raise LLMError(f"{self.r.label} request failed ({status or type(exc).__name__}): {exc}") from exc
        self.calls += 1
        if getattr(resp, "usage", None):
            self.usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            self.usage["completion_tokens"] += resp.usage.completion_tokens or 0
        choice = resp.choices[0]
        text = _THINK.sub("", choice.message.content or "")
        finish = choice.finish_reason or "stop"
        if self.db is not None and finish == "stop" and text.strip():
            with self._lock:
                self.db.execute("insert or replace into c values (?,?)", (key, text))
                self.db.commit()
        return text, finish

    def json(
        self, messages: list[dict], validate: Callable[[Any], T | None], *, attempts: int = 3, max_tokens: int = 8192
    ) -> T | None:
        """Ask for JSON, repair it, validate it; feed failures back; None if it never validates."""
        import json_repair

        msgs = list(messages)
        for i in range(attempts):
            try:
                text, finish = self.complete(
                    msgs, json_mode=True, temperature=self.temperature + 0.2 * i, max_tokens=max_tokens
                )
            except LLMError:
                if i == attempts - 1:
                    raise
                continue
            if finish == "length":
                return None
            try:
                value = validate(json_repair.loads(text))
            except (ValueError, TypeError, KeyError):
                value = None
            if value is not None:
                return value
            msgs = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "That JSON failed validation (missing or extra keys, "
                    "or illegal values). Return the corrected JSON object only.",
                },
            ]
        return None

    def stats(self) -> dict:
        return {"model": self.r.label, "calls": self.calls, "cached": self.cached, **self.usage}
