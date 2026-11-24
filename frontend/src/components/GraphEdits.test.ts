import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
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
const preview: GraphMergePreview = { graph_version: 'v2', entities: [{ ...first, mention_count: 3 }, { ...second, mention_count: 2 }],
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
  const found = wrapper.findAll('button').find(item => item.text() === text || item.attributes('aria-label') === text)
  if (!found) throw new Error(`Missing button ${text}`)
  return found
}
async function toNameStep(wrapper: VueWrapper) {
  const next = wrapper.findAll('button').find(item => item.text() === '确定名称 →')
  if (next) await next.trigger('click')
}
async function openHistory(wrapper: VueWrapper) {
  await wrapper.setProps({ section: 'history' })
  await flushPromises()
  const revisions = wrapper.findAll('.revision-item')
  if (revisions.length > 1) await revisions[1]!.trigger('click')
}
async function chooseSecond(wrapper: VueWrapper) {
  await wrapper.findAll('input')[0]!.setValue('乙')
  await vi.advanceTimersByTimeAsync(300)
  await flushPromises()
  await wrapper.find('.graph-candidates button').trigger('click')
}
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
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
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers() })

describe('GraphEdits', () => {
  it('preserves the merge draft across task pages and requires name and preview stages before saving', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    expect(button(wrapper, '确定名称 →').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.flow-actions .primary').text()).toBe('确定名称 →')
    await chooseSecond(wrapper)
    await toNameStep(wrapper)
    await wrapper.findAll('input')[1]!.setValue('规范名称')
    await wrapper.setProps({ section: 'history' })
    await flushPromises()
    await wrapper.setProps({ section: 'entities' })
    expect(wrapper.findAll('.graph-merge-selected li')).toHaveLength(2)
    expect((wrapper.findAll('input')[1]!.element as HTMLInputElement).value).toBe('规范名称')
    expect(wrapper.get('.merge-steps [aria-current]').text()).toContain('确定名称')
    expect(graphApi.merge).not.toHaveBeenCalled()
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(wrapper.get('.merge-steps [aria-current]').text()).toContain('预览确认')
    expect(graphApi.merge).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('automatically searches once after typing settles and clears results for an empty query', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    expect(wrapper.findAll('button').some(item => item.text() === '搜索实体')).toBe(false)
    const input = wrapper.findAll('input')[0]!
    await input.setValue('宝')
    await vi.advanceTimersByTimeAsync(150)
    await input.setValue('宝玉')
    await vi.advanceTimersByTimeAsync(299)
    expect(graphApi.search).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(graphApi.search).toHaveBeenCalledTimes(1)
    expect(graphApi.search).toHaveBeenCalledWith('n', 'v2', '宝玉', { entityTypes: ['person'] }, expect.any(AbortSignal))
    expect(wrapper.findAll('.graph-candidates button')).toHaveLength(1)
    await input.setValue('  ')
    expect(wrapper.findAll('.graph-candidates button')).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(300)
    expect(graphApi.search).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('aborts the previous lookup and ignores late results from an older query', async () => {
    let resolve!: (page: Awaited<ReturnType<typeof graphApi.search>>) => void
    vi.mocked(graphApi.search).mockReturnValueOnce(new Promise(done => { resolve = done }))
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    const input = wrapper.findAll('input')[0]!
    await input.setValue('旧名字')
    await vi.advanceTimersByTimeAsync(300)
    const signal = vi.mocked(graphApi.search).mock.calls[0]![4]!
    expect(signal.aborted).toBe(false)
    await input.setValue('新名字')
    expect(signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    const latest = wrapper.find('.graph-candidates').text()
    resolve({ graph_version: 'v2', items: [{ ...second, id: 'old', name: '过时结果' }], total: 1, offset: 0, limit: 20,
      truncated: false, truncation_reason: null, search_complete: true })
    await flushPromises()
    expect(wrapper.find('.graph-candidates').text()).toBe(latest)
    expect(wrapper.text()).not.toContain('过时结果')
    wrapper.unmount()
  })

  it.each(['clear', 'close', 'book', 'version', 'unmount'])('cancels a pending debounced search on %s', async action => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    await wrapper.findAll('input')[0]!.setValue('尚未发送')
    if (action === 'clear') await wrapper.findAll('input')[0]!.setValue('')
    if (action === 'close') await button(wrapper, '取消合并').trigger('click')
    if (action === 'book') await wrapper.setProps({ novelId: 'other' })
    if (action === 'version') await wrapper.setProps({ version: 'v3' })
    if (action === 'unmount') wrapper.unmount()
    await vi.advanceTimersByTimeAsync(300)
    expect(graphApi.search).not.toHaveBeenCalled()
    if (action !== 'unmount') wrapper.unmount()
  })

  it('previews a whole-book merge, accepts an entered name, and saves a version', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    expect(wrapper.text()).toContain('合并作用于全书')
    await chooseSecond(wrapper)
    expect(graphApi.search).toHaveBeenCalledWith('n', 'v2', '乙', { entityTypes: ['person'] }, expect.any(AbortSignal))
    await toNameStep(wrapper)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(graphApi.previewMerge).toHaveBeenCalledWith('n', { version: 'v2', entity_ids: ['a', 'b'], canonical_name: '甲' })
    expect(wrapper.get('.merge-impact').text()).toContain('8 → 6')
    await wrapper.findAll('input')[1]!.setValue('合并后的新名称')
    expect(wrapper.find('.graph-merge-preview').exists()).toBe(false)
    vi.mocked(graphApi.previewMerge).mockResolvedValueOnce({ ...preview, target: { ...preview.target, name: '合并后的新名称' } })
    await toNameStep(wrapper)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(graphApi.previewMerge).toHaveBeenLastCalledWith('n', { version: 'v2', entity_ids: ['a', 'b'], canonical_name: '合并后的新名称' })
    expect(wrapper.get('.merge-target-card h3').text()).toBe('合并后的新名称')
    await button(wrapper, '确认合并并保存版本').trigger('click')
    await flushPromises()
    expect(graphApi.merge).toHaveBeenCalledWith('n', { version: 'v2', entity_ids: ['a', 'b'], canonical_name: '合并后的新名称' })
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(wrapper.text()).toContain('已保存为新版本')
    wrapper.unmount()
  })

  it('adds multiple entities without duplicates, keeps the typed name, and invalidates previews on removal', async () => {
    const third: GraphNode = { id: 'c', name: '丙', type: 'person', aliases: [] }
    vi.mocked(graphApi.search).mockResolvedValueOnce({ graph_version: 'v2', items: [first, second, third], total: 3, offset: 0, limit: 20,
      truncated: false, truncation_reason: null, search_complete: true })
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    await wrapper.findAll('input')[1]!.setValue('新的规范名')
    await wrapper.findAll('input')[0]!.setValue('人物')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect(wrapper.findAll('.graph-candidates button')).toHaveLength(2)
    await wrapper.find('.graph-candidates button').trigger('click')
    await wrapper.find('.graph-candidates button').trigger('click')
    expect(wrapper.findAll('.graph-merge-selected li')).toHaveLength(3)
    expect(wrapper.findAll('.graph-merge-selected li')).toHaveLength(3)
    expect(wrapper.find('select').exists()).toBe(false)
    await toNameStep(wrapper)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(graphApi.previewMerge).toHaveBeenCalledWith('n', { version: 'v2', entity_ids: ['a', 'b', 'c'], canonical_name: '新的规范名' })
    if (wrapper.find('.graph-merge-preview').exists()) { await button(wrapper, '上一步').trigger('click'); await button(wrapper, '上一步').trigger('click') }
    await button(wrapper, '移除 甲').trigger('click')
    expect(wrapper.find('.graph-merge-preview').exists()).toBe(false)
    expect(wrapper.findAll('input')[1]!.element.value).toBe('新的规范名')
    await toNameStep(wrapper)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(graphApi.previewMerge).toHaveBeenLastCalledWith('n', { version: 'v2', entity_ids: ['b', 'c'], canonical_name: '新的规范名' })
    if (wrapper.find('.graph-merge-preview').exists()) { await button(wrapper, '上一步').trigger('click'); await button(wrapper, '上一步').trigger('click') }
    await button(wrapper, '移除 乙').trigger('click')
    expect(button(wrapper, '确定名称 →').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('requires a nonblank final name and discards late search results after changing the selection type', async () => {
    const wrapper = mount(GraphEdits, { props })
    await button(wrapper, '合并实体').trigger('click')
    await chooseSecond(wrapper)
    await wrapper.findAll('input')[1]!.setValue('   ')
    await toNameStep(wrapper)
    expect(button(wrapper, '预览合并').attributes('disabled')).toBeDefined()
    let resolve!: (page: Awaited<ReturnType<typeof graphApi.search>>) => void
    vi.mocked(graphApi.search).mockReturnValueOnce(new Promise(done => { resolve = done }))
    await wrapper.findAll('input')[0]!.setValue('旧结果')
    await vi.advanceTimersByTimeAsync(300)
    if (wrapper.find('.graph-merge-preview').exists()) { await button(wrapper, '上一步').trigger('click'); await button(wrapper, '上一步').trigger('click') }
    await button(wrapper, '移除 甲').trigger('click')
    if (wrapper.find('.graph-merge-preview').exists()) { await button(wrapper, '上一步').trigger('click'); await button(wrapper, '上一步').trigger('click') }
    await button(wrapper, '移除 乙').trigger('click')
    resolve({ graph_version: 'v2', items: [first], total: 1, offset: 0, limit: 20,
      truncated: false, truncation_reason: null, search_complete: true })
    await flushPromises()
    expect(wrapper.findAll('.graph-candidates button')).toHaveLength(0)
    wrapper.unmount()
  })

  it('restores an older build with an explicit confirmation and keeps history visible', async () => {
    const wrapper = mount(GraphEdits, { props })
    await openHistory(wrapper)
    await flushPromises()
    expect(wrapper.findAll('.revision-item')).toHaveLength(2)
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
    await openHistory(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('正文或原文位置映射已变化')
    expect(wrapper.findAll('button').some(item => item.text() === '恢复此版本')).toBe(false)
    wrapper.unmount()
  })

  it('allows history to load when no compatible current graph remains', async () => {
    const wrapper = mount(GraphEdits, { props: { ...props, version: '', root: null } })
    expect(button(wrapper, '合并实体').attributes('disabled')).toBeDefined()
    await openHistory(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('构建图谱')
    wrapper.unmount()
  })

  it('discards an old book history response and clears an old version preview', async () => {
    let resolve!: (page: GraphHistory) => void
    vi.mocked(graphApi.history).mockReturnValueOnce(new Promise(done => { resolve = done }))
    vi.mocked(graphApi.history).mockResolvedValue({ ...history, items: [], total: 0 })
    const wrapper = mount(GraphEdits, { props })
    await openHistory(wrapper)
    await wrapper.setProps({ novelId: 'other', version: 'other-version' })
    resolve(history)
    await flushPromises()
    expect(wrapper.findAll('.revision-item')).toHaveLength(0)
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
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    await wrapper.find('.graph-candidates button').trigger('click')
    await chooseSecond(wrapper)
    await toNameStep(wrapper)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    await button(wrapper, '确认合并并保存版本').trigger('click')
    await flushPromises()
    expect(graphApi.merge).toHaveBeenCalledWith('n', { version: 'v2', entity_ids: ['a', 'b'], canonical_name: '甲', replaces_correction_id: 'rule' })
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
    await toNameStep(wrapper)
    await button(wrapper, '预览合并').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('图谱已更新，请刷新')
    expect(wrapper.emitted('changed')).toHaveLength(1)
    await toNameStep(wrapper)
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
