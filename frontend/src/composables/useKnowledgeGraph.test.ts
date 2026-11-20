import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, graphApi } from '../api'
import type { GraphNode, GraphResult, GraphStatus } from '../graphTypes'
import { useKnowledgeGraph } from './useKnowledgeGraph'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, graphApi: { status: vi.fn(), build: vi.fn(), stop: vi.fn(), search: vi.fn(), neighbors: vi.fn(), paths: vi.fn() } }
})
const node: GraphNode = { id: 'a', name: '甲', type: 'person', aliases: [] }
const result: GraphResult = { graph_version: 'v1', truncated: false, truncation_reason: null, search_complete: true, nodes: [node], edges: [], paths: [] }
function state(version = 'v1'): GraphStatus {
  return { available: true, active_version: version, error_code: null, configured: true, model_name: 'test', source_characters: 100,
    estimated_extraction_units: 1, job: null, ontology: { entity_types: ['person'], relation_types: [] } }
}
function setup() {
  let graph!: ReturnType<typeof useKnowledgeGraph>
  const id = ref('book-1')
  const wrapper = mount(defineComponent({ setup() { graph = useKnowledgeGraph(id); return () => h('div') } }))
  return { graph, id, wrapper }
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(graphApi.status).mockResolvedValue(state())
  vi.mocked(graphApi.neighbors).mockResolvedValue(result)
  vi.mocked(graphApi.search).mockResolvedValue({ ...result, items: [node], offset: 0, limit: 20, total: 1 })
})
afterEach(() => vi.useRealTimers())

describe('useKnowledgeGraph', () => {
  it('forwards chapter and type scope, without invoking build implicitly', async () => {
    const { graph, wrapper } = setup()
    await flushPromises()
    graph.root.value = node
    const filters = { startChapterId: 'book-1:s1', endChapterId: 'book-1:s2', entityTypes: ['person'], relationTypes: ['knows'] }
    await graph.query(2, filters)
    expect(graphApi.neighbors).toHaveBeenCalledWith('book-1', 'v1', 'a', 2, filters, expect.any(AbortSignal))
    expect(graph.result.value?.nodes[0]?.name).toBe('甲')
    expect(graphApi.build).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('drops a response from a previous novel', async () => {
    let finish!: (value: GraphResult) => void
    vi.mocked(graphApi.neighbors).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { graph, id, wrapper } = setup()
    await flushPromises()
    graph.root.value = node
    const request = graph.query(1, {})
    id.value = 'book-2'
    await flushPromises()
    finish(result)
    await request
    expect(graph.result.value).toBeNull()
    expect(graph.root.value).toBeNull()
    wrapper.unmount()
  })
  it('invalidates pending queries when filters or search text change', async () => {
    let finish!: (value: GraphResult) => void
    vi.mocked(graphApi.neighbors).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { graph, wrapper } = setup()
    await flushPromises()
    graph.root.value = node
    const request = graph.query(1, {})
    graph.invalidate()
    finish(result)
    await request
    expect(graph.result.value).toBeNull()
    expect(graph.loading.value).toBe(false)
    wrapper.unmount()
  })
  it('clears old entity IDs and reloads status after a version change', async () => {
    const { graph, wrapper } = setup()
    await flushPromises()
    graph.root.value = node
    vi.mocked(graphApi.status).mockResolvedValue(state('v2'))
    vi.mocked(graphApi.neighbors).mockRejectedValueOnce(new ApiError('图谱已更新，请刷新。', 409, 'graph_version_changed'))
    await graph.query(1, {})
    expect(graph.status.value?.active_version).toBe('v2')
    expect(graph.root.value).toBeNull()
    expect(graph.error.value).toContain('图谱已更新')
    wrapper.unmount()
  })
  it('polls active jobs and stops polling after unmount', async () => {
    vi.useFakeTimers()
    vi.mocked(graphApi.status).mockResolvedValue({ ...state(), job: { status: 'building' } as GraphStatus['job'] })
    const { wrapper } = setup()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1500)
    expect(graphApi.status).toHaveBeenCalledTimes(2)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(3000)
    expect(graphApi.status).toHaveBeenCalledTimes(2)
  })
})
