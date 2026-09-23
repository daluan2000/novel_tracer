import { describe, expect, it } from 'vitest'
import { applyRunEvent, emptyRunView } from './runState'
import type { RunEvent } from './types'

function event(overrides: Partial<RunEvent>): RunEvent {
  return {
    sequence: 1,
    timestamp: '2026-09-23T00:00:00Z',
    type: 'update',
    status: 'running',
    node: 'planner',
    detail: { label: '制定调查计划' },
    snapshot: {
      plan: [], current_task_id: null, evidence: [], hypotheses: [],
      unresolved_questions: [], suggested_queries: [], review: null,
      step_count: 0, max_steps: 16, replan_count: 0,
      termination_reason: null, final_answer: null, limitations: [], metrics: {},
    },
    error: null,
    ...overrides,
  }
}

describe('run event reducer', () => {
  it('tracks active and visited graph nodes', () => {
    const planned = applyRunEvent(emptyRunView(), event({ node: 'planner' }))
    const researching = applyRunEvent(planned, event({ sequence: 2, node: 'researcher' }))
    expect(researching.activeNode).toBe('researcher')
    expect(researching.visitedNodes).toContain('planner')
  })

  it('keeps the last snapshot for status-only events', () => {
    const updated = applyRunEvent(emptyRunView(), event({}))
    const status = applyRunEvent(updated, event({ sequence: 2, type: 'status', node: null, snapshot: {} }))
    expect(status.snapshot?.max_steps).toBe(16)
  })

  it('maps terminal events and errors to UI states', () => {
    const failed = applyRunEvent(emptyRunView(), event({ type: 'error', status: 'failed', error: 'boom' }))
    expect(failed.status).toBe('failed')
    expect(failed.error).toBe('boom')
  })
})
