# Writer 结构化输出失败复盘

- 记录时间：2026-10-08（Asia/Shanghai）
- 影响节点：`writer`（生成最终解读）
- 错误码：`structured_output_retry`、`structured_output_failed`、`stream_operation_failed`

## 现象

最终 Writer 节点在三次自动尝试后仍报“模型未按要求返回结构化结果”。手动从该节点重试两次也得到相同结果。前置 Researcher 和 Assessor 节点可以正常完成。

## 根因

Writer 原本通过 LangChain `with_structured_output(..., method="function_calling")` 强制模型调用 `FinalAnswer` 工具。当前 Qwen OpenAI-compatible 接口没有稳定遵守 named `tool_choice`：

- 失败 trace 中 Writer 连续 9 次返回普通正文，全部被记录为 `missing_tool_call`。
- 每次都有约 418–458 个输出 token，说明模型已经生成答案，不是空响应、超时或上下文超限。
- Writer 提示词要求直接输出 Markdown，与兼容接口未强制执行的工具调用约束存在竞争。
- 旧的降级解析只接受完整 JSON，因此模型已生成的 Markdown 正文被丢弃。

`stream_operation_failed` 是 Web 事件流转发节点最终异常的上层错误，不是模型流式拼接失败。Writer 实际使用同步 `invoke()`。

## 修复方式

1. Writer 不再绑定 `FinalAnswer` Function Calling schema，改为直接调用基础模型。
2. 将模型返回的 Markdown 文本直接作为 `final_answer`，不再因缺少 `tool_calls` 丢弃有效答案。
3. `limitations` 不再依赖模型的结构化格式，而是从 Assessor 已验证的 `missing_information`、`contradictions` 和终止原因确定性生成。
4. 保留 Planner、Assessor 和 Replanner 的结构化 Function Calling，因为这些节点的输出会驱动程序分支，仍需要严格类型。
5. Writer 返回空文本时保持明确失败，避免把空答案标记为完成。

## 回归验证

- 验证 Writer 可直接保留 Markdown 正文。
- 验证 `limitations` 的缺失信息、证据矛盾和步数上限提示为确定性输出。
- 验证最终图调用仍会产生 Writer 更新，模型用量统计仍包含 Writer 调用。
