import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, graphApi } from '../api'
import type { NovelInfo } from '../types'
import type { GraphJob, GraphNode, GraphResult, GraphStatus } from '../graphTypes'
import KnowledgeGraphPanel from './KnowledgeGraphPanel.vue'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, api: { sections: vi.fn(), context: vi.fn() }, graphApi: { status: vi.fn(), build: vi.fn(), stop: vi.fn(), search: vi.fn(), neighbors: vi.fn(), paths: vi.fn(), evidence: vi.fn() } }
})
const novel = { novel_id: 'book-1' } as NovelInfo
const node: GraphNode = { id: 'a', name: '甲', type: 'person', aliases: [] }
const span = { chunk_id: 'c1', section_id: 's1', section_title: '第一章', start_char: 0, end_char: 6, start_line: 1, end_line: 1, quote: '甲并非乙的师父。' }
const edge = { id: 'r1', source: 'a', target: 'b', type: 'disciple_of', polarity: 'denied' as const, statement_mode: 'character_claim' as const, evidence_total: 1, evidence_groups: [{ id: 'g1', spans: [span] }] }
const result: GraphResult = { graph_version: 'v1', truncated: false, truncation_reason: null, search_complete: true,
  nodes: [node, { ...node, id: 'b', name: '乙' }], edges: [edge], paths: [] }
const status: GraphStatus = { available: true, active_version: 'v1', error_code: null, configured: true, model_name: 'test', source_characters: 100,
  estimated_extraction_units: 1, job: null, ontology: { entity_types: ['person'], relation_types: [{ type: 'disciple_of', label: '师父是', symmetric: false }] } }
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(graphApi.status).mockResolvedValue(status)
  vi.mocked(api.sections).mockResolvedValue({ items: [], total: 0, offset: 0, limit: 200 })
  vi.mocked(graphApi.search).mockResolvedValue({ ...result, items: [node], offset: 0, limit: 20, total: 1 })
  vi.mocked(graphApi.neighbors).mockResolvedValue(result)
  vi.mocked(api.context).mockResolvedValue([{ chunk_id: 'c1', section_id: 's1', section_title: '第一章', start_line: 1, end_line: 1, text: span.quote }])
})

describe('KnowledgeGraphPanel', () => {
  it('explains timed-out usage and resumes saved batches instead of rebuilding', async () => {
    const job: GraphJob = {
      job_id: 'saved-job', graph_version: 'v1', status: 'failed', phase: 'extracting',
      stop_requested: false, can_resume: true, error_code: 'graph_model_timeout',
      extraction: { completed: 4, total: 417 }, resolution: { completed: 0, total: 0 },
      entity_count: 0, relation_count: 0, elapsed_seconds: 306,
      usage: { request_count: 6, reported_input_tokens: 12721, reported_output_tokens: 9500, unknown_request_count: 2, failed_request_count: 2 },
      events: [{ code: 'graph_extraction_output_split', level: 'warning', created_at: '' }],
    }
    vi.mocked(graphApi.status).mockResolvedValue({ ...status, job, available: false, active_version: null,
      max_output_tokens: 6144, request_timeout_seconds: 180, timeout_retries: 1 })
    vi.mocked(graphApi.build).mockResolvedValue(job)
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('6144 token')
    expect(wrapper.text()).toContain('已完成批次已保存')
    expect(wrapper.text()).toContain('可能已产生消耗')
    expect(wrapper.text()).toContain('模型输出达到上限，已自动缩小片段')
    expect(wrapper.text()).not.toContain('graph_model_timeout')
    const resume = wrapper.findAll('button').find(button => button.text() === '继续构建')!
    await resume.trigger('click')
    await flushPromises()
    expect(graphApi.build).toHaveBeenCalledWith('book-1', 'resume', 'saved-job')
    wrapper.unmount()
  })
  it('shows cost disclosure and explicitly starts construction', async () => {
    vi.mocked(graphApi.status).mockResolvedValue({ ...status, available: false, active_version: null })
    vi.mocked(graphApi.build).mockResolvedValue({ job_id: 'j1' } as GraphJob)
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('产生模型用量')
    expect(graphApi.build).not.toHaveBeenCalled()
    await wrapper.find('.graph-actions .button').trigger('click')
    await flushPromises()
    expect(graphApi.build).toHaveBeenCalledWith('book-1', 'start', undefined)
    wrapper.unmount()
  })
  it('labels denied/reported edges and reads original evidence', async () => {
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    await wrapper.find('.graph-entity-search input').setValue('甲')
    // A filter change triggers search immediately without waiting for debounce.
    await wrapper.findAll('select')[1]!.setValue('person')
    await flushPromises()
    await wrapper.find('.graph-candidates button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('（否认）')
    expect(wrapper.text()).toContain('（人物说法）')
    await wrapper.find('.graph-edge-list button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.graph-evidence').text()).toContain(span.quote)
    await wrapper.find('.graph-evidence .text-button').trigger('click')
    await flushPromises()
    expect(api.context).toHaveBeenCalledWith('book-1', 'c1')
    expect(wrapper.text()).toContain('可能包含筛选范围外')
    wrapper.unmount()
  })
})
