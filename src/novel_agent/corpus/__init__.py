"""Novel loading, structure detection, chunking, and retrieval."""

from novel_agent.corpus.loader import load_novel
from novel_agent.corpus.repository import NovelCorpus

__all__ = ["NovelCorpus", "load_novel"]
