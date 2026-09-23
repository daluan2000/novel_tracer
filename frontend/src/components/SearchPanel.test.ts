import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { NovelInfo } from '../types'
import SearchPanel from './SearchPanel.vue'

vi.mock('../api', () => ({
  api: { search: vi.fn(), context: vi.fn() },
}))

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

describe('SearchPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.search).mockResolvedValue([{
      chunk_id: 'chunk-1', section_id: 'section-1', section_title: '第一章',
      start_line: 1, end_line: 5, score: 8, matched_terms: ['人物'], snippet: '人物在这里。',
    }])
    vi.mocked(api.context).mockResolvedValue([{
      chunk_id: 'chunk-1', section_id: 'section-1', section_title: '第一章',
      start_line: 1, end_line: 5, text: '人物在这里出现。',
    }])
  })

  it('searches and expands a result context', async () => {
    const wrapper = mount(SearchPanel, { props: { novel } })
    await wrapper.find('input').setValue('人物')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.search).toHaveBeenCalledWith('book-1', '人物', 5)
    expect(wrapper.text()).toContain('人物在这里。')

    await wrapper.find('.text-button').trigger('click')
    await flushPromises()
    expect(api.context).toHaveBeenCalledWith('book-1', 'chunk-1')
    expect(wrapper.text()).toContain('人物在这里出现。')
  })
})
