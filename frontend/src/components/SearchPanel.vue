<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import type { NovelChunk, NovelInfo, SearchHit, SectionItem } from '../types'
import RetrievalObservability from './RetrievalObservability.vue'
import InspectorPane from './InspectorPane.vue'
import PanelDrawer from './PanelDrawer.vue'

const props = withDefaults(defineProps<{ novel: NovelInfo; retrievalRevision?: number; active?: boolean }>(), { active: true })
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
const filtersOpen = ref(false)
const statusOpen = ref(false)
const contextBusy = ref(false)
const contextError = ref('')
let contextGeneration = 0, searchGeneration = 0
const activeHit = computed(() => results.value.find(hit => hit.chunk_id === openChunk.value))
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
  return chapterOptions.value.find((chapter) => chapter.sectionId === sectionId)?.label || fallback || '未命名章节'
}

watch(() => props.novel.novel_id, async (novelId) => {
  const generation = ++chapterLoadGeneration
  searchGeneration++; loading.value = false; error.value = ''; statusOpen.value = false; filtersOpen.value = false
  results.value = []
  contexts.value = {}
  openChunk.value = null
  contextGeneration++; contextBusy.value = false; contextError.value = ''
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
      if (generation !== chapterLoadGeneration) return
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
  const generation = ++searchGeneration
  const range = { start: startChapter.value?.label || '', end: endChapter.value?.label || '' }
  loading.value = true
  error.value = ''
  openChunk.value = null; contextGeneration++; contextBusy.value = false
  try {
    const hits = await api.search(props.novel.novel_id, query.value.trim(), topK.value, {
      startChapterId: startChapter.value?.id,
      endChapterId: endChapter.value?.id,
    })
    if (generation !== searchGeneration) return
    results.value = hits
    hasSearched.value = true
    submittedRange.value = range
  } catch (cause) {
    if (generation === searchGeneration) error.value = cause instanceof Error ? cause.message : '搜索失败。'
  } finally {
    if (generation === searchGeneration) { loading.value = false; await retrievalObservability.value?.refresh() }
  }
}

async function toggleContext(hit: SearchHit) {
  if (openChunk.value === hit.chunk_id) { closeContext(); return }
  const generation = ++contextGeneration, novelId = props.novel.novel_id
  openChunk.value = hit.chunk_id; contextError.value = ''; contextBusy.value = false
  if (contexts.value[hit.chunk_id]) return
  contextBusy.value = true
  try {
    const chunks = await api.context(novelId, hit.chunk_id)
    if (generation === contextGeneration) contexts.value[hit.chunk_id] = chunks
  } catch (cause) {
    if (generation === contextGeneration) contextError.value = cause instanceof Error ? cause.message : '上下文加载失败。'
  } finally { if (generation === contextGeneration) contextBusy.value = false }
}
onBeforeUnmount(() => { contextGeneration++; searchGeneration++; chapterLoadGeneration++ })
function closeContext() { openChunk.value = null; contextGeneration++; contextBusy.value = false }
</script>

<template>
  <section class="workspace-panel search-panel">
    <form class="search-form" @submit.prevent="search">
      <div class="search-primary-row">
        <label class="field grow"><span>关键词（多个词用空格分隔）</span><input v-model="query" placeholder="例如：吕树 吕小鱼" /></label>
        <label class="field compact"><span>结果数</span><select v-model="topK"><option v-for="n in [3, 5, 10, 20]" :key="n" :value="n">{{ n }}</option></select></label>
        <button type="button" class="button secondary filter-toggle" :aria-expanded="filtersOpen" @click="filtersOpen = !filtersOpen">章节范围</button>
        <button class="button primary" :disabled="loading || chaptersLoading || !!rangeError">{{ loading ? '检索中…' : '开始检索' }}</button>
      </div>
      <div v-show="filtersOpen" class="chapter-range">
        <label class="field">
          <span>从哪一章开始</span>
          <input v-model="startChapterValue" list="search-start-chapters" :disabled="chaptersLoading" :aria-invalid="!!rangeError" placeholder="全书开头" autocomplete="off" />
          <datalist id="search-start-chapters"><option v-for="chapter in chapterOptions" :key="chapter.id" :value="chapter.label" /></datalist>
        </label>
        <span class="range-separator" aria-hidden="true">至</span>
        <label class="field">
          <span>到哪一章结束</span>
          <input v-model="endChapterValue" list="search-end-chapters" :disabled="chaptersLoading" :aria-invalid="!!rangeError" placeholder="全书末尾" autocomplete="off" />
          <datalist id="search-end-chapters"><option v-for="chapter in chapterOptions" :key="chapter.id" :value="chapter.label" /></datalist>
        </label>
      </div>
      <p v-if="rangeError" class="field-error" role="alert">{{ rangeError }}</p>
      <p v-else-if="chaptersError" class="field-error" role="alert">{{ chaptersError }}</p>
    </form>
    <div class="search-body">
      <div class="search-results-scroll">
    <div v-if="error" class="inline-anomaly level-error" role="alert"><strong>原文检索失败</strong><p>{{ error }}</p></div>
    <div v-if="hasSearched" class="search-summary"><span>{{ rangeSummary }}</span><strong>{{ results.length }} 条结果</strong></div>
    <div v-if="results.length" class="result-list">
      <article v-for="hit in results" :key="hit.chunk_id" class="search-result" :class="{ selected: openChunk === hit.chunk_id }">
        <div class="result-meta">
          <span>{{ chapterDisplay(hit.section_id, hit.section_title) }}</span>
          <span>行 {{ hit.start_line }}–{{ hit.end_line }}</span>
          <span>相关度 {{ hit.score.toFixed(1) }}</span>
        </div>
        <p>{{ hit.snippet }}</p>
        <div class="result-footer">
          <div class="tags"><span v-for="term in hit.matched_terms" :key="term">{{ term }}</span></div>
          <button class="text-button" @click="toggleContext(hit)">{{ openChunk === hit.chunk_id ? '收起上下文' : '查看上下文' }}</button>
        </div>
      </article>
    </div>
    <div v-else-if="!loading" class="empty-state"><span>⌕</span><p>{{ hasSearched ? '当前章节范围内未找到相关原文。' : '输入人物、地点或事件，查找小说中的相关原文。' }}</p></div>
      </div>
      <InspectorPane :open="active && !!openChunk" title="原文阅读" @close="closeContext">
        <template v-if="activeHit"><div class="source-location"><strong>{{ chapterDisplay(activeHit.section_id, activeHit.section_title) }}</strong><span>行 {{ activeHit.start_line }}–{{ activeHit.end_line }}</span></div><p v-if="contextBusy" role="status">正在读取原文…</p><p v-if="contextError" class="field-error" role="alert">{{ contextError }}</p><div class="context-block source-reader"><p v-for="chunk in contexts[activeHit.chunk_id]" :key="chunk.chunk_id" :class="{ focused: chunk.chunk_id === activeHit.chunk_id }"><span>行 {{ chunk.start_line }}–{{ chunk.end_line }}</span>{{ chunk.text }}</p></div></template>
        <div v-else class="inspector-empty"><span>原文就在这里</span><p>选择一条检索结果，在这里阅读相邻段落，核对人物与情节。</p></div>
      </InspectorPane>
    </div>
    <div class="search-bottom-bar"><span>{{ hasSearched ? `${results.length} 条命中` : '支持人物、地点及事件关键词' }}</span><button class="text-button" @click="statusOpen = true">检索状态与用量</button></div>
    <PanelDrawer :open="active && statusOpen" title="检索状态与用量" @close="statusOpen = false"><RetrievalObservability ref="retrievalObservability" :novel-id="novel.novel_id" :refresh-key="retrievalRevision" /></PanelDrawer>
  </section>
</template>
