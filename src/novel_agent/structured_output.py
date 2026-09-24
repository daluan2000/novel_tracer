from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Sequence, TypeVar

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ValidationError

from novel_agent.tracing import ModelUsageCallback, model_usage_event

ModelT = TypeVar("ModelT", bound=BaseModel)
DiagnosticCallback = Callable[[dict[str, Any]], None]

_JSON_FENCE = re.compile(r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class StructuredResult(Generic[ModelT]):
    value: ModelT
    retry_count: int = 0
    content_fallback_count: int = 0


class StructuredOutputError(RuntimeError):
    def __init__(
        self,
        *,
        source_node: str,
        schema_name: str,
        attempts: int,
        failure_reason: str,
    ):
        self.source_node = source_node
        self.schema_name = schema_name
        self.attempts = attempts
        self.failure_reason = failure_reason
        self.state: dict[str, Any] | None = None
        super().__init__(
            f"节点 {source_node} 连续 {attempts} 次未返回有效的 {schema_name} 结构化结果。"
        )


def _emit(callback: DiagnosticCallback | None, **data: Any) -> None:
    if callback is not None:
        callback(data)


def _raw_text(raw: Any) -> str | None:
    content = getattr(raw, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _content_json(text: str, schema: type[ModelT]) -> ModelT | None:
    match = _JSON_FENCE.fullmatch(text)
    candidate = match.group(1).strip() if match else text.strip()
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(decoded)
    except ValidationError:
        return None


def _validate_parsed(value: Any, schema: type[ModelT]) -> ModelT:
    if isinstance(value, schema):
        return value
    return schema.model_validate(value)


def _failure_reason(raw: Any, parsing_error: Any) -> str:
    if parsing_error is not None:
        return "schema_validation_error"
    if raw is None:
        return "empty_response"
    tool_calls = getattr(raw, "tool_calls", None) or []
    content = getattr(raw, "content", None)
    if not tool_calls and not content:
        return "empty_response"
    if not tool_calls:
        return "missing_tool_call"
    return "invalid_structured_output"


def invoke_structured(
    runnable: Any,
    messages: Sequence[Any],
    schema: type[ModelT],
    *,
    source_node: str,
    retries: int,
    on_diagnostic: DiagnosticCallback | None = None,
    on_model_usage: ModelUsageCallback | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> StructuredResult[ModelT]:
    """调用结构化模型，并在有限次数内处理格式错误。

    网络/供应商异常会立即向上抛出；只有“模型已经返回，但结果不符合 schema”
    才会重试。如果模型把合法 JSON 放在普通文本而非 tool call 中，也允许进行
    一次兼容性解析并记录 fallback 诊断。
    """

    max_attempts = retries + 1
    base_messages = list(messages)
    invocation_messages = base_messages
    last_reason = "invalid_structured_output"

    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        response = runnable.invoke(invocation_messages)
        if on_model_usage is not None:
            on_model_usage(model_usage_event(response, source_node, time.perf_counter() - started))
        raw = None
        parsing_error = None
        parsed = response
        if isinstance(response, dict) and {"raw", "parsed", "parsing_error"}.issubset(response):
            raw = response.get("raw")
            parsed = response.get("parsed")
            parsing_error = response.get("parsing_error")

        # 首选 with_structured_output 已解析出的对象。
        if parsed is not None:
            try:
                value = _validate_parsed(parsed, schema)
                return StructuredResult(value=value, retry_count=attempt - 1)
            except ValidationError:
                parsing_error = parsing_error or ValidationError

        # 某些兼容接口忽略 function_calling，转而在 content 中输出 JSON；这里
        # 作为降级路径接受它，但会发出诊断，便于观察模型行为。
        tool_calls = getattr(raw, "tool_calls", None) or []
        text = _raw_text(raw)
        if not tool_calls and text is not None:
            fallback = _content_json(text, schema)
            if fallback is not None:
                _emit(
                    on_diagnostic,
                    source_node=source_node,
                    schema=schema.__name__,
                    diagnostic_code="content_json_fallback",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry_number=attempt - 1,
                    max_retries=retries,
                    failure_reason="missing_tool_call",
                )
                return StructuredResult(
                    value=fallback,
                    retry_count=attempt - 1,
                    content_fallback_count=1,
                )

        last_reason = _failure_reason(raw, parsing_error)
        if attempt >= max_attempts:
            _emit(
                on_diagnostic,
                source_node=source_node,
                schema=schema.__name__,
                diagnostic_code="structured_output_failed",
                attempt=attempt,
                max_attempts=max_attempts,
                retry_number=retries,
                max_retries=retries,
                failure_reason=last_reason,
            )
            raise StructuredOutputError(
                source_node=source_node,
                schema_name=schema.__name__,
                attempts=max_attempts,
                failure_reason=last_reason,
            )

        _emit(
            on_diagnostic,
            source_node=source_node,
            schema=schema.__name__,
            diagnostic_code="structured_output_retry",
            attempt=attempt,
            max_attempts=max_attempts,
            retry_number=attempt,
            max_retries=retries,
            failure_reason=last_reason,
        )
        # 指数退避后追加一条纠错消息；base_messages 保持不变，避免重复堆叠
        # 多条失败提示。
        sleeper(min(0.5 * (2 ** (attempt - 1)), 4.0))
        invocation_messages = [
            *base_messages,
            HumanMessage(
                content=(
                    f"上一次响应未生成有效的 {schema.__name__}。"
                    f"请只调用要求的 {schema.__name__} 工具并提供完整参数，不要返回普通文本。"
                )
            ),
        ]

    raise AssertionError("structured retry loop exited unexpectedly")
