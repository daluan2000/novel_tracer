from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from novel_agent.models import NovelChunk, NovelDocument, SearchHit
from novel_agent.novel_loader import load_novel


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

    @staticmethod
    def _query_terms(keyword: str) -> list[str]:
        """规范化搜索词，同时保留完整短语和按空格拆开的单词，并进行去重。"""

        normalized = re.sub(r"\s+", " ", keyword).strip().casefold()
        if not normalized:
            return []
        terms = [term for term in normalized.split(" ") if term]
        return list(dict.fromkeys([normalized, *terms]))

    def search(self, keyword: str, top_k: int = 5) -> list[SearchHit]:
        """使用透明的关键词频次评分搜索相关 Chunk。

        完整短语命中的权重最高；各拆分词命中会累加分数，全部拆分词同时出现
        还有额外加分。结果按“分数降序、原文位置升序”排列，并返回命中附近
        的短片段。这里不是语义/向量搜索，因此同义词需要 Researcher 改写查询。
        """

        terms = self._query_terms(keyword)
        if not terms:
            raise ValueError("搜索关键词不能为空。")

        # repository 层也设置上限，避免调用方绕过 Tool 的限制拉取过多正文。
        top_k = min(max(top_k, 1), 20)
        phrase = terms[0]
        hits: list[SearchHit] = []

        for chunk in self.document.chunks:
            # 正文和章节标题都参与匹配；casefold 比 lower 更适合通用大小写归一化。
            haystack = chunk.text.casefold()
            title = (chunk.section_title or "").casefold()
            counts = Counter({term: haystack.count(term) + title.count(term) for term in terms})
            matched = [term for term in terms if counts[term] > 0]
            if not matched:
                continue
            phrase_count = counts[phrase]
            individual_terms = terms[1:] or terms

            # 完整短语每次命中记 5 分，拆分词按出现次数记分；所有拆分词均出现
            # 再奖励 3 分，使同时覆盖多个关键词的 Chunk 排在前面。
            score = phrase_count * 5.0 + sum(counts[term] for term in individual_terms)
            if individual_terms and all(counts[term] > 0 for term in individual_terms):
                score += 3.0

            # 摘要围绕最早命中位置截取，减少传给模型的无关正文。
            positions = [haystack.find(term) for term in matched if haystack.find(term) >= 0]
            position = min(positions) if positions else 0
            snippet_start = max(0, position - 80)
            snippet_end = min(len(chunk.text), position + max(len(term) for term in matched) + 140)
            snippet = chunk.text[snippet_start:snippet_end].strip()
            hits.append(
                SearchHit(
                    chunk_id=chunk.chunk_id,
                    section_id=chunk.section_id,
                    section_title=chunk.section_title,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    score=score,
                    matched_terms=matched,
                    snippet=snippet,
                )
            )

        hits.sort(key=lambda item: (-item.score, item.start_line, item.chunk_id))
        return hits[:top_k]

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
