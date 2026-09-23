import { describe, expect, it } from 'vitest'
import { timelineText } from './timeline'
import type { RunEvent } from './types'

function event(detail: RunEvent['detail']): RunEvent {
  return {
    sequence: 1,
    timestamp: '2026-09-23T00:00:00Z',
    type: 'status',
    status: 'running',
    node: 'observe',
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

    expect(text).toContain('正在重试 1/2')
    expect(text).not.toContain('raw')
  })

  it('labels content JSON fallback', () => {
    expect(timelineText(event({ diagnostic_code: 'content_json_fallback' })))
      .toContain('Schema 校验通过')
  })
})
