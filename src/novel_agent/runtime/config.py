from __future__ import annotations

import os
import math
from dataclasses import dataclass

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    model_name: str
    base_url: str | None = None
    temperature: float = 0.0
    thinking_mode: str | None = None
    request_timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "ModelConfig":
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "未配置 OPENAI_API_KEY。请复制 .env.example 为 .env，填写支持 Tool Calling 的模型配置。"
            )
        model_name = os.getenv("MODEL_NAME", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
        thinking_mode = os.getenv("MODEL_THINKING_MODE", "").strip().lower() or None
        if thinking_mode is None and model_name.lower().startswith("deepseek"):
            # DeepSeek thinking mode rejects the named tool_choice used by
            # function-calling structured output.
            thinking_mode = "disabled"
        if thinking_mode not in {None, "enabled", "disabled"}:
            raise RuntimeError("MODEL_THINKING_MODE 只能是 enabled、disabled 或留空。")
        return cls(
            api_key=api_key,
            model_name=model_name,
            base_url=os.getenv("OPENAI_BASE_URL", "").strip() or None,
            temperature=float(os.getenv("NOVEL_AGENT_TEMPERATURE", "0")),
            thinking_mode=thinking_mode,
            request_timeout_seconds=_timeout_from_env(
                "MODEL_REQUEST_TIMEOUT_SECONDS",
                120.0,
            ),
        )

    def create_model(self) -> ChatOpenAI:
        extra_body = (
            {"thinking": {"type": self.thinking_mode}}
            if self.thinking_mode is not None
            else None
        )
        return ChatOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model_name,
            temperature=self.temperature,
            extra_body=extra_body,
            timeout=self.request_timeout_seconds,
        )


@dataclass(frozen=True)
class EmbeddingConfig:
    """OpenAI-compatible embedding settings with chat credential fallbacks."""

    model_name: str
    api_key: str
    base_url: str | None = None
    request_timeout_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.model_name and self.api_key)

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        load_dotenv()
        return cls(
            model_name=os.getenv("EMBEDDING_MODEL", "").strip(),
            api_key=(
                os.getenv("EMBEDDING_API_KEY", "").strip()
                or os.getenv("OPENAI_API_KEY", "").strip()
            ),
            base_url=(
                os.getenv("EMBEDDING_BASE_URL", "").strip()
                or os.getenv("OPENAI_BASE_URL", "").strip()
                or None
            ),
            request_timeout_seconds=_timeout_from_env(
                "EMBEDDING_REQUEST_TIMEOUT_SECONDS",
                30.0,
            ),
        )


def _timeout_from_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是 5 到 600 之间的秒数。") from exc
    if not math.isfinite(value) or not 5 <= value <= 600:
        raise RuntimeError(f"{name} 必须是 5 到 600 之间的秒数。")
    return value


def default_max_steps() -> int:
    load_dotenv()
    return int(os.getenv("NOVEL_AGENT_MAX_STEPS", "16"))


def structured_output_retries() -> int:
    load_dotenv()
    raw_value = os.getenv("NOVEL_AGENT_STRUCTURED_RETRIES", "2").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("NOVEL_AGENT_STRUCTURED_RETRIES 必须是 0 到 5 之间的整数。") from exc
    if not 0 <= value <= 5:
        raise RuntimeError("NOVEL_AGENT_STRUCTURED_RETRIES 必须是 0 到 5 之间的整数。")
    return value
