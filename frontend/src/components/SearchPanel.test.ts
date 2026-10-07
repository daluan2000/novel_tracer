import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { NovelInfo } from '../types'
import SearchPanel from './SearchPanel.vue'

vi.mock('../api', () => ({
  api: { search: vi.fn(), context: vi.fn(), retrievalStatus: vi.fn(), sections: vi.fn() },
}))

const novel: NovelInfo = {
  novel_id: 'book-1', filename: 'sample.txt', encoding: 'utf-8',
  character_count: 100, line_count: 10, section_count: 2, chunk_count: 2,
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
    vi.mocked(api.sections).mockResolvedValue({
      items: [
        { chapter_id: 'book-1:section-1', section_id: 'section-1', title: '第一章 起点', detected: true, confidence: 1, start_line: 1, end_line: 5, chunk_count: 1 },
        { chapter_id: 'book-1:section-2', section_id: 'section-2', title: '第二章 尾声', detected: true, confidence: 1, start_line: 6, end_line: 10, chunk_count: 1 },
      ],
      offset: 0, limit: 200, total: 2,
    })
    vi.mocked(api.search).mockResolvedValue([{
      chunk_id: 'chunk-1', section_id: 'section-1', section_title: '第一章',
      start_line: 1, end_line: 5, score: 8, matched_terms: ['人物'], snippet: '人物在这里。',
    }])
    vi.mocked(api.context).mockResolvedValue([{
      chunk_id: 'chunk-1', section_id: 'section-1', section_title: '第一章',
      start_line: 1, end_line: 5, text: '人物在这里出现。',
    }])
    vi.mocked(api.retrievalStatus).mockResolvedValue({
      status: 'degraded', active_mode: 'lexical', embedding_enabled: false, passage_count: 12,
      embedding_model: null, error_code: 'embedding_not_configured',
      embedding_progress: { completed: 0, total: 12, percentage: 0 },
      metrics: {
        document_request_count: 0, document_text_count: 0,
        document_input_characters: 0, query_request_count: 0,
        query_input_characters: 0, query_cache_hit_count: 0,
        index_cache_hit: null, failed_request_count: 0, fallback_count: 1,
        last_request_elapsed_seconds: null,
      },
      events: [{
        timestamp: '2026-09-28T00:00:00Z', level: 'warning',
        code: 'embedding_not_configured', message: '未配置 Embedding，检索已使用 BM25。',
        operation: 'index_build', fallback: 'bm25',
      }],
    })
  })

  it('searches and expands a result context', async () => {
    const wrapper = mount(SearchPanel, { props: { novel } })
    await flushPromises()
    await wrapper.find('input').setValue('人物')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.search).toHaveBeenCalledWith('book-1', '人物', 5, {
      startChapterId: undefined,
      endChapterId: undefined,
    })
    expect(wrapper.text()).toContain('人物在这里。')
    expect(wrapper.text()).toContain('搜索范围：全书')
    expect(wrapper.text()).toContain('章节 1 · 第一章 起点')
    expect(wrapper.text()).toContain('语义检索用量与运行状态')
    expect(wrapper.text()).toContain('embedding_not_configured')

    await wrapper.find('.text-button').trigger('click')
    await flushPromises()
    expect(api.context).toHaveBeenCalledWith('book-1', 'chunk-1')
    expect(wrapper.text()).toContain('人物在这里出现。')
  })

  it('searches within a selected inclusive chapter range', async () => {
    const wrapper = mount(SearchPanel, { props: { novel } })
    await flushPromises()
    const inputs = wrapper.findAll('.chapter-range input')
    await wrapper.find('input').setValue('人物')
    await inputs[0].setValue('章节 1 · 第一章 起点')
    await inputs[1].setValue('章节 2 · 第二章 尾声')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.search).toHaveBeenCalledWith('book-1', '人物', 5, {
      startChapterId: 'book-1:section-1',
      endChapterId: 'book-1:section-2',
    })
    expect(wrapper.text()).toContain('搜索范围：章节 1 · 第一章 起点 — 章节 2 · 第二章 尾声')
  })

  it('blocks reversed ranges and clears the range when the novel changes', async () => {
    const wrapper = mount(SearchPanel, { props: { novel } })
    await flushPromises()
    const inputs = wrapper.findAll('.chapter-range input')
    await inputs[0].setValue('章节 2 · 第二章 尾声')
    await inputs[1].setValue('章节 1 · 第一章 起点')

    expect(wrapper.text()).toContain('起始章节不能晚于结束章节。')
    expect(wrapper.find('.search-primary-row button').attributes('disabled')).toBeDefined()

    await wrapper.setProps({ novel: { ...novel, novel_id: 'book-2' } })
    await flushPromises()
    const changedInputs = wrapper.findAll('.chapter-range input')
    expect((changedInputs[0].element as HTMLInputElement).value).toBe('')
    expect((changedInputs[1].element as HTMLInputElement).value).toBe('')
  })
})
