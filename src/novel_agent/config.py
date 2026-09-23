from __future__ import annotations

import os
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
        )


def default_max_steps() -> int:
    load_dotenv()
    return int(os.getenv("NOVEL_AGENT_MAX_STEPS", "16"))
