from __future__ import annotations

from langchain_openai import OpenAIEmbeddings

from novel_agent.corpus.retrieval.models import EmbeddingProvider
from novel_agent.runtime.config import EmbeddingConfig


def create_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    if not config.enabled:
        raise ValueError("Embedding 配置不完整。")
    return OpenAIEmbeddings(
        model=config.model_name,
        api_key=config.api_key,
        base_url=config.base_url,
        chunk_size=10,
        timeout=config.request_timeout_seconds,
    )
