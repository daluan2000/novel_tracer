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

    expect(text).toContain('Assessor 返回格式不符合要求')
    expect(text).toContain('第 1 次，共 2 次')
    expect(text).not.toContain('raw')
  })

  it('labels content JSON fallback', () => {
    expect(timelineText(event({ diagnostic_code: 'content_json_fallback' })))
      .toContain('已通过兼容方式解析')
  })
})
