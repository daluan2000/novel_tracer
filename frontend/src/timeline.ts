import type { RunEvent } from './types'

const toolNames: Record<string, string> = {
  get_book_structure: '查看小说结构',
  search_novel: '检索小说原文',
  read_context: '读取原文上下文',
  read_section: '读取章节原文',
}

function nodeName(value: string | null) {
  return value ? value[0].toUpperCase() + value.slice(1) : '模型调用'
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
  if (detail.tools?.length) return `已完成：${detail.tools.map((name) => toolNames[name] ?? name).join('、')}`
  if (detail.evidence_count !== undefined) return `当前已校验 ${detail.evidence_count} 条证据`
  if (detail.rationale) return detail.rationale
  return detail.label || event.type
}
