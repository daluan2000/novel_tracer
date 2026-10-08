"""Persistent multi-turn conversation storage for the web application."""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from novel_agent.web.events import utc_now

CONVERSATION_VERSION = 1
ACTIVE_STATUSES = {"queued", "running", "stopping"}
TITLE_LENGTH = 32
HISTORY_LIMIT = 10

LOGGER = logging.getLogger(__name__)


def _title_from_question(question: str) -> str:
    normalized = re.sub(r"\s+", " ", question).strip()
    if len(normalized) <= TITLE_LENGTH:
        return normalized
    return normalized[:TITLE_LENGTH].rstrip() + "…"


class ConversationStore:
    """Thread-safe JSON store for conversations and their run snapshots."""

    def __init__(self, data_root: str | Path = "output"):
        self.root = Path(data_root).expanduser() / "conversations"
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate(path: Path, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("conversation must be a JSON object")
        required = {
            "version": int,
            "conversation_id": str,
            "novel_id": str,
            "title": str,
            "created_at": str,
            "updated_at": str,
            "turns": list,
        }
        for key, expected in required.items():
            if not isinstance(value.get(key), expected):
                raise ValueError(f"conversation field {key!r} is invalid")
        if value["version"] != CONVERSATION_VERSION:
            raise ValueError("unsupported conversation version")
        if value["conversation_id"] != path.stem:
            raise ValueError("conversation id does not match filename")
        for turn in value["turns"]:
            if not isinstance(turn, dict):
                raise ValueError("conversation turn is invalid")
            for key in ("turn_id", "run_id", "question", "status", "created_at"):
                if not isinstance(turn.get(key), str):
                    raise ValueError(f"conversation turn field {key!r} is invalid")
            if not isinstance(turn.get("events", []), list):
                raise ValueError("conversation turn events are invalid")
            if not isinstance(turn.get("snapshot", {}), dict):
                raise ValueError("conversation turn snapshot is invalid")
        return value

    def _write(self, item: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{item['conversation_id']}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._items.clear()
            for path in self.root.glob("*.json"):
                if path.is_symlink():
                    continue
                try:
                    item = self._validate(
                        path, json.loads(path.read_text(encoding="utf-8"))
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    LOGGER.warning("Skipping invalid conversation at %s: %s", path, exc)
                    continue
                changed = self._interrupt_active_turns(item)
                self._items[item["conversation_id"]] = item
                if changed:
                    self._write(item)

    @staticmethod
    def _interrupt_active_turns(item: dict[str, Any]) -> bool:
        changed = False
        for turn in item["turns"]:
            if turn["status"] not in ACTIVE_STATUSES:
                continue
            now = utc_now()
            turn["status"] = "failed"
            turn["updated_at"] = now
            turn["completed_at"] = now
            turn["error"] = "运行因服务重启中断，请重新提问。"
            turn["retryable"] = False
            turn["resumable"] = False
            events = turn.setdefault("events", [])
            events.append(
                {
                    "sequence": len(events) + 1,
                    "timestamp": now,
                    "type": "error",
                    "status": "failed",
                    "node": None,
                    "detail": {
                        "label": "运行因服务重启中断",
                        "code": "run_interrupted",
                        "level": "error",
                        "retryable": False,
                        "resumable": False,
                    },
                    "snapshot": turn.get("snapshot", {}),
                    "error": turn["error"],
                }
            )
            item["updated_at"] = now
            changed = True
        return changed

    def create(self, novel_id: str) -> dict[str, Any]:
        now = utc_now()
        item = {
            "version": CONVERSATION_VERSION,
            "conversation_id": uuid.uuid4().hex[:12],
            "novel_id": novel_id,
            "title": "新对话",
            "created_at": now,
            "updated_at": now,
            "turns": [],
        }
        with self._lock:
            self._items[item["conversation_id"]] = item
            self._write(item)
            return copy.deepcopy(item)

    def get(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                return copy.deepcopy(self._items[conversation_id])
            except KeyError as exc:
                raise KeyError("会话不存在。") from exc

    def list_for_novel(self, novel_id: str) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                {
                    "conversation_id": item["conversation_id"],
                    "novel_id": item["novel_id"],
                    "title": item["title"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                    "turn_count": len(item["turns"]),
                    "latest_status": (
                        item["turns"][-1]["status"] if item["turns"] else None
                    ),
                    "active_run_id": next(
                        (
                            turn["run_id"]
                            for turn in reversed(item["turns"])
                            if turn["status"] in ACTIVE_STATUSES
                        ),
                        None,
                    ),
                }
                for item in self._items.values()
                if item["novel_id"] == novel_id
            ]
        return sorted(items, key=lambda value: value["updated_at"], reverse=True)

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            try:
                item = self._items[conversation_id]
            except KeyError as exc:
                raise KeyError("会话不存在。") from exc
            if any(turn["status"] in ACTIVE_STATUSES for turn in item["turns"]):
                raise RuntimeError("运行中的会话不能删除。")
            path = self.root / f"{conversation_id}.json"
            path.unlink(missing_ok=True)
            del self._items[conversation_id]

    def history(self, conversation_id: str) -> list[dict[str, str]]:
        with self._lock:
            try:
                item = self._items[conversation_id]
            except KeyError as exc:
                raise KeyError("会话不存在。") from exc
            completed = [
                {
                    "question": turn["question"],
                    "answer": str(turn.get("snapshot", {}).get("final_answer") or ""),
                }
                for turn in item["turns"]
                if turn["status"] == "completed"
                and turn.get("snapshot", {}).get("final_answer")
            ]
            return copy.deepcopy(completed[-HISTORY_LIMIT:])

    def start_turn(
        self,
        conversation_id: str,
        *,
        run_id: str,
        question: str,
        max_steps: int,
    ) -> str:
        now = utc_now()
        turn_id = uuid.uuid4().hex[:12]
        with self._lock:
            try:
                item = self._items[conversation_id]
            except KeyError as exc:
                raise KeyError("会话不存在。") from exc
            if not item["turns"]:
                item["title"] = _title_from_question(question)
            item["turns"].append(
                {
                    "turn_id": turn_id,
                    "run_id": run_id,
                    "question": question,
                    "max_steps": max_steps,
                    "status": "queued",
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "events": [],
                    "snapshot": {},
                    "error": None,
                    "retryable": False,
                    "resumable": False,
                }
            )
            item["updated_at"] = now
            self._write(item)
        return turn_id

    def record_event(
        self, conversation_id: str, run_id: str, event: dict[str, Any]
    ) -> None:
        with self._lock:
            item = self._items.get(conversation_id)
            if item is None:
                LOGGER.warning("Conversation %s disappeared during run %s", conversation_id, run_id)
                return
            turn = next(
                (candidate for candidate in item["turns"] if candidate["run_id"] == run_id),
                None,
            )
            if turn is None:
                LOGGER.warning("Run %s has no persisted conversation turn", run_id)
                return
            turn["events"].append(copy.deepcopy(event))
            turn["status"] = event["status"]
            turn["updated_at"] = event["timestamp"]
            if event.get("snapshot"):
                turn["snapshot"] = copy.deepcopy(event["snapshot"])
            if event.get("error"):
                turn["error"] = event["error"]
            detail = event.get("detail") or {}
            if detail.get("code") in {
                "manual_retry_requested",
                "manual_retry_resumed",
            }:
                turn["error"] = None
            if "retryable" in detail:
                turn["retryable"] = bool(detail["retryable"])
            if "resumable" in detail:
                turn["resumable"] = bool(detail["resumable"])
            if event["status"] in ACTIVE_STATUSES:
                turn["completed_at"] = None
            elif event["status"] in {"completed", "cancelled", "failed"}:
                turn["completed_at"] = event["timestamp"]
                if event["status"] in {"completed", "cancelled"}:
                    turn["error"] = None
                    turn["retryable"] = False
                    turn["resumable"] = False
            item["updated_at"] = event["timestamp"]
            self._write(item)
