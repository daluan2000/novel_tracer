<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'
import type { NovelChunk, NovelInfo, SearchHit, SectionItem } from '../types'
import RetrievalObservability from './RetrievalObservability.vue'

const props = defineProps<{ novel: NovelInfo; retrievalRevision?: number }>()
const query = ref('')
const topK = ref(5)
const results = ref<SearchHit[]>([])
const loading = ref(false)
const error = ref('')
const openChunk = ref<string | null>(null)
const contexts = ref<Record<string, NovelChunk[]>>({})
const retrievalObservability = ref<InstanceType<typeof RetrievalObservability> | null>(null)
const chapters = ref<SectionItem[]>([])
const chaptersLoading = ref(false)
const chaptersError = ref('')
const startChapterValue = ref('')
const endChapterValue = ref('')
const hasSearched = ref(false)
const submittedRange = ref<{ start: string; end: string } | null>(null)
let chapterLoadGeneration = 0

const chapterOptions = computed(() => chapters.value.map((chapter, index) => ({
  id: chapter.chapter_id,
  sectionId: chapter.section_id,
  index,
  label: `章节 ${index + 1} · ${chapter.title || '未命名章节'}`,
})))
const startChapter = computed(() => chapterOptions.value.find((chapter) => chapter.label === startChapterValue.value))
const endChapter = computed(() => chapterOptions.value.find((chapter) => chapter.label === endChapterValue.value))
const rangeError = computed(() => {
  if (startChapterValue.value && !startChapter.value) return '请选择目录中存在的起始章节。'
  if (endChapterValue.value && !endChapter.value) return '请选择目录中存在的结束章节。'
  if (startChapter.value && endChapter.value && startChapter.value.index > endChapter.value.index) {
    return '起始章节不能晚于结束章节。'
  }
  return ''
})
const rangeSummary = computed(() => {
  if (!submittedRange.value) return ''
  if (!submittedRange.value.start && !submittedRange.value.end) return '搜索范围：全书'
  return `搜索范围：${submittedRange.value.start || '全书开头'} — ${submittedRange.value.end || '全书末尾'}`
})

function chapterDisplay(sectionId: string, fallback: string | null) {
  return chapterOptions.value.find((chapter) => chapter.sectionId === sectionId)?.label || fallback || '未命名 Section'
}

watch(() => props.novel.novel_id, async (novelId) => {
  const generation = ++chapterLoadGeneration
  results.value = []
  contexts.value = {}
  openChunk.value = null
  startChapterValue.value = ''
  endChapterValue.value = ''
  hasSearched.value = false
  submittedRange.value = null
  chapters.value = []
  chaptersError.value = ''
  chaptersLoading.value = true
  try {
    const loaded: SectionItem[] = []
    let offset = 0
    while (true) {
      const page = await api.sections(novelId, offset, 200)
      loaded.push(...page.items)
      offset += page.items.length
      if (!page.items.length || offset >= page.total) break
    }
    if (generation === chapterLoadGeneration) chapters.value = loaded
  } catch (cause) {
    if (generation === chapterLoadGeneration) {
      chaptersError.value = cause instanceof Error ? cause.message : '章节目录加载失败。'
    }
  } finally {
    if (generation === chapterLoadGeneration) chaptersLoading.value = false
  }
}, { immediate: true })

async function search() {
  if (!query.value.trim()) {
    error.value = '请输入搜索关键词。'
    return
  }
  if (rangeError.value) {
    error.value = rangeError.value
    return
  }
  loading.value = true
  error.value = ''
  try {
    results.value = await api.search(props.novel.novel_id, query.value.trim(), topK.value, {
      startChapterId: startChapter.value?.id,
      endChapterId: endChapter.value?.id,
    })
    hasSearched.value = true
    submittedRange.value = {
      start: startChapter.value?.label || '',
      end: endChapter.value?.label || '',
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '搜索失败。'
  } finally {
    loading.value = false
    await retrievalObservability.value?.refresh()
  }
}

async function toggleContext(hit: SearchHit) {
  if (openChunk.value === hit.chunk_id) {
    openChunk.value = null
    return
  }
  openChunk.value = hit.chunk_id
  if (!contexts.value[hit.chunk_id]) {
    try {
      contexts.value[hit.chunk_id] = await api.context(props.novel.novel_id, hit.chunk_id)
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '上下文加载失败。'
    }
  }
}
</script>

<template>
  <section class="workspace-panel">
    <div class="section-heading"><div><p class="eyebrow">混合检索</p><h2>查找原文</h2></div><span>关键词与语义联合检索</span></div>
    <RetrievalObservability ref="retrievalObservability" :novel-id="novel.novel_id" :refresh-key="retrievalRevision" />
    <form class="search-form" @submit.prevent="search">
      <div class="search-primary-row">
        <label class="field grow"><span>关键词（多个词用空格分隔）</span><input v-model="query" placeholder="例如：吕树 吕小鱼" /></label>
        <label class="field compact"><span>结果数</span><select v-model="topK"><option v-for="n in [3, 5, 10, 20]" :key="n" :value="n">{{ n }}</option></select></label>
        <button class="button primary" :disabled="loading || chaptersLoading || !!rangeError">{{ loading ? '检索中…' : '开始检索' }}</button>
      </div>
      <div class="chapter-range">
        <label class="field">
          <span>起始章节（可搜索）</span>
          <input v-model="startChapterValue" list="search-start-chapters" :disabled="chaptersLoading" :aria-invalid="!!rangeError" placeholder="全书开头" autocomplete="off" />
          <datalist id="search-start-chapters"><option v-for="chapter in chapterOptions" :key="chapter.id" :value="chapter.label" /></datalist>
        </label>
        <span class="range-separator" aria-hidden="true">至</span>
        <label class="field">
          <span>结束章节（可搜索）</span>
          <input v-model="endChapterValue" list="search-end-chapters" :disabled="chaptersLoading" :aria-invalid="!!rangeError" placeholder="全书末尾" autocomplete="off" />
          <datalist id="search-end-chapters"><option v-for="chapter in chapterOptions" :key="chapter.id" :value="chapter.label" /></datalist>
        </label>
      </div>
      <p v-if="rangeError" class="field-error" role="alert">{{ rangeError }}</p>
      <p v-else-if="chaptersError" class="field-error" role="alert">{{ chaptersError }}</p>
    </form>
    <div v-if="error" class="inline-anomaly level-error" role="alert"><strong>原文检索失败</strong><p>{{ error }}</p></div>
    <div v-if="hasSearched" class="search-summary"><span>{{ rangeSummary }}</span><strong>{{ results.length }} 条结果</strong></div>
    <div v-if="results.length" class="result-list">
      <article v-for="hit in results" :key="hit.chunk_id" class="search-result">
        <div class="result-meta">
          <span>{{ chapterDisplay(hit.section_id, hit.section_title) }}</span>
          <span>Ln {{ hit.start_line }}–{{ hit.end_line }}</span>
          <span>Score {{ hit.score.toFixed(1) }}</span>
        </div>
        <p>{{ hit.snippet }}</p>
        <div class="result-footer">
          <div class="tags"><span v-for="term in hit.matched_terms" :key="term">{{ term }}</span></div>
          <button class="text-button" @click="toggleContext(hit)">{{ openChunk === hit.chunk_id ? '收起上下文' : '查看上下文' }}</button>
        </div>
        <div v-if="openChunk === hit.chunk_id" class="context-block">
          <p v-for="chunk in contexts[hit.chunk_id]" :key="chunk.chunk_id" :class="{ focused: chunk.chunk_id === hit.chunk_id }">
            <span>Ln {{ chunk.start_line }}–{{ chunk.end_line }}</span>{{ chunk.text }}
          </p>
        </div>
      </article>
    </div>
    <div v-else-if="!loading" class="empty-state"><span>⌕</span><p>{{ hasSearched ? '当前章节范围内未找到相关原文。' : '输入人物、地点或事件，查找小说中的相关原文。' }}</p></div>
  </section>
</template>
