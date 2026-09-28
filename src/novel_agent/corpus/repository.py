from __future__ import annotations

from pathlib import Path

from novel_agent.corpus.loader import load_novel
from novel_agent.corpus.models import NovelChunk, NovelDocument, SearchHit
from novel_agent.corpus.retrieval.embeddings import create_embedding_provider
from novel_agent.corpus.retrieval.service import RetrievalService
from novel_agent.runtime.config import EmbeddingConfig


class NovelCorpus:
    """一部已解析小说的只读查询层，也是 Agent 工具访问原文的统一入口。

    ``NovelDocument`` 保存加载、章节识别和分块后的完整数据；本类在它上面建立
    若干内存索引，并提供结构浏览、关键词搜索、上下文读取和引文校验能力。
    Researcher 不直接操作原始 TXT，而是通过 tools.py 间接调用这里的方法。
    """

    def __init__(self, document: NovelDocument):
        """保存解析后的文档，并建立按 chunk/section 查询所需的内存索引。"""

        self.document = document

        # chunk_id -> NovelChunk：用于 get_chunk() 和引文校验的 O(1) 精确查找。
        self._chunk_by_id = {chunk.chunk_id: chunk for chunk in document.chunks}

        # section_id -> 该章节包含的 Chunk：用于按章节、按范围读取原文。
        self._section_chunks: dict[str, list[NovelChunk]] = {}
        for chunk in document.chunks:
            self._section_chunks.setdefault(chunk.section_id, []).append(chunk)

        # chunk_id -> 全书 Chunk 序号：用于读取命中块前后的相邻上下文。
        self._global_index = {chunk.chunk_id: index for index, chunk in enumerate(document.chunks)}
        self._retrieval = RetrievalService(document)

    @classmethod
    def from_path(cls, path: str | Path) -> "NovelCorpus":
        """从 TXT 路径加载、识别章节并切块，最后构造可查询的语料库。"""

        return cls(load_novel(path))

    def structure_summary(self) -> dict:
        """返回全书结构摘要，供界面展示或 Agent 浏览目录。

        摘要包含编码、字符/行/章节/Chunk 数量、结构识别报告，以及每个章节
        的行号范围和 Chunk 数；不包含大段正文。
        """

        document = self.document
        return {
            "source_path": document.source.source_path,
            "encoding": document.source.encoding,
            "character_count": len(document.source.text),
            "line_count": len(document.source.lines),
            "section_count": len(document.sections),
            "chunk_count": len(document.chunks),
            "structure": document.structure.model_dump(),
            "sections": [
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "detected": section.detected,
                    "confidence": section.confidence,
                    "start_line": section.start_line,
                    "end_line": section.end_line,
                    "chunk_count": len(self._section_chunks.get(section.section_id, [])),
                }
                for section in document.sections
            ],
        }

    def search(self, keyword: str, top_k: int = 5) -> list[SearchHit]:
        """Search passage-level lexical and dense indexes with lexical fallback."""

        return self._retrieval.search(keyword, top_k)

    def retrieval_status(self) -> dict:
        """Return safe, public index readiness information."""

        return self._retrieval.status()

    def prepare_dense_index(self, config: EmbeddingConfig) -> bool:
        """Mark dense indexing as building, or degraded when it is not configured."""

        return self._retrieval.prepare_dense(
            model_name=config.model_name,
            configured=config.enabled,
        )

    def build_dense_index(
        self,
        config: EmbeddingConfig,
        cache_root: str | Path = "output/indexes",
    ) -> bool:
        """Build or load the dense index; failures leave lexical search available."""

        try:
            provider = create_embedding_provider(config)
            self._retrieval.build_dense_index(
                provider,
                model_name=config.model_name,
                cache_root=Path(cache_root),
            )
        except Exception:
            self._retrieval.mark_dense_failed()
            return False
        return True

    def read_context(self, chunk_id: str, before: int = 1, after: int = 1) -> list[NovelChunk]:
        """按全书顺序读取目标 Chunk 及其前后相邻块。

        邻接关系跨章节也有效；before/after 被限制为 0～5，越过书首或书尾时
        自动截断。Agent 工具层会进一步把可请求范围限制为前后各 1 块。
        """

        if chunk_id not in self._global_index:
            raise KeyError(f"不存在的 chunk_id：{chunk_id}")
        before = min(max(before, 0), 5)
        after = min(max(after, 0), 5)
        index = self._global_index[chunk_id]
        start = max(0, index - before)
        end = min(len(self.document.chunks), index + after + 1)
        return self.document.chunks[start:end]

    def read_section(self, section_id: str, start_chunk: int = 0, limit: int = 3) -> list[NovelChunk]:
        """分页读取某个章节中的 Chunk。

        ``start_chunk`` 是章节内部的零基下标，不是全书 Chunk 编号；``limit``
        在 repository 层最多为 5，Agent 工具层最多开放 3。
        """

        if section_id not in self._section_chunks:
            raise KeyError(f"不存在或没有正文 Chunk 的 section_id：{section_id}")
        if start_chunk < 0:
            raise ValueError("start_chunk 不能为负数。")
        limit = min(max(limit, 1), 5)
        return self._section_chunks[section_id][start_chunk : start_chunk + limit]

    def get_chunk(self, chunk_id: str) -> NovelChunk:
        """根据稳定的 chunk_id 精确取得一个 Chunk，不存在时抛出 KeyError。"""

        if chunk_id not in self._chunk_by_id:
            raise KeyError(f"不存在的 chunk_id：{chunk_id}")
        return self._chunk_by_id[chunk_id]

    def validate_quote(self, chunk_id: str, quote: str) -> bool:
        """确认引文是否逐字存在于指定 Chunk，防止模型伪造或改写原文。

        Assessor 提取证据后会调用此方法；只有返回 True 的引文才会进入最终
        evidence。这里做连续子串校验，不进行模糊匹配或语义判断。
        """

        if not quote.strip():
            return False
        chunk = self.get_chunk(chunk_id)
        return quote.strip() in chunk.text
