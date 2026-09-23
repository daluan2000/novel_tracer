# Novel Agent Token 优化实施总结

## 优化目标

本次改造以减少模型调用次数、避免重复上下文、限制工具输出体积和建立 token 观测能力为主，不通过降低默认调查预算换取节省。

保留不变的配置：

- `NOVEL_AGENT_MAX_STEPS=16`
- `NOVEL_AGENT_STRUCTURED_RETRIES=2`

## 已实施内容

### 1. 合并 Observer 和 Checker

原工具轮次：

```text
Researcher -> ToolNode -> Observer -> Checker -> Researcher/Writer
```

优化后：

```text
Researcher -> ToolNode -> Assessor -> Researcher/Replanner/Writer
```

Assessor 现在使用单次结构化模型调用完成：

- 抽取当轮原文证据。
- 更新假设和未解问题。
- 判断已完成任务和下一任务。
- 评估证据是否充分。
- 判断是否需要重规划。

引文校验、引文去重、单任务证据上限、任务尝试次数和计划状态修正仍由程序端确定性执行。

结果：常规工具轮次的模型调用从 3 次减少为 2 次。

### 2. 停止重放已处理的工具原文

Researcher 现在只保留原始用户问题，不再将上一轮 AI tool call 和 ToolMessage 原文重新发送给模型。

Researcher 改为接收：

- 当前任务。
- 各任务状态摘要。
- 最近 6 条证据摘要。
- 最多 8 条未解问题。
- 最多 5 条建议查询。
- 最近 3 个工具调用的指纹、参数和成功状态。

另外增加了回归保护：如果 Researcher 当轮没有调用工具，Assessor 不会误用上一轮的工具结果。

### 3. 缩小工具输出

模型使用的工具边界已调整为：

| 工具 | 优化后行为 |
| --- | --- |
| `get_book_structure` | 默认只返回总览，不返回完整章节列表；可选分页，最多 20 条 |
| `search_novel` | 默认 3 条，最多 8 条 |
| `read_context` | 默认只返回目标 Chunk，最多前后各 1 个 Chunk |
| `read_section` | 默认 2 个 Chunk，最多 3 个 Chunk |

Chunk 工具响应只保留：

- `chunk_id`
- `section_id`
- `section_title`
- `start_line`
- `end_line`
- `text`

已删除字符偏移和结构检测标记等模型决策不需要的字段。成功响应不再包装 `ok: true` 和空 `error`；失败响应仍保留结构化 `error`。

搜索摘要的前后文长度也已缩短。

### 4. 限制累计状态与节点输入

已实施的状态上限：

- 未解问题：8 条。
- 建议查询：5 条。
- 假设：8 条，优先保留 active/revised 状态。
- Planner/Replanner 输出任务：最多 3 个。
- Assessor 单轮新增证据：最多 3 条。

各节点改为使用专用状态投影：

- Assessor 对历史证据仅接收 ID、claim、Chunk 和支持/反对状态，当轮工具原文仍完整保留以便逐字抽取引文。
- Replanner 只接收证据摘要和证据缺口。
- Writer 仅接收已验证证据、相关假设、矛盾、缺失信息和终止原因，不再接收完整计划和中间 review。
- 模型用 JSON 改为紧凑序列化。

### 5. 增加简单问题快速路径

增加了不消耗模型 token 的本地问题分类。

对不含“分析、变化、关系、动机、主题、对比、影响”等复杂解读信号的短问题，Planner 节点直接产生一个本地任务，不调用规划模型。

简单题的实际模型路径为：

```text
Researcher -> ToolNode -> Assessor -> Writer
```

复杂问题仍保留 Planner 和 Replanner。分类不确定时，复杂信号会优先使问题进入完整路径。

### 6. 增加节点级 token 观测

所有直接和结构化模型调用现在都会记录：

- `input_tokens`
- `output_tokens`
- `total_tokens`
- `reasoning_tokens`
- `cached_tokens`
- 调用耗时
- usage 已报告/未报告调用次数

指标同时按节点汇总，并输出到：

- Agent 最终状态。
- CLI 运行指标。
- Web 快照及指标面板。
- JSONL Trace 中的独立 `model_usage` 事件。

对不返回 usage 元数据的兼容供应商，调用记入 `unknown_call_count`，不将未知 token 错误当成零。

### 7. Prompt Cache 友好化

Researcher、Assessor、Replanner 和 Writer 的消息现在都将：

1. 稳定 System Prompt 放在最前。
2. 稳定的原始用户问题放在动态状态之前。
3. 不断变化的状态 JSON 放在最后。

这种排列使支持前缀缓存的供应商更容易命中缓存。实际命中数可通过新增的 `cached_tokens` 指标观察。

Thinking mode 仍使用现有 `MODEL_THINKING_MODE` 配置，没有为 Qwen 强制发送未经供应商确认的参数，避免破坏 OpenAI-compatible 接口。是否产生 reasoning token 可在真实运行后直接从指标中确认。

### 8. Web 与项目文档同步

- Web 流程图已将 Observer + Checker 更新为 Assessor。
- 时间线已支持 Assessor 节点和其结构化输出诊断。
- 运行指标面板新增模型调用数和总 token。
- README 中的架构图、工具边界、轨迹示例和状态说明已同步。

## 主要代码变更

| 文件 | 变更 |
| --- | --- |
| `src/novel_agent/graph.py` | Assessor 合并节点、简单题快速路径、上下文投影、Prompt 分层和新路由 |
| `src/novel_agent/models.py` | 新增统一 `AssessmentOutput` |
| `src/novel_agent/prompts.py` | 合并取证/审查 Prompt，将计划任务数收紧为 1–3 |
| `src/novel_agent/tools.py` | 工具限额、精简 DTO、紧凑 JSON 和结构分页 |
| `src/novel_agent/repository.py` | 缩短搜索摘要 |
| `src/novel_agent/tracing.py` | usage 规范化、累计和按节点汇总 |
| `src/novel_agent/structured_output.py` | 对每次结构化调用记录 usage 和耗时 |
| `src/novel_agent/service.py` | 将 usage 写入运行状态与 Trace |
| `src/novel_agent/state.py` | 增加问题模式、模型调用数和 token usage |
| `src/novel_agent/web.py` | Assessor 事件与公开指标 |
| `frontend/src` | 更新节点图、类型和 token 指标展示 |

## 测试与验证

后端：

```text
pytest -q
63 passed
```

新增或更新的回归覆盖：

- Assessor 结构化输出绑定和图路由。
- 完整 Researcher -> ToolNode -> Assessor -> Writer 循环。
- 简单问题本地单任务计划。
- 处理后的 ToolMessage 不再进入 Researcher。
- 无新工具调用时不重用旧工具结果。
- 工具 Chunk 上限和返回字段。
- token usage 字段规范化、节点汇总和未知 usage 降级。
- Web 运行事件和结构化输出失败处理。

前端：

```text
npm test -- --run
5 test files passed, 10 tests passed

npm run build
TypeScript 检查和 Vite 生产构建通过
```

另外已执行 Python `compileall` 和 `git diff --check`，未发现语法或补丁格式问题。

## 未执行的外部验证

本次没有自动使用 `.env` 中的真实 API Key 运行付费模型基准，避免为了验证 token 优化反而额外消耗 token。

下次真实问答完成后，可直接在 CLI 运行指标、Web 指标面板或 JSONL Trace 中查看：

- 实际总 token。
- 各节点 token 占比。
- `qwen3.7-flash` 是否返回 reasoning token。
- 供应商 Prompt Cache 是否产生 cached token。

如需要定量的“优化前/后降幅”，应在同一小说、同一问题、同一模型和同一供应商配置下各运行多次后比较中位数。
