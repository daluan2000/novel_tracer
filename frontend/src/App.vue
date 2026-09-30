<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import type { ConfigStatus, NovelInfo, RetrievalStatus } from './types'
import AgentWorkspace from './components/AgentWorkspace.vue'
import SearchPanel from './components/SearchPanel.vue'
import StructurePanel from './components/StructurePanel.vue'

type TabId = 'agent' | 'structure' | 'search'
const SELECTED_NOVEL_KEY = 'novel-agent:selected-novel'

const config = ref<ConfigStatus | null>(null)
const novel = ref<NovelInfo | null>(null)
const novels = ref<NovelInfo[]>([])
const activeTab = ref<TabId>('agent')
const uploading = ref(false)
const dragging = ref(false)
const runActive = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const retrievalStatus = ref<RetrievalStatus | null>(null)
const embeddingUpdating = ref(false)
const retrievalRevision = ref(0)
let retrievalTimer: ReturnType<typeof setTimeout> | null = null

const tabs: Array<{ id: TabId; label: string; index: string }> = [
  { id: 'agent', label: '智能分析', index: '01' },
  { id: 'structure', label: '结构概览', index: '02' },
  { id: 'search', label: '文本检索', index: '03' },
]

onMounted(async () => {
  try {
    const [loadedConfig, loadedNovels] = await Promise.all([api.config(), api.novels()])
    config.value = loadedConfig
    novels.value = loadedNovels.items
    const savedId = localStorage.getItem(SELECTED_NOVEL_KEY)
    const restored = novels.value.find((item) => item.novel_id === savedId) ?? novels.value[0]
    if (restored) selectNovel(restored)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法连接后端服务。'
  }
})

function selectNovel(selected: NovelInfo) {
  novel.value = selected
  localStorage.setItem(SELECTED_NOVEL_KEY, selected.novel_id)
  retrievalStatus.value = null
  void refreshRetrievalStatus(selected.novel_id)
}

function selectNovelById(novelId: string) {
  const selected = novels.value.find((item) => item.novel_id === novelId)
  if (selected) selectNovel(selected)
}

async function upload(file?: File) {
  if (!file || runActive.value) return
  error.value = ''
  if (!file.name.toLowerCase().endsWith('.txt')) {
    error.value = '请选择 TXT 小说文件。'
    return
  }
  uploading.value = true
  try {
    const uploaded = await api.uploadNovel(file)
    novels.value = [uploaded, ...novels.value.filter((item) => item.novel_id !== uploaded.novel_id)]
    selectNovel(uploaded)
    activeTab.value = 'agent'
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '小说上传失败。'
  } finally {
    uploading.value = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

function handleDrop(event: DragEvent) {
  dragging.value = false
  upload(event.dataTransfer?.files[0])
}

function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value)
}

const embeddingLabel = computed(() => {
  if (!config.value?.embedding_configured) return 'Embedding 未配置'
  if (embeddingUpdating.value) return '正在切换…'
  if (retrievalStatus.value?.status === 'building') {
    return `正在构建索引 ${retrievalStatus.value.embedding_progress.percentage}%`
  }
  if (retrievalStatus.value?.status === 'degraded' && retrievalStatus.value.embedding_enabled) return '启用失败'
  return retrievalStatus.value?.embedding_enabled ? 'Embedding 已构建完成' : 'Embedding 已关闭'
})

function scheduleRetrievalRefresh(novelId: string) {
  if (retrievalTimer) clearTimeout(retrievalTimer)
  retrievalTimer = null
  if (retrievalStatus.value?.status === 'building') {
    retrievalTimer = setTimeout(() => void refreshRetrievalStatus(novelId), 1500)
  }
}

async function refreshRetrievalStatus(novelId: string) {
  try {
    const status = await api.retrievalStatus(novelId)
    if (novel.value?.novel_id !== novelId) return
    retrievalStatus.value = status
    scheduleRetrievalRefresh(novelId)
  } catch (cause) {
    if (novel.value?.novel_id === novelId) {
      error.value = cause instanceof Error ? cause.message : '检索状态读取失败。'
    }
  }
}

async function toggleEmbedding(event: Event) {
  if (!novel.value) return
  const enabled = (event.target as HTMLInputElement).checked
  const novelId = novel.value.novel_id
  embeddingUpdating.value = true
  error.value = ''
  try {
    const status = await api.setEmbeddingEnabled(novelId, enabled)
    if (novel.value?.novel_id === novelId) {
      retrievalStatus.value = status
      retrievalRevision.value += 1
      scheduleRetrievalRefresh(novelId)
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Embedding 设置更新失败。'
    await refreshRetrievalStatus(novelId)
  } finally {
    embeddingUpdating.value = false
  }
}

onBeforeUnmount(() => { if (retrievalTimer) clearTimeout(retrievalTimer) })
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="#" aria-label="Novel Lens 首页"><span class="brand-mark">NL</span><span><strong>Novel Lens</strong><small>证据型小说解读工作台</small></span></a>
      <div class="topbar-status">
        <span class="model-badge" :class="{ ready: config?.ready }"><i></i>{{ config?.ready ? config.model_name : '模型未就绪' }}</span>
        <span class="privacy-note">本机运行 · 文件不离开设备</span>
      </div>
    </header>

    <main>
      <section class="hero">
        <div class="hero-copy"><p class="eyebrow">阅读 · 追溯 · 核验</p><h1>把整部长篇小说，<br /><em>变成可追溯的答案。</em></h1><p>导入 TXT，查看系统如何规划问题、检索原文、核验证据，并让每个结论都能回到出处。</p></div>
        <label class="upload-zone" :class="{ dragging, disabled: runActive }" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="handleDrop">
          <input ref="fileInput" type="file" accept=".txt,text/plain" :disabled="uploading || runActive" @change="upload(($event.target as HTMLInputElement).files?.[0])" />
          <span class="upload-icon" aria-hidden="true">↥</span>
          <span v-if="uploading"><strong>正在解析小说…</strong><small>识别编码、章节与文本块</small></span>
          <span v-else-if="novel"><strong>{{ novel.filename }}</strong><small>{{ runActive ? '任务运行时不可替换' : '点击或拖入另一份 TXT' }}</small></span>
          <span v-else><strong>拖入小说 TXT</strong><small>或点击选择文件 · 最大 50 MiB</small></span>
        </label>
      </section>

      <p v-if="error" class="global-error" role="alert">{{ error }}</p>

      <section v-if="novel" class="book-strip">
        <div><span class="book-glyph">文</span><div><strong>{{ novel.filename }}</strong><small>{{ novel.encoding }} · {{ novel.elapsed_seconds.toFixed(2) }} 秒完成解析</small></div></div>
        <label class="book-selector">
          <span>已保存小说</span>
          <select :value="novel.novel_id" :disabled="runActive || uploading || embeddingUpdating" @change="selectNovelById(($event.target as HTMLSelectElement).value)">
            <option v-for="item in novels" :key="item.novel_id" :value="item.novel_id">{{ item.filename }}</option>
          </select>
        </label>
        <label class="embedding-toggle" :class="{ unavailable: !config?.embedding_configured }">
          <input
            type="checkbox"
            :checked="retrievalStatus?.embedding_enabled ?? false"
            :disabled="!config?.embedding_configured || runActive || uploading || embeddingUpdating"
            @change="toggleEmbedding"
          />
          <span class="toggle-track"><i></i></span>
          <span><strong>{{ embeddingLabel }}</strong><small>{{ config?.embedding_model || '请先配置 EMBEDDING_MODEL' }}</small></span>
        </label>
        <dl><div><dt>字符</dt><dd>{{ formatNumber(novel.character_count) }}</dd></div><div><dt>行</dt><dd>{{ formatNumber(novel.line_count) }}</dd></div><div><dt>章节</dt><dd>{{ formatNumber(novel.section_count) }}</dd></div><div><dt>Chunk</dt><dd>{{ formatNumber(novel.chunk_count) }}</dd></div></dl>
      </section>

      <section class="workbench" :class="{ empty: !novel }">
        <nav class="tabs" aria-label="工作台功能">
          <button v-for="tab in tabs" :key="tab.id" :class="{ active: activeTab === tab.id }" :disabled="!novel" @click="activeTab = tab.id"><span>{{ tab.index }}</span>{{ tab.label }}</button>
        </nav>
        <template v-if="novel">
          <AgentWorkspace v-show="activeTab === 'agent'" :novel="novel" :config="config" :retrieval-revision="retrievalRevision" @running-change="runActive = $event" />
          <StructurePanel v-show="activeTab === 'structure'" :novel="novel" />
          <SearchPanel v-show="activeTab === 'search'" :novel="novel" :retrieval-revision="retrievalRevision" />
        </template>
        <div v-else class="onboarding"><span>01</span><h2>先导入一部小说</h2><p>导入后即可查看章节结构、检索原文，并开展基于证据的智能分析。</p></div>
      </section>
    </main>
    <footer><span>Novel Lens</span><p>每一个结论，都应该能回到原文。</p><span>本地版 · v0.1</span></footer>
  </div>
</template>
