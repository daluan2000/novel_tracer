"""Persistent uploaded-novel storage for the web application."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from novel_agent.application.service import load_corpus
from novel_agent.corpus.repository import NovelCorpus
from novel_agent.runtime.config import EmbeddingConfig

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MANIFEST_VERSION = 1
SOURCE_FILENAME = "source.txt"
MANIFEST_FILENAME = "manifest.json"
INDEX_BUILD_CONCURRENCY = 2

logger = logging.getLogger(__name__)


@dataclass
class StoredNovel:
    novel_id: str
    filename: str
    path: Path
    corpus: NovelCorpus
    elapsed_seconds: float
    source_hash: str
    created_at: str


class NovelStore:
    def __init__(
        self,
        upload_limit: int = MAX_UPLOAD_BYTES,
        data_root: str | Path = "output",
        index_cache_root: str | Path | None = None,
    ):
        self.upload_limit = upload_limit
        self.data_root = Path(data_root).expanduser()
        self.root = self.data_root / "novels"
        self.index_cache_root = (
            Path(index_cache_root).expanduser()
            if index_cache_root is not None
            else self.data_root / "indexes"
        )
        self._items: dict[str, StoredNovel] = {}
        self._novel_id_by_hash: dict[str, str] = {}
        self._index_tasks: dict[str, asyncio.Task[None]] = {}
        self._mutation_lock = asyncio.Lock()
        self._index_semaphore = asyncio.Semaphore(INDEX_BUILD_CONCURRENCY)
        self._opened = False

    @staticmethod
    def _source_hash(corpus: NovelCorpus) -> str:
        return hashlib.sha256(corpus.document.source.text.encode("utf-8")).hexdigest()

    @staticmethod
    def _embedding_config() -> EmbeddingConfig:
        try:
            return EmbeddingConfig.from_env()
        except (RuntimeError, ValueError):
            logger.error(
                "Embedding configuration is invalid; using lexical retrieval only."
            )
            return EmbeddingConfig(model_name="", api_key="")

    @staticmethod
    def _validate_manifest(directory: Path, manifest: Any) -> dict[str, Any]:
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        required = {
            "version": int,
            "novel_id": str,
            "filename": str,
            "source_hash": str,
            "encoding": str,
            "created_at": str,
        }
        for field, expected_type in required.items():
            if not isinstance(manifest.get(field), expected_type):
                raise ValueError(f"manifest field {field!r} is invalid")
        if manifest["version"] != MANIFEST_VERSION:
            raise ValueError("unsupported manifest version")
        if manifest["novel_id"] != directory.name:
            raise ValueError("manifest novel_id does not match its directory")
        if (
            not manifest["filename"]
            or Path(manifest["filename"]).name != manifest["filename"]
        ):
            raise ValueError("manifest filename is invalid")
        if len(manifest["source_hash"]) != 64:
            raise ValueError("manifest source_hash is invalid")
        try:
            int(manifest["source_hash"], 16)
        except ValueError as exc:
            raise ValueError("manifest source_hash is invalid") from exc
        datetime.fromisoformat(manifest["created_at"])
        return manifest

    @staticmethod
    def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
        temporary_path = directory / f"{MANIFEST_FILENAME}.tmp"
        manifest_path = directory / MANIFEST_FILENAME
        temporary_path.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.replace(manifest_path)

    @staticmethod
    def _load_persisted(directory: Path) -> StoredNovel:
        manifest_path = directory / MANIFEST_FILENAME
        source_path = directory / SOURCE_FILENAME
        manifest = NovelStore._validate_manifest(
            directory,
            json.loads(manifest_path.read_text(encoding="utf-8")),
        )
        if not source_path.is_file():
            raise ValueError("persisted source file is missing")
        loaded = load_corpus(source_path)
        source_hash = NovelStore._source_hash(loaded.corpus)
        if source_hash != manifest["source_hash"]:
            raise ValueError("persisted source hash does not match its manifest")
        if loaded.corpus.document.source.encoding != manifest["encoding"]:
            raise ValueError("persisted source encoding does not match its manifest")
        return StoredNovel(
            novel_id=manifest["novel_id"],
            filename=manifest["filename"],
            path=source_path,
            corpus=loaded.corpus,
            elapsed_seconds=loaded.elapsed_seconds,
            source_hash=source_hash,
            created_at=manifest["created_at"],
        )

    def _register(self, item: StoredNovel) -> bool:
        if item.novel_id in self._items or item.source_hash in self._novel_id_by_hash:
            return False
        self._items[item.novel_id] = item
        self._novel_id_by_hash[item.source_hash] = item.novel_id
        return True

    def _schedule_dense_index(
        self,
        item: StoredNovel,
        config: EmbeddingConfig,
    ) -> None:
        if not item.corpus.prepare_dense_index(config):
            return
        if item.novel_id in self._index_tasks:
            return
        task = asyncio.create_task(self._build_dense_index(item, config))
        self._index_tasks[item.novel_id] = task

        def discard(completed: asyncio.Task[None], novel_id: str = item.novel_id) -> None:
            self._index_tasks.pop(novel_id, None)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error("Unexpected dense-index task failure for novel %s", novel_id)

        task.add_done_callback(discard)

    async def open(self) -> None:
        """Restore persisted novels and start validation/loading of dense indexes."""

        if self._opened:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_cache_root.mkdir(parents=True, exist_ok=True)
        restored: list[StoredNovel] = []
        for directory in self.root.iterdir():
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or directory.name.startswith(".")
            ):
                continue
            try:
                restored.append(await asyncio.to_thread(self._load_persisted, directory))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Skipping invalid persisted novel at %s: %s",
                    directory,
                    exc,
                )

        config = self._embedding_config()
        for item in sorted(restored, key=lambda candidate: candidate.created_at):
            if not self._register(item):
                logger.warning(
                    "Skipping duplicate persisted novel %s (%s)",
                    item.novel_id,
                    item.filename,
                )
                continue
            self._schedule_dense_index(item, config)
        self._opened = True

    async def close(self) -> None:
        tasks = list(self._index_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._index_tasks.clear()
        self._items.clear()
        self._novel_id_by_hash.clear()
        self._opened = False

    async def _build_dense_index(
        self,
        item: StoredNovel,
        config: EmbeddingConfig,
    ) -> None:
        async with self._index_semaphore:
            await asyncio.to_thread(
                item.corpus.build_dense_index,
                config,
                self.index_cache_root,
            )

    async def add(self, upload: UploadFile) -> StoredNovel:
        filename = Path(upload.filename or "").name
        if Path(filename).suffix.lower() != ".txt":
            raise ValueError("仅支持 .txt 小说文件。")

        self.root.mkdir(parents=True, exist_ok=True)
        staging_root = self.data_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_path = staging_root / f"{uuid.uuid4().hex}.txt"
        total = 0
        try:
            with staging_path.open("xb") as target:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.upload_limit:
                        raise ValueError("文件不能超过 50 MiB。")
                    target.write(chunk)
            if total == 0:
                raise ValueError("上传文件不能为空。")
            loaded = await asyncio.to_thread(load_corpus, staging_path)
            source_hash = self._source_hash(loaded.corpus)

            async with self._mutation_lock:
                existing_id = self._novel_id_by_hash.get(source_hash)
                if existing_id is not None:
                    return self._items[existing_id]

                novel_id = uuid.uuid4().hex[:12]
                while novel_id in self._items or (self.root / novel_id).exists():
                    novel_id = uuid.uuid4().hex[:12]
                created_at = datetime.now(timezone.utc).isoformat()
                pending_directory = self.root / f".{novel_id}.pending"
                final_directory = self.root / novel_id
                pending_directory.mkdir()
                try:
                    persisted_source = pending_directory / SOURCE_FILENAME
                    staging_path.replace(persisted_source)
                    self._write_manifest(
                        pending_directory,
                        {
                            "version": MANIFEST_VERSION,
                            "novel_id": novel_id,
                            "filename": filename,
                            "source_hash": source_hash,
                            "encoding": loaded.corpus.document.source.encoding,
                            "created_at": created_at,
                        },
                    )
                    pending_directory.replace(final_directory)
                except Exception:
                    shutil.rmtree(pending_directory, ignore_errors=True)
                    raise

                final_source = final_directory / SOURCE_FILENAME
                loaded.corpus.document.source.source_path = str(final_source.resolve())
                item = StoredNovel(
                    novel_id=novel_id,
                    filename=filename,
                    path=final_source,
                    corpus=loaded.corpus,
                    elapsed_seconds=loaded.elapsed_seconds,
                    source_hash=source_hash,
                    created_at=created_at,
                )
                self._register(item)

            self._schedule_dense_index(item, self._embedding_config())
            return item
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise
        finally:
            staging_path.unlink(missing_ok=True)
            await upload.close()

    def list(self) -> list[StoredNovel]:
        return sorted(
            self._items.values(),
            key=lambda item: (item.created_at, item.novel_id),
            reverse=True,
        )

    def get(self, novel_id: str) -> StoredNovel:
        try:
            return self._items[novel_id]
        except KeyError as exc:
            raise KeyError("小说不存在，请重新选择或上传。") from exc
