<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../api'
import type { NovelChunk, NovelInfo, SearchHit } from '../types'

const props = defineProps<{ novel: NovelInfo }>()
const query = ref('')
const topK = ref(5)
const results = ref<SearchHit[]>([])
const loading = ref(false)
const error = ref('')
const openChunk = ref<string | null>(null)
const contexts = ref<Record<string, NovelChunk[]>>({})

watch(() => props.novel.novel_id, () => {
  results.value = []
  contexts.value = {}
  openChunk.value = null
})

async function search() {
  if (!query.value.trim()) {
    error.value = '请输入搜索关键词。'
    return
  }
  loading.value = true
  error.value = ''
  try {
    results.value = await api.search(props.novel.novel_id, query.value.trim(), topK.value)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '搜索失败。'
  } finally {
    loading.value = false
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
    <div class="section-heading"><div><p class="eyebrow">LOCAL RETRIEVAL</p><h2>原文检索</h2></div><span>不调用模型</span></div>
    <form class="search-form" @submit.prevent="search">
      <label class="field grow"><span>关键词（多个词用空格分隔）</span><input v-model="query" placeholder="例如：吕树 吕小鱼" /></label>
      <label class="field compact"><span>结果数</span><select v-model="topK"><option v-for="n in [3, 5, 10, 20]" :key="n" :value="n">{{ n }}</option></select></label>
      <button class="button primary" :disabled="loading">{{ loading ? '检索中…' : '开始检索' }}</button>
    </form>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <div v-if="results.length" class="result-list">
      <article v-for="hit in results" :key="hit.chunk_id" class="search-result">
        <div class="result-meta">
          <span>{{ hit.section_title || '未命名 Section' }}</span>
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
    <div v-else-if="!loading" class="empty-state"><span>⌕</span><p>输入人物、地点或事件词，查看 Agent 实际能够检索到的原文。</p></div>
  </section>
</template>
