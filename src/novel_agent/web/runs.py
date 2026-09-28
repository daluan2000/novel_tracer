"""Background agent-run lifecycle management for the web API."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from novel_agent.application.service import AgentExecutionResult, execute_agent
from novel_agent.runtime.structured_output import StructuredOutputError
from novel_agent.web.events import (
    NODE_LABELS,
    TERMINAL_STATUSES,
    event_detail,
    public_snapshot,
    utc_now,
)
from novel_agent.web.store import StoredNovel

Executor = Callable[..., AgentExecutionResult]
LOGGER = logging.getLogger(__name__)


def _safe_run_failure(error: BaseException) -> tuple[str, str, bool]:
    """Classify provider/runtime failures without returning sensitive exception text."""

    names: list[str] = []
    current: BaseException | None = error
    for _ in range(4):
        if current is None:
            break
        names.append(type(current).__name__.lower())
        current = current.__cause__ or current.__context__
    joined = " ".join(names)
    if isinstance(error, TimeoutError) or "timeout" in joined:
        return "model_timeout", "模型请求超时，请稍后重试；已保留此前的执行记录。", True
    if any(token in joined for token in ("authentication", "permission", "unauthorized")):
        return "model_authentication_failed", "模型服务鉴权失败，请检查 API Key 配置。", False
    if "ratelimit" in joined or "rate_limit" in joined:
        return "model_rate_limited", "模型服务触发频率限制，请稍后重试。", True
    if any(token in joined for token in ("connection", "network", "connecterror")):
        return "model_connection_failed", "无法连接模型服务，请检查网络与服务地址。", True
    if isinstance(error, (RuntimeError, ValueError)):
        return "runtime_configuration_error", "任务运行配置无效，请检查后端配置。", False
    return "agent_execution_failed", "任务执行失败，请查看服务端日志后重试。", True


@dataclass
class RunRecord:
    run_id: str
    novel_id: str
    question: str
    max_steps: int
    status: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(
        self,
        event_type: str,
        *,
        node: str | None = None,
        detail: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.condition:
            if snapshot is not None:
                self.snapshot = snapshot
            event = {
                "sequence": len(self.events) + 1,
                "timestamp": utc_now(),
                "type": event_type,
                "status": self.status,
                "node": node,
                "detail": detail or {},
                "snapshot": self.snapshot,
                "error": error,
            }
            self.events.append(event)
            self.condition.notify_all()

    def event_at(self, index: int, timeout: float = 15.0) -> dict[str, Any] | None:
        with self.condition:
            if index >= len(self.events) and self.status not in TERMINAL_STATUSES:
                self.condition.wait(timeout=timeout)
            return self.events[index] if index < len(self.events) else None


class RunManager:
    def __init__(
        self,
        *,
        executor: Executor = execute_agent,
        model_factory: Callable[[], Any] | None = None,
    ):
        self.executor = executor
        self.model_factory = model_factory
        self._records: dict[str, RunRecord] = {}
        self._active_run_id: str | None = None
        self._lock = threading.Lock()

    def start(self, novel: StoredNovel, question: str, max_steps: int) -> RunRecord:
        with self._lock:
            if self._active_run_id:
                active = self._records[self._active_run_id]
                if active.status not in TERMINAL_STATUSES:
                    raise RuntimeError("已有 Agent 任务正在运行。")
            run_id = uuid.uuid4().hex[:12]
            record = RunRecord(
                run_id=run_id,
                novel_id=novel.novel_id,
                question=question,
                max_steps=max_steps,
            )
            self._records[run_id] = record
            self._active_run_id = run_id

        record.emit("status", detail={"label": "任务已进入队列"})
        worker = threading.Thread(
            target=self._run,
            args=(record, novel),
            daemon=True,
            name=f"novel-agent-{run_id}",
        )
        worker.start()
        return record

    def _run(self, record: RunRecord, novel: StoredNovel) -> None:
        record.status = "running"
        record.emit("status", detail={"label": "Agent 开始运行"})

        def on_update(node: str, update: dict[str, Any], state: dict[str, Any]) -> None:
            snapshot = public_snapshot(state)
            record.emit(
                "update",
                node=node,
                detail=event_detail(node, update, snapshot),
                snapshot=snapshot,
            )

        def on_diagnostic(diagnostic: dict[str, Any], state: dict[str, Any]) -> None:
            code = diagnostic.get("diagnostic_code")
            source_node = str(diagnostic.get("source_node") or "unknown")
            if code == "structured_output_retry":
                label = (
                    f"{NODE_LABELS.get(source_node, source_node)}结构化输出失败，"
                    f"正在重试 {diagnostic.get('retry_number')}/{diagnostic.get('max_retries')}"
                )
            elif code == "content_json_fallback":
                label = (
                    f"{NODE_LABELS.get(source_node, source_node)}"
                    "已采用校验通过的文本 JSON"
                )
            else:
                label = (
                    f"{NODE_LABELS.get(source_node, source_node)}"
                    "结构化输出重试已耗尽"
                )
            record.emit(
                "status",
                node=source_node,
                detail={
                    "label": label,
                    "level": "warning",
                    "operation": "model_response",
                    **diagnostic,
                },
                snapshot=public_snapshot(state),
            )

        try:
            kwargs: dict[str, Any] = {}
            if self.model_factory is not None:
                kwargs["model"] = self.model_factory()
            result = self.executor(
                novel.corpus,
                record.question,
                max_steps=record.max_steps,
                thread_id=record.run_id,
                trace_path=Path("output/traces") / f"{record.run_id}.jsonl",
                on_update=on_update,
                on_diagnostic=on_diagnostic,
                should_cancel=lambda: record.cancel_requested,
                **kwargs,
            )
            snapshot = public_snapshot(result.state)
            if result.cancelled:
                record.status = "cancelled"
                record.emit(
                    "cancelled",
                    detail={"label": "任务已在节点边界停止"},
                    snapshot=snapshot,
                )
            else:
                record.status = "completed"
                termination = snapshot.get("termination_reason")
                terminal_detail: dict[str, Any] = {"label": "分析完成"}
                if termination == "max_steps_reached":
                    terminal_detail.update(
                        level="warning",
                        code="max_steps_reached",
                        label="已达到最大调查步数，使用现有证据生成结果",
                    )
                elif termination == "repeated_tool_call":
                    terminal_detail.update(
                        level="warning",
                        code="repeated_tool_call",
                        label="检测到重复工具调用，已提前终止调查",
                    )
                record.emit(
                    "complete",
                    detail=terminal_detail,
                    snapshot=snapshot,
                )
        except StructuredOutputError as exc:
            record.status = "failed"
            snapshot = public_snapshot(exc.state or {})
            record.emit(
                "error",
                node=exc.source_node,
                detail={
                    "label": (
                        f"{NODE_LABELS.get(exc.source_node, exc.source_node)}"
                        "结构化输出失败"
                    ),
                    "code": "structured_output_failed",
                    "level": "error",
                    "operation": "model_response",
                    "retryable": True,
                    "schema": exc.schema_name,
                    "attempt": exc.attempts,
                    "max_attempts": exc.attempts,
                    "failure_reason": exc.failure_reason,
                },
                snapshot=snapshot,
                error="模型未按要求返回结构化结果，请重试或更换支持 Function Calling 的模型。",
            )
        except Exception as exc:
            LOGGER.exception("Agent run %s failed", record.run_id)
            record.status = "failed"
            code, message, retryable = _safe_run_failure(exc)
            record.emit(
                "error",
                detail={
                    "label": message,
                    "code": code,
                    "level": "error",
                    "operation": "agent_run",
                    "retryable": retryable,
                },
                error=message,
            )
        finally:
            with self._lock:
                if self._active_run_id == record.run_id:
                    self._active_run_id = None

    def get(self, run_id: str) -> RunRecord:
        try:
            return self._records[run_id]
        except KeyError as exc:
            raise KeyError("任务不存在或服务已重启。") from exc

    def cancel(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        with record.condition:
            if record.status not in TERMINAL_STATUSES:
                record.cancel_requested = True
                record.condition.notify_all()
        return record
