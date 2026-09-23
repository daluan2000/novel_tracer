import type { RunEvent } from './types'

export function timelineText(event: RunEvent) {
  const detail = event.detail
  if (detail.diagnostic_code === 'structured_output_retry') {
    return `${detail.label || event.node || '模型节点'}（${detail.retry_number}/${detail.max_retries}）`
  }
  if (detail.diagnostic_code === 'content_json_fallback') {
    return detail.label || '已采用 Schema 校验通过的文本 JSON'
  }
  if (detail.diagnostic_code === 'structured_output_failed') {
    return detail.label || '结构化输出重试已耗尽'
  }
  if (detail.tool_calls?.length) return `准备调用 ${detail.tool_calls.map((call) => call.name).join('、')}`
  if (detail.tools?.length) return `已执行 ${detail.tools.join('、')}`
  if (detail.evidence_count !== undefined) return `当前已校验 ${detail.evidence_count} 条证据`
  if (detail.rationale) return detail.rationale
  return detail.label || event.type
}
