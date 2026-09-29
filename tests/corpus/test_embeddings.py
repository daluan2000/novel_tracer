from __future__ import annotations

from langchain_openai import OpenAIEmbeddings

from novel_agent.corpus.retrieval.embeddings import create_embedding_provider
from novel_agent.runtime.config import EmbeddingConfig


def test_embedding_provider_sends_raw_strings_to_compatible_apis() -> None:
    provider = create_embedding_provider(
        EmbeddingConfig(
            model_name="test-embedding",
            api_key="test-key",
            base_url="https://provider.example/v1",
        )
    )

    assert isinstance(provider, OpenAIEmbeddings)
    assert provider.check_embedding_ctx_length is False
    assert provider.chunk_size == 10
