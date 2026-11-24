import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, graphApi } from '../api'
import type { GraphCorrection, GraphHistory, GraphMergePreview, GraphNode } from '../graphTypes'
import GraphEdits from './GraphEdits.vue'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, graphApi: { history: vi.fn(), search: vi.fn(), previewMerge: vi.fn(), merge: vi.fn(), restore: vi.fn(),
    corrections: vi.fn(), skipCorrection: vi.fn() } }
})
const first: GraphNode = { id: 'a', name: '甲', type: 'person', aliases: ['阿甲'] }
const second: GraphNode = { id: 'b', name: '乙', type: 'person', aliases: [] }
const preview: GraphMergePreview = { graph_version: 'v2', source: { ...second, mention_count: 2 },
  target: { ...first, mention_count: 5, mention_count_before: 3 }, relation_count_before: 8, relation_count_after: 6, self_relations_removed: 1 }
const history: GraphHistory = { graph_version: 'v2', offset: 0, limit: 20, total: 2, items: [
  { version: 'v2', parent_version: 'v1', operation: 'merge', created_at: '2026-11-24T01:00:00Z', details: { source: second, target: first },
    restored_from: null, entity_count: 2, relation_count: 6, current: true, can_restore: false, restore_blocked_reason: null },
  { version: 'v1', parent_version: null, operation: 'build', created_at: '2026-11-24T00:00:00Z', details: {},
    restored_from: null, entity_count: 3, relation_count: 8, current: false, can_restore: true, restore_blocked_reason: null },
] }
const rule: GraphCorrection = { id: 'rule', basis_version: 'v1', source_name: '乙', target_name: '甲', status: 'pending', reason: '原身份已拆分' }
const props = { novelId: 'n', version: 'v2', root: first, running: false, pendingCorrections: 1 }
const mutation = { graph_version: 'v3', operation: 'merge' as const, details: {} }

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(item => item.text() === text)
  if (!found) throw new Error(`Missing button ${text}`)
  return found
}
async function chooseSecond(wrapper: VueWrapper) {
  await wrapper.findAll('input')[1]!.setValue('乙')
  await button(wrapper, '搜索第二个实体').trigger('click')
  await flushPromises()
  await wrapper.find('.graph-candidates button').trigger('click')
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(graphApi.history).mockResolvedValue(history)
  vi.mocked(graphApi.search).mockResolvedValue({ graph_version: 'v2', items: [second], total: 1, offset: 0, limit: 20,
    truncated: false, truncation_reason: null, search_complete: true })
  vi.mocked(graphApi.previewMerge).mockResolvedValue(preview)
  vi.mocked(graphApi.merge).mockResolvedValue(mutation)
  vi.mocked(graphApi.restore).mockResolvedValue({ ...mutation, operation: 'restore' })
  vi.mocked(graphApi.corrections).mockResolvedValue({ graph_version: 'v2', items: [rule] })
  vi.mocked(graphApi.skipCorrection).mockResolvedValue({ ...mutation, operation: 'correction' })
})

describe('GraphEdits', () => {
  it('previews a whole-book merge, lets the user choose the name, and saves a version', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    expect(wrapper.text()).toContain('合并作用于全书')
    await chooseSecond(wrapper)
    expect(graphApi.search).toHaveBeenCalledWith('n', 'v2', '乙', { entityTypes: ['person'] })
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(graphApi.previewMerge).toHaveBeenCalledWith('n', { version: 'v2', source_id: 'b', target_id: 'a' })
    expect(wrapper.text()).toContain('全图关系 8 → 6 条')
    await wrapper.find('select').setValue('b')
    expect(wrapper.find('.graph-merge-preview').exists()).toBe(false)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(graphApi.previewMerge).toHaveBeenLastCalledWith('n', { version: 'v2', source_id: 'a', target_id: 'b' })
    await button(wrapper, '确认合并并保存版本').trigger('click')
    await flushPromises()
    expect(graphApi.merge).toHaveBeenCalledWith('n', { version: 'v2', source_id: 'a', target_id: 'b' })
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(wrapper.text()).toContain('已保存为新版本')
    wrapper.unmount()
  })

  it('restores an older build with an explicit confirmation and keeps history visible', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '查看修改历史').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('乙 → 甲')
    await button(wrapper, '恢复此版本').trigger('click')
    expect(graphApi.restore).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('今后重建以此为准')
    await button(wrapper, '确认恢复并保存版本').trigger('click')
    await flushPromises()
    expect(graphApi.restore).toHaveBeenCalledWith('n', 'v2', 'v1')
    expect(wrapper.emitted('changed')).toHaveLength(1)
    wrapper.unmount()
  })

  it('disables writes during builds while allowing history and incompatible versions to be inspected', async () => {
    vi.mocked(graphApi.history).mockResolvedValue({ ...history, items: [history.items[0]!, {
      ...history.items[1]!, can_restore: false, restore_blocked_reason: '正文或原文位置映射已变化，无法恢复此版本',
    }] })
    const wrapper = mount(GraphEdits, { props: { ...props, running: true } })
    expect(button(wrapper, '合并实体').attributes('disabled')).toBeDefined()
    await button(wrapper, '查看修改历史').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('正文或原文位置映射已变化')
    expect(wrapper.findAll('button').some(item => item.text() === '恢复此版本')).toBe(false)
    wrapper.unmount()
  })

  it('allows history to load when no compatible current graph remains', async () => {
    const wrapper = mount(GraphEdits, { props: { ...props, version: '', root: null } })
    expect(button(wrapper, '合并实体').attributes('disabled')).toBeDefined()
    await button(wrapper, '查看修改历史').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('构建图谱')
    wrapper.unmount()
  })

  it('discards an old book history response and clears an old version preview', async () => {
    let resolve!: (page: GraphHistory) => void
    vi.mocked(graphApi.history).mockReturnValue(new Promise(done => { resolve = done }))
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '查看修改历史').trigger('click')
    await wrapper.setProps({ novelId: 'other', version: 'other-version' })
    resolve(history)
    await flushPromises()
    expect(wrapper.find('.graph-history').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('乙 → 甲')
    expect(wrapper.emitted('changed')).toBeUndefined()
    wrapper.unmount()
  })

  it('exposes conflict reasons and replaces the old correction through the merge flow', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '待确认人工修正（1）').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('原身份已拆分')
    await button(wrapper, '重新选择实体').trigger('click')
    vi.mocked(graphApi.search).mockResolvedValueOnce({ graph_version: 'v2', items: [first], total: 1, offset: 0, limit: 20,
      truncated: false, truncation_reason: null, search_complete: true })
    await wrapper.findAll('input')[0]!.setValue('甲')
    await button(wrapper, '搜索第一个实体').trigger('click')
    await flushPromises()
    await wrapper.find('.graph-candidates button').trigger('click')
    await chooseSecond(wrapper)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    await button(wrapper, '确认合并并保存版本').trigger('click')
    await flushPromises()
    expect(graphApi.merge).toHaveBeenCalledWith('n', { version: 'v2', source_id: 'b', target_id: 'a', replaces_correction_id: 'rule' })
    wrapper.unmount()
  })

  it('skips a pending rule for this revision', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '待确认人工修正（1）').trigger('click')
    await flushPromises()
    await button(wrapper, '跳过本次应用').trigger('click')
    await flushPromises()
    expect(graphApi.skipCorrection).toHaveBeenCalledWith('n', 'v2', 'rule')
    expect(wrapper.emitted('changed')).toHaveLength(1)
    wrapper.unmount()
  })

  it('requests a status refresh on version conflicts and ignores completed writes after switching books', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    await chooseSecond(wrapper)
    vi.mocked(graphApi.previewMerge).mockRejectedValueOnce(new ApiError('图谱已更新，请刷新', 409, 'graph_version_changed'))
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('图谱已更新，请刷新')
    expect(wrapper.emitted('changed')).toHaveLength(1)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    let finish!: (value: typeof mutation) => void
    vi.mocked(graphApi.merge).mockReturnValueOnce(new Promise(done => { finish = done }))
    await button(wrapper, '确认合并并保存版本').trigger('click')
    await wrapper.setProps({ novelId: 'other', version: 'other-version' })
    finish(mutation)
    await flushPromises()
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('已保存为新版本')
    wrapper.unmount()
  })
})
