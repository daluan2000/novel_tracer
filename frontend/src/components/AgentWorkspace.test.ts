import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { ConfigStatus, NovelInfo } from '../types'
import AgentWorkspace from './AgentWorkspace.vue'

vi.mock('../api', () => ({
  api: {
    context: vi.fn(),
    retrievalStatus: vi.fn().mockResolvedValue({
      status: 'hybrid_ready', active_mode: 'hybrid', passage_count: 20,
      embedding_model: 'embedding-test', error_code: null,
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
        status: 'running', activeNode: 'assessor', visitedNodes: ['planner', 'tools'],
        error: null,
        events: [{
          sequence: 1, timestamp: '2026-09-28T00:00:00Z', type: 'status', status: 'running',
          node: 'assessor', error: null, snapshot: {},
          detail: { label: '结构化输出失败，正在重试', diagnostic_code: 'structured_output_retry', level: 'warning' },
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
      isRunning: ref(false), start: vi.fn(), stop: vi.fn(), runId: ref('run-1'),
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
  structured_output_retries: 2, error: null,
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
    expect(wrapper.text()).not.toContain('证据型解读')
  })
})
