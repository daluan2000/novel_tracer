"""Temporary uploaded-novel storage for the web application."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from novel_agent.application.service import load_corpus
from novel_agent.corpus.repository import NovelCorpus
from novel_agent.runtime.config import EmbeddingConfig

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@dataclass
class StoredNovel:
    novel_id: str
    filename: str
    path: Path
    corpus: NovelCorpus
    elapsed_seconds: float


class NovelStore:
    def __init__(
        self,
        upload_limit: int = MAX_UPLOAD_BYTES,
        index_cache_root: str | Path = "output/indexes",
    ):
        self.upload_limit = upload_limit
        self.index_cache_root = Path(index_cache_root)
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="novel-agent-")
        self.root = Path(self._temporary_directory.name)
        self._items: dict[str, StoredNovel] = {}
        self._index_tasks: dict[str, asyncio.Task[None]] = {}

    async def close(self) -> None:
        tasks = list(self._index_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._index_tasks.clear()
        self._items.clear()
        self._temporary_directory.cleanup()

    async def _build_dense_index(
        self,
        item: StoredNovel,
        config: EmbeddingConfig,
    ) -> None:
        await asyncio.to_thread(
            item.corpus.build_dense_index,
            config,
            self.index_cache_root,
        )

    async def add(self, upload: UploadFile) -> StoredNovel:
        filename = Path(upload.filename or "").name
        if Path(filename).suffix.lower() != ".txt":
            raise ValueError("仅支持 .txt 小说文件。")

        novel_id = uuid.uuid4().hex[:12]
        destination = self.root / f"{novel_id}.txt"
        total = 0
        try:
            with destination.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.upload_limit:
                        raise ValueError("文件不能超过 50 MiB。")
                    target.write(chunk)
            if total == 0:
                raise ValueError("上传文件不能为空。")
            loaded = await asyncio.to_thread(load_corpus, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

        item = StoredNovel(
            novel_id=novel_id,
            filename=filename,
            path=destination,
            corpus=loaded.corpus,
            elapsed_seconds=loaded.elapsed_seconds,
        )
        self._items[novel_id] = item
        embedding_config = EmbeddingConfig.from_env()
        if item.corpus.prepare_dense_index(embedding_config):
            task = asyncio.create_task(self._build_dense_index(item, embedding_config))
            self._index_tasks[novel_id] = task
            task.add_done_callback(lambda _task: self._index_tasks.pop(novel_id, None))
        return item

    def get(self, novel_id: str) -> StoredNovel:
        try:
            return self._items[novel_id]
        except KeyError as exc:
            raise KeyError("小说不存在或服务已重启，请重新上传。") from exc
