import type { RunEvent } from './types'

const toolNames: Record<string, string> = {
  get_book_structure: '查看小说结构',
  search_novel: '检索小说原文',
  read_context: '读取原文上下文',
  read_section: '读取章节原文',
  search_graph_entities: '搜索图谱实体',
  get_graph_entity: '查询实体名称',
  get_graph_relations: '查询实体关系',
  find_graph_paths: '查询关联路径',
}

function nodeName(value: string | null) {
  return value ? ({ planner: '问题规划', researcher: '线索调查', tools: '查找原文', assessor: '证据核验', writer: '整理回答', replanner: '调整计划' } as Record<string, string>)[value] || value : '模型调用'
}

export function timelineText(event: RunEvent) {
  const detail = event.detail
  if (detail.diagnostic_code === 'structured_output_retry') {
    const progress = detail.retry_number !== undefined && detail.max_retries !== undefined
      ? `（第 ${detail.retry_number} 次，共 ${detail.max_retries} 次）`
      : ''
    return `${nodeName(event.node)} 返回格式不符合要求，正在重试${progress}`
  }
  if (detail.diagnostic_code === 'content_json_fallback') {
    return `${nodeName(event.node)} 的结果已通过兼容方式解析`
  }
  if (detail.diagnostic_code === 'structured_output_failed') {
    return `${nodeName(event.node)} 多次返回错误格式，已停止重试`
  }
  if (detail.tool_calls?.length) return `准备${detail.tool_calls.map((call) => toolNames[call.name] ?? call.name).join('、')}`
  if (detail.graph_queries?.length) return detail.graph_queries.map(q => `已完成${toolNames[q.tool] || q.tool}：${q.node_count} 个实体、${q.edge_count} 条关系 · 图谱 ${q.graph_version.slice(0, 8)}${q.truncated ? '（结果已截断）' : ''}`).join('；')
  if (detail.tools?.length) return `已完成：${detail.tools.map((name) => toolNames[name] ?? name).join('、')}`
  if (detail.evidence_count !== undefined) return `当前已校验 ${detail.evidence_count} 条证据`
  if (detail.rationale) return detail.rationale
  return detail.label || event.type
}
