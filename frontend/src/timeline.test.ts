import { describe, expect, it } from 'vitest'
import { timelineText } from './timeline'
import type { RunEvent } from './types'

function event(detail: RunEvent['detail']): RunEvent {
  return {
    sequence: 1,
    timestamp: '2026-09-23T00:00:00Z',
    type: 'status',
    status: 'running',
    node: 'assessor',
    detail,
    snapshot: {},
    error: null,
  }
}

describe('timeline diagnostics', () => {
  it('formats structured retries without exposing raw responses', () => {
    const text = timelineText(event({
      label: '校验并整理证据结构化输出失败，正在重试 1/2',
      diagnostic_code: 'structured_output_retry',
      retry_number: 1,
      max_retries: 2,
    }))

    expect(text).toContain('证据核验 返回格式不符合要求')
    expect(text).toContain('第 1 次，共 2 次')
    expect(text).not.toContain('raw')
  })

  it('labels content JSON fallback', () => {
    expect(timelineText(event({ diagnostic_code: 'content_json_fallback' })))
      .toContain('已通过兼容方式解析')
  })
  it('shows graph provenance and result limits in tool events', () => {
    const text = timelineText(event({ tools: ['get_graph_relations'], graph_queries: [{ tool: 'get_graph_relations', graph_version: '12345678abcdef', node_count: 3, edge_count: 2, truncated: true }] }))
    expect(text).toContain('查询实体关系')
    expect(text).toContain('图谱 12345678')
    expect(text).toContain('结果已截断')
  })
})
