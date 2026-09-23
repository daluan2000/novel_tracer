<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from './api'
import type { ConfigStatus, NovelInfo } from './types'
import AgentWorkspace from './components/AgentWorkspace.vue'
import SearchPanel from './components/SearchPanel.vue'
import StructurePanel from './components/StructurePanel.vue'

type TabId = 'agent' | 'structure' | 'search'

const config = ref<ConfigStatus | null>(null)
const novel = ref<NovelInfo | null>(null)
const activeTab = ref<TabId>('agent')
const uploading = ref(false)
const dragging = ref(false)
const runActive = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

const tabs: Array<{ id: TabId; label: string; index: string }> = [
  { id: 'agent', label: 'Agent 分析', index: '01' },
  { id: 'structure', label: '结构概览', index: '02' },
  { id: 'search', label: '文本检索', index: '03' },
]

onMounted(async () => {
  try {
    config.value = await api.config()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法连接后端服务。'
  }
})

async function upload(file?: File) {
  if (!file || runActive.value) return
  error.value = ''
  if (!file.name.toLowerCase().endsWith('.txt')) {
    error.value = '请选择 TXT 小说文件。'
    return
  }
  uploading.value = true
  try {
    novel.value = await api.uploadNovel(file)
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
        <div class="hero-copy"><p class="eyebrow">READ · TRACE · VERIFY</p><h1>把整部长篇小说，<br /><em>变成可追溯的答案。</em></h1><p>导入 TXT，观察 Agent 如何规划、检索、核验证据，并回到每一处原文。</p></div>
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
        <div><span class="book-glyph">文</span><div><strong>{{ novel.filename }}</strong><small>{{ novel.encoding }} · {{ novel.elapsed_seconds.toFixed(2) }}s 完成解析</small></div></div>
        <dl><div><dt>字符</dt><dd>{{ formatNumber(novel.character_count) }}</dd></div><div><dt>行</dt><dd>{{ formatNumber(novel.line_count) }}</dd></div><div><dt>章节</dt><dd>{{ formatNumber(novel.section_count) }}</dd></div><div><dt>Chunks</dt><dd>{{ formatNumber(novel.chunk_count) }}</dd></div></dl>
      </section>

      <section class="workbench" :class="{ empty: !novel }">
        <nav class="tabs" aria-label="工作台功能">
          <button v-for="tab in tabs" :key="tab.id" :class="{ active: activeTab === tab.id }" :disabled="!novel" @click="activeTab = tab.id"><span>{{ tab.index }}</span>{{ tab.label }}</button>
        </nav>
        <template v-if="novel">
          <AgentWorkspace v-show="activeTab === 'agent'" :novel="novel" :config="config" @running-change="runActive = $event" />
          <StructurePanel v-show="activeTab === 'structure'" :novel="novel" />
          <SearchPanel v-show="activeTab === 'search'" :novel="novel" />
        </template>
        <div v-else class="onboarding"><span>01</span><h2>先导入一部小说</h2><p>结构概览、本地检索与 Agent 调查会在这里展开。</p></div>
      </section>
    </main>
    <footer><span>Novel Lens</span><p>每一个结论，都应该能回到原文。</p><span>Local / v0.1</span></footer>
  </div>
</template>
