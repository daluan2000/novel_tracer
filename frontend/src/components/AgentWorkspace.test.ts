import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { ConfigStatus, NovelInfo } from '../types'
import AgentWorkspace from './AgentWorkspace.vue'

const runActions = vi.hoisted(() => ({ retry: vi.fn() }))

vi.mock('../api', () => ({
  api: {
    context: vi.fn(),
    conversations: vi.fn().mockResolvedValue({
      items: [{
        conversation_id: 'conversation-1', novel_id: 'book-1', title: '人物关系如何变化',
        created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T01:00:00Z',
        turn_count: 1, latest_status: 'failed', active_run_id: null,
      }],
      total: 1,
    }),
    conversation: vi.fn().mockResolvedValue({
      version: 1, conversation_id: 'conversation-1', novel_id: 'book-1', title: '人物关系如何变化',
      created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T01:00:00Z',
      turns: [{
        turn_id: 'turn-1', run_id: 'run-1', question: '人物关系如何变化？', max_steps: 10,
        status: 'failed', created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T01:00:00Z',
        completed_at: '2026-09-28T01:00:00Z', events: [], snapshot: {},
        error: '模型返回格式无效。', retryable: true, resumable: true,
      }],
    }),
    createConversation: vi.fn(),
    deleteConversation: vi.fn(),
    createConversationRun: vi.fn(),
    retrievalStatus: vi.fn().mockResolvedValue({
      status: 'hybrid_ready', active_mode: 'hybrid', embedding_enabled: true, passage_count: 20,
      embedding_model: 'embedding-test', error_code: null,
      embedding_progress: { completed: 20, total: 20, percentage: 100 },
      metrics: {
        document_request_count: 2, document_text_count: 20,
        document_input_characters: 8000, query_request_count: 1,
        query_input_characters: 8, query_cache_hit_count: 0,
        index_cache_hit: false, failed_request_count: 0, fallback_count: 0,
        last_request_elapsed_seconds: 0.5,
      },
      events: [],
    }),
  },
}))

vi.mock('../composables/useRun', async () => {
  const { reactive, ref } = await import('vue')
  return {
    useRun: () => ({
      view: reactive({
        status: 'failed', activeNode: 'assessor', visitedNodes: ['planner', 'tools'],
        error: '模型返回格式无效。',
        events: [{
          sequence: 1, timestamp: '2026-09-28T00:00:00Z', type: 'error', status: 'failed',
          node: 'assessor', error: '模型返回格式无效。', snapshot: {},
          detail: { label: '结构化输出失败', diagnostic_code: 'structured_output_retry', level: 'error', resumable: true },
        }],
        snapshot: {
          plan: [], current_task_id: null, evidence: [], hypotheses: [],
          unresolved_questions: [], suggested_queries: [], review: null,
          step_count: 2, max_steps: 10, replan_count: 0, termination_reason: null,
          final_answer: null, limitations: [],
          metrics: {
            step_count: 2, model_call_count: 1, tool_call_count: 1,
            evidence_count: 0, evidence_coverage: 0, structured_retry_count: 1,
            content_fallback_count: 0,
            token_usage: {
              input_tokens: 1234, output_tokens: 56, total_tokens: 1290,
              reasoning_tokens: 12, cached_tokens: 100, elapsed_seconds: 1.25,
              reported_call_count: 1, unknown_call_count: 0,
              by_node: { assessor: { input_tokens: 1234, output_tokens: 56, total_tokens: 1290, elapsed_seconds: 1.25 } },
            },
          },
        },
      }),
      isRunning: ref(false), start: vi.fn(), startConversation: vi.fn(), restore: vi.fn(),
      retry: runActions.retry, stop: vi.fn(), runId: ref('run-1'),
    }),
  }
})

const novel: NovelInfo = {
  novel_id: 'book-1', filename: 'sample.txt', encoding: 'utf-8',
  character_count: 100, line_count: 10, section_count: 1, chunk_count: 1,
  elapsed_seconds: 0.1,
  structure: {
    strategy: 'detected_headings', confidence: 0.9, heading_count: 1,
    detectors_used: ['numbered'], rejected_candidate_count: 0,
    fallback_used: false, warnings: [],
  },
}
const config: ConfigStatus = {
  ready: true, model_name: 'test-model', default_max_steps: 16,
  structured_output_retries: 2, embedding_configured: true,
  embedding_model: 'embedding-test', error: null,
}

describe('AgentWorkspace observability', () => {
  it('shows token details and anomalies before a final answer exists', async () => {
    const wrapper = mount(AgentWorkspace, { props: { novel, config } })
    await flushPromises()

    expect(wrapper.text()).toContain('模型用量')
    expect(wrapper.text()).toContain('1,234')
    expect(wrapper.text()).toContain('推理 Token')
    expect(wrapper.text()).toContain('structured_output_retry')
    expect(wrapper.text()).toContain('语义检索用量与运行状态')
    expect(wrapper.text()).toContain('Embedding 已构建完成')
    expect(wrapper.text()).toContain('100%')
    expect(wrapper.text()).toContain('人物关系如何变化')
    expect(wrapper.text()).toContain('当前会话')
    expect(wrapper.text()).not.toContain('证据型解读')
  })

  it('offers retry from the failed node', async () => {
    const wrapper = mount(AgentWorkspace, { props: { novel, config } })
    await flushPromises()

    const button = wrapper.findAll('button').find((item) => item.text().includes('从 Assessor 重试'))
    expect(button).toBeDefined()
    await button!.trigger('click')
    expect(runActions.retry).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('重新分析')
  })

  it('confirms and deletes the selected conversation', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    vi.mocked(api.deleteConversation).mockResolvedValue({
      conversation_id: 'conversation-1', deleted: true,
    })
    const wrapper = mount(AgentWorkspace, { props: { novel, config } })
    await flushPromises()

    await wrapper.get('.delete-conversation').trigger('click')
    await flushPromises()

    expect(window.confirm).toHaveBeenCalledOnce()
    expect(api.deleteConversation).toHaveBeenCalledWith('conversation-1')
  })
})
