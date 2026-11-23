import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, graphApi } from '../api'
import type { NovelInfo } from '../types'
import type { GraphJob, GraphNode, GraphResult, GraphStatus } from '../graphTypes'
import KnowledgeGraphPanel from './KnowledgeGraphPanel.vue'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, api: { sections: vi.fn(), context: vi.fn() }, graphApi: { status: vi.fn(), build: vi.fn(), stop: vi.fn(), search: vi.fn(), neighbors: vi.fn(), paths: vi.fn(), evidence: vi.fn(), unresolved: vi.fn() } }
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
  vi.mocked(graphApi.unresolved).mockResolvedValue({ ...result, items: [], total: 0, offset: 0, limit: 20 })
  vi.mocked(api.context).mockResolvedValue([{ chunk_id: 'c1', section_id: 's1', section_title: '第一章', start_line: 1, end_line: 1, text: span.quote }])
})

describe('KnowledgeGraphPanel', () => {
  it('loads pending mentions as source records and discards them when changing books', async () => {
    vi.mocked(graphApi.unresolved).mockResolvedValue({ ...result, items: [{ mention_id: 'm1', surface: '宝玉',
      type: 'person', anchor: span, contexts: [{ ...span, quote: '甄家的太太带了他们家的宝玉来了。' }],
      candidates: [{ ...node, name: '贾宝玉' }, { ...node, id: 'z', name: '甄宝玉' }], reason: 'competing_identities' }],
      total: 1, offset: 0, limit: 20 })
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    expect(graphApi.unresolved).not.toHaveBeenCalled()
    await wrapper.find('.graph-unresolved button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.graph-unresolved').text()).toContain('当前范围 1 处待确认提及')
    expect(wrapper.find('.graph-unresolved').text()).toContain('贾宝玉、甄宝玉')
    expect(wrapper.find('.graph-unresolved').text()).toContain('甄家的太太')
    expect(graphApi.neighbors).not.toHaveBeenCalled()
    await wrapper.setProps({ novel: { ...novel, novel_id: 'book-2' } })
    await flushPromises()
    expect(wrapper.find('.graph-unresolved').text()).not.toContain('甄家的太太')
    wrapper.unmount()
  })
  it('shows concurrency, separate wall/model times, and readable rate-limit retries', async () => {
    vi.mocked(graphApi.status).mockResolvedValue({ ...status,
      extraction_concurrency: 4, resolution_concurrency: 4, rate_limit_retries: 3,
      job: {
        job_id: 'j', graph_version: 'v1', status: 'completed', phase: 'publishing', stop_requested: false,
        can_resume: false, error_code: null, extraction: { completed: 8, total: 8 },
        resolution: { completed: 16, total: 16 }, entity_count: 4, relation_count: 2, elapsed_seconds: 10,
        phase_elapsed_seconds: { extracting: 3.2, resolving: 6.5 },
        phase_model_seconds: { extracting: 12.1, resolving: 23.4 },
        quality_stats: { candidates_before: { person: 12, item: 20 }, candidates_after: { person: 12, item: 2 },
          entities_after: { person: 3, item: 1 }, filtered_reasons: { low_frequency_background: 18 },
          minimum_mentions: 3, ambiguous_count: 2, identity_rejected_count: 4, batch_request_count: 5 },
        usage: { request_count: 20, reported_input_tokens: 100, reported_output_tokens: 100,
          unknown_request_count: 0, failed_request_count: 1 },
        events: [{ code: 'graph_model_rate_limit_retry', level: 'warning', created_at: '' }],
      },
    })
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('抽取最多 4 路并发')
    expect(wrapper.text()).toContain('阶段累计耗时：')
    expect(wrapper.text()).toContain('3.2 秒')
    expect(wrapper.text()).toContain('12.1 秒')
    expect(wrapper.text()).toContain('并发时可大于实际耗时')
    expect(wrapper.text()).toContain('正在等待后重试')
    expect(wrapper.text()).not.toContain('graph_model_rate_limit_retry')
    expect(wrapper.text()).toContain('物品：候选记录 20 → 2 · 最终实体 1')
    expect(wrapper.text()).toContain('低频背景实体：过滤 18 项')
    expect(wrapper.text()).toContain('拒绝身份线索 4 条')
    expect(wrapper.text()).toContain('身份不确定 2 次')
    expect(wrapper.text()).toContain('批量消歧请求 5 次')
    wrapper.unmount()
  })
  it('shows all aliases in search candidates and selected entities for both path endpoints', async () => {
    const monkey: GraphNode = { id: 'monkey', name: '孙悟空', type: 'person', aliases: ['悟空', '孙大圣', '齐天大圣'] }
    vi.mocked(graphApi.search).mockResolvedValue({ ...result, items: [monkey], offset: 0, limit: 20, total: 1 })
    vi.mocked(graphApi.paths).mockResolvedValue({ ...result, nodes: [monkey], edges: [], paths: [] })
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    await wrapper.find('.graph-entity-search input').setValue('齐天大圣')
    await wrapper.findAll('select')[1]!.setValue('person')
    await flushPromises()
    expect(wrapper.find('.graph-candidates').text()).toContain('孙悟空')
    expect(wrapper.find('.graph-candidates').text()).toContain('其他名称：悟空、孙大圣、齐天大圣')
    await wrapper.find('.graph-candidates button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.graph-entity-search').text()).toContain('已选：孙悟空')
    expect(wrapper.find('.graph-entity-search').text()).toContain('其他名称：悟空、孙大圣、齐天大圣')
    await wrapper.findAll('select')[0]!.setValue('paths')
    await wrapper.findAll('.graph-entity-search input')[1]!.setValue('孙大圣')
    await wrapper.findAll('select')[3]!.setValue(2)
    await flushPromises()
    const target = wrapper.findAll('.graph-entity-search')[1]!
    expect(target.find('.graph-candidates').text()).toContain('其他名称：悟空、孙大圣、齐天大圣')
    await target.find('.graph-candidates button').trigger('click')
    await flushPromises()
    expect(target.text()).toContain('已选：孙悟空')
    expect(target.text()).toContain('其他名称：悟空、孙大圣、齐天大圣')
    wrapper.unmount()
  })
  it('shows cached candidates and local processing diagnostics without blaming model configuration', async () => {
    vi.mocked(graphApi.status).mockResolvedValue({ ...status, job: {
      job_id: 'j', graph_version: 'v1', status: 'failed', phase: 'extracting', stop_requested: false,
      can_resume: true, error_code: 'graph_extraction_processing_failed', extraction: { completed: 35, total: 429, cached: 1 },
      resolution: { completed: 0, total: 0 }, entity_count: 0, relation_count: 0, elapsed_seconds: 655,
      usage: { request_count: 52, reported_input_tokens: 183023, reported_output_tokens: 98494, unknown_request_count: 0, failed_request_count: 0 },
      events: [{ code: 'graph_processing_failure:extract:25:0:a:a:b:ValueError', level: 'error', created_at: '' },
        { code: 'graph_extraction_capacity_saved', level: 'warning', created_at: '' }],
    } })
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('另有 1 批成功返回的候选已缓存')
    expect(wrapper.text()).toContain('修复后继续构建可复用缓存')
    expect(wrapper.text()).toContain('片段 extract:25:0:a:a:b 处理失败（ValueError）')
    expect(wrapper.text()).toContain('已保存所有通过校验的候选')
    expect(wrapper.text()).not.toContain('请检查模型配置后继续')
    wrapper.unmount()
  })
  it('explains output-limit recovery and filtered candidates in readable diagnostics', async () => {
    vi.mocked(graphApi.status).mockResolvedValue({ ...status, job: {
      job_id: 'j', graph_version: 'v1', status: 'failed', phase: 'extracting', stop_requested: false,
      can_resume: true, error_code: 'graph_model_output_limit', extraction: { completed: 4, total: 419 },
      resolution: { completed: 0, total: 0 }, entity_count: 0, relation_count: 0, elapsed_seconds: 182,
      usage: { request_count: 8, reported_input_tokens: 23277, reported_output_tokens: 27822, unknown_request_count: 0, failed_request_count: 3 },
      events: [{ code: 'graph_model_output_limit', level: 'error', created_at: '' },
        { code: 'rejected_relations:8', level: 'warning', created_at: '' },
        { code: 'graph_extraction_compact_fallback', level: 'warning', created_at: '' }],
    } })
    const wrapper = mount(KnowledgeGraphPanel, { props: { novel }, global: { stubs: { KnowledgeGraphCanvas: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('片段编号精简抽取')
    expect(wrapper.text()).toContain('过滤了 8 个未通过校验的关系')
    expect(wrapper.text()).not.toContain('graph_model_output_limit')
    expect(wrapper.text()).not.toContain('rejected_relations')
    wrapper.unmount()
  })
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
