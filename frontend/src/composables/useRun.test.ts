import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RunEvent } from '../types'
import { useRun } from './useRun'

const apiMock = vi.hoisted(() => ({
  createRun: vi.fn(),
  createConversationRun: vi.fn(),
  retryRun: vi.fn(),
  cancelRun: vi.fn(),
}))

vi.mock('../api', () => ({ api: apiMock }))

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: ((message: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }

  close() {
    this.closed = true
  }

  emit(event: RunEvent) {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent)
  }
}

function event(sequence: number, overrides: Partial<RunEvent>): RunEvent {
  return {
    sequence,
    timestamp: '2026-09-30T00:00:00Z',
    type: 'status',
    status: 'running',
    node: null,
    detail: {},
    snapshot: {},
    error: null,
    ...overrides,
  }
}

describe('useRun manual retry', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    apiMock.createRun.mockReset().mockResolvedValue({ run_id: 'run-1', status: 'queued' })
    apiMock.createConversationRun.mockReset().mockResolvedValue({
      conversation_id: 'conversation-1', turn_id: 'turn-1', run_id: 'run-2', status: 'queued',
    })
    apiMock.retryRun.mockReset().mockResolvedValue({
      run_id: 'run-1', status: 'queued', failed_node: 'assessor', manual_retry_count: 1,
    })
  })

  it('replays history without closing on the previous failure', async () => {
    let run!: ReturnType<typeof useRun>
    const wrapper = mount(defineComponent({
      setup() {
        run = useRun()
        return () => h('div')
      },
    }))
    await run.start('book-1', '问题', 5)
    const first = FakeEventSource.instances[0]
    first.emit(event(1, { status: 'queued' }))
    first.emit(event(2, { type: 'update', node: 'planner' }))
    first.emit(event(3, {
      type: 'error', status: 'failed', node: 'assessor', error: 'boom',
      detail: { resumable: true },
    }))
    expect(first.closed).toBe(true)

    await run.retry()
    const resumed = FakeEventSource.instances[1]
    expect(resumed.url).toContain('/api/runs/run-1/events')
    resumed.emit(event(3, {
      type: 'error', status: 'failed', node: 'assessor', error: 'boom',
      detail: { resumable: true },
    }))
    expect(resumed.closed).toBe(false)
    resumed.emit(event(4, {
      status: 'queued', node: 'assessor',
      detail: { code: 'manual_retry_requested', manual_retry_count: 1 },
    }))
    expect(run.view.status).toBe('queued')
    expect(run.view.error).toBeNull()
    resumed.emit(event(6, { type: 'complete', status: 'completed', node: 'writer' }))
    expect(resumed.closed).toBe(true)
    expect(run.view.status).toBe('completed')
    wrapper.unmount()
  })

  it('restores persisted events and resumes after the last sequence', async () => {
    const terminal = vi.fn()
    let run!: ReturnType<typeof useRun>
    const wrapper = mount(defineComponent({
      setup() {
        run = useRun(terminal)
        return () => h('div')
      },
    }))
    run.restore({
      turn_id: 'turn-1', run_id: 'run-1', question: '追问', max_steps: 5,
      status: 'running', created_at: '2026-09-30T00:00:00Z', updated_at: '2026-09-30T00:00:00Z',
      completed_at: null, error: null, retryable: false, resumable: false, snapshot: {},
      events: [
        event(1, { status: 'queued' }),
        event(2, { type: 'update', status: 'running', node: 'planner' }),
      ],
    })

    expect(run.view.events).toHaveLength(2)
    expect(FakeEventSource.instances[0].url).toContain('after=2')
    FakeEventSource.instances[0].emit(event(3, {
      type: 'complete', status: 'completed', node: 'writer',
    }))
    expect(terminal).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
})
