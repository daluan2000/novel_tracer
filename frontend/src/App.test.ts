import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'
import App from './App.vue'

vi.mock('./api', () => ({
  api: {
    config: vi.fn(), novels: vi.fn(), uploadNovel: vi.fn(), sections: vi.fn(),
    search: vi.fn(), context: vi.fn(), retrievalStatus: vi.fn(),
    createRun: vi.fn(), cancelRun: vi.fn(),
  },
}))

describe('App upload workflow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.config).mockResolvedValue({
      ready: true, model_name: 'test-model', default_max_steps: 16,
      structured_output_retries: 2, error: null,
    })
    vi.mocked(api.sections).mockResolvedValue({ items: [], offset: 0, limit: 50, total: 0 })
    vi.mocked(api.novels).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(api.uploadNovel).mockResolvedValue({
      novel_id: 'book-1', filename: 'sample.txt', encoding: 'utf-8',
      character_count: 1000, line_count: 100, section_count: 10, chunk_count: 12,
      elapsed_seconds: 0.2,
      structure: {
        strategy: 'detected_headings', confidence: 0.95, heading_count: 10,
        detectors_used: ['numbered'], rejected_candidate_count: 0,
        fallback_used: false, warnings: [],
      },
    })
  })

  it('keeps workbench tabs disabled until a TXT upload succeeds', async () => {
    const wrapper = mount(App)
    await flushPromises()
    expect(wrapper.findAll('.tabs button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)

    const input = wrapper.find('input[type="file"]')
    const file = new File(['第一章\n正文'], 'sample.txt', { type: 'text/plain' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await flushPromises()

    expect(api.uploadNovel).toHaveBeenCalledWith(file)
    expect(wrapper.text()).toContain('sample.txt')
    expect(wrapper.findAll('.tabs button').every((button) => button.attributes('disabled') === undefined)).toBe(true)
  })

  it('shows a useful error without uploading non-TXT files', async () => {
    const wrapper = mount(App)
    await flushPromises()
    const input = wrapper.find('input[type="file"]')
    const file = new File(['bad'], 'sample.md', { type: 'text/markdown' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')

    expect(wrapper.text()).toContain('请选择 TXT 小说文件')
    expect(api.uploadNovel).not.toHaveBeenCalled()
  })

  it('restores the last selected persisted novel and switches between saved novels', async () => {
    const first = await vi.mocked(api.uploadNovel).getMockImplementation()!(new File([], 'ignored.txt'))
    const second = { ...first, novel_id: 'book-2', filename: 'second.txt' }
    vi.mocked(api.novels).mockResolvedValue({ items: [second, first], total: 2 })
    localStorage.setItem('novel-agent:selected-novel', 'book-1')

    const wrapper = mount(App)
    await flushPromises()

    const selector = wrapper.find('.book-selector select')
    expect((selector.element as HTMLSelectElement).value).toBe('book-1')
    expect(wrapper.find('.book-strip').text()).toContain('sample.txt')

    await selector.setValue('book-2')
    expect(wrapper.find('.book-strip').text()).toContain('second.txt')
    expect(localStorage.getItem('novel-agent:selected-novel')).toBe('book-2')
  })

  it('does not duplicate a saved novel when the backend reuses it', async () => {
    const saved = await vi.mocked(api.uploadNovel).getMockImplementation()!(new File([], 'ignored.txt'))
    vi.mocked(api.novels).mockResolvedValue({ items: [saved], total: 1 })

    const wrapper = mount(App)
    await flushPromises()
    const input = wrapper.find('input[type="file"]')
    const file = new File(['same text'], 'renamed.txt', { type: 'text/plain' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await flushPromises()

    expect(wrapper.findAll('.book-selector option')).toHaveLength(1)
  })
})
