<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import type { ConfigStatus, NovelInfo, RetrievalStatus } from './types'
import AgentWorkspace from './components/AgentWorkspace.vue'
import SearchPanel from './components/SearchPanel.vue'
import StructurePanel from './components/StructurePanel.vue'
import AppIcon from './components/AppIcon.vue'
import RetrievalObservability from './components/RetrievalObservability.vue'
import { graphSections, parseRoute, routeHash, type PageId, type GraphSection } from './navigation'
const KnowledgeGraphPanel = defineAsyncComponent(() => import('./components/KnowledgeGraphPanel.vue'))

type TabId = PageId
const SELECTED_NOVEL_KEY = 'novel-agent:selected-novel'

const config = ref<ConfigStatus | null>(null)
const novel = ref<NovelInfo | null>(null)
const novels = ref<NovelInfo[]>([])
const route = ref(parseRoute(window.location.hash))
const activeTab = computed(() => route.value.page)
const graphSection = computed(() => route.value.graphSection)
const graphLabel = computed(() => graphSections.find(item => item.id === graphSection.value)!.label)
let lastGraphSection: GraphSection = graphSection.value
const visited = ref(new Set<TabId>([activeTab.value]))
const libraryQuery = ref('')
const loading = ref(true)
const uploading = ref(false)
const dragging = ref(false)
const runActive = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const retrievalStatus = ref<RetrievalStatus | null>(null)
const embeddingUpdating = ref(false)
const retrievalRevision = ref(0)
let retrievalTimer: ReturnType<typeof setTimeout> | null = null

const tabs: Array<{ id: TabId; label: string; description: string }> = [
  { id: 'library', label: '我的书架', description: '管理小说，选择这一次想读的故事。' },
  { id: 'agent', label: '小说问答', description: '从一个问题开始，带着原文读懂故事。' },
  { id: 'structure', label: '章节目录', description: '浏览全书章节，查看原文位置。' },
  { id: 'search', label: '原文检索', description: '找到人物、地点与事件出现的段落。' },
  { id: 'graph', label: '关系图谱', description: '从人物出发，探索故事里的联系。' },
  { id: 'settings', label: '检索设置', description: '管理语义索引，查看检索状态与用量。' },
]
const currentPage = computed(() => tabs.find(tab => tab.id === activeTab.value)!)
const filteredNovels = computed(() => novels.value.filter(item => item.filename.toLocaleLowerCase().includes(libraryQuery.value.trim().toLocaleLowerCase())))

function navigate(page: TabId, section: GraphSection = lastGraphSection) {
  route.value = { page, graphSection: section }
  if (page === 'graph') lastGraphSection = section
  visited.value.add(page)
  const hash = routeHash(route.value)
  if (window.location.hash !== hash) window.location.hash = hash
}
function syncPage() {
  route.value = parseRoute(window.location.hash)
  if (activeTab.value === 'graph') lastGraphSection = graphSection.value
  visited.value.add(activeTab.value)
  if (window.location.hash === '#/graph') window.history.replaceState(null, '', '#/graph/explore')
}
function coverVariant(id: string) {
  return [...id].reduce((value, character) => (value * 31 + character.charCodeAt(0)) >>> 0, 0) % 4
}
function openNovel(item: NovelInfo) { selectNovel(item); navigate('agent') }

onMounted(async () => {
  window.addEventListener('hashchange', syncPage)
  syncPage()
  try {
    const [loadedConfig, loadedNovels] = await Promise.all([api.config(), api.novels()])
    config.value = loadedConfig
    novels.value = loadedNovels.items
    const savedId = localStorage.getItem(SELECTED_NOVEL_KEY)
    const restored = novels.value.find((item) => item.novel_id === savedId) ?? novels.value[0]
    if (restored) selectNovel(restored)
    else navigate('library')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法连接后端服务。'
  } finally {
    loading.value = false
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
  if (!file || runActive.value || uploading.value || embeddingUpdating.value) return
  error.value = ''
  if (!file.name.toLowerCase().endsWith('.txt')) {
    error.value = '请选择 TXT 小说文件。'
    return
  }
  if (file.size > 50 * 1024 * 1024) {
    error.value = '文件太大了，请选择不超过 50 MiB 的 TXT 文件。'
    return
  }
  uploading.value = true
  try {
    const uploaded = await api.uploadNovel(file)
    novels.value = [uploaded, ...novels.value.filter((item) => item.novel_id !== uploaded.novel_id)]
    selectNovel(uploaded)
    navigate('agent')
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
  if (!config.value?.embedding_configured) return '语义检索未配置'
  if (embeddingUpdating.value) return '正在切换…'
  if (retrievalStatus.value?.status === 'building') {
    return `正在构建索引 ${retrievalStatus.value.embedding_progress.percentage}%`
  }
  if (retrievalStatus.value?.status === 'degraded' && retrievalStatus.value.embedding_enabled) return '语义索引暂不可用'
  return retrievalStatus.value?.embedding_enabled ? '语义索引已就绪' : '语义检索未开启'
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
    error.value = cause instanceof Error ? cause.message : '语义检索设置更新失败。'
    await refreshRetrievalStatus(novelId)
  } finally {
    embeddingUpdating.value = false
  }
}

onBeforeUnmount(() => {
  window.removeEventListener('hashchange', syncPage)
  if (retrievalTimer) clearTimeout(retrievalTimer)
})
</script>


<template>
  <div class="app-shell">
    <aside class="app-sidebar">
      <a class="brand" href="#/library"><span class="brand-mark"><AppIcon name="book" /></span><span><strong>Novel Lens</strong><small>读懂故事的每一面</small></span></a>
      <div v-if="novel" class="book-strip">
        <label class="book-selector"><span>当前小说</span><select :value="novel.novel_id" aria-label="当前小说" :disabled="runActive || uploading || embeddingUpdating" @change="selectNovelById(($event.target as HTMLSelectElement).value)"><option v-for="item in novels" :key="item.novel_id" :value="item.novel_id">{{ item.filename }}</option></select></label>
        <small>{{ formatNumber(novel.character_count) }} 字 · {{ novel.section_count }} 章</small>
      </div>
      <p class="nav-caption">阅读工作台</p>
      <nav class="tabs" aria-label="工作台功能">
        <div v-for="tab in tabs" :key="tab.id" class="nav-item">
          <button :class="{ active: activeTab === tab.id }" :aria-current="activeTab === tab.id ? 'page' : undefined" :disabled="!novel && tab.id !== 'library'" @click="navigate(tab.id)"><AppIcon :name="tab.id" /><span>{{ tab.label }}</span><i v-if="tab.id === 'agent' && runActive" class="running-dot" /></button>
          <nav v-if="tab.id === 'graph' && activeTab === 'graph'" class="graph-subnav" aria-label="图谱子页面"><a v-for="section in graphSections" :key="section.id" :href="`#/graph/${section.id}`" :aria-current="graphSection === section.id ? 'page' : undefined" @click.prevent="navigate('graph', section.id)">{{ section.label }}</a></nav>
        </div>
      </nav>
      <div class="sidebar-bottom"><span class="model-badge" :class="{ ready: config?.ready }"><i />{{ config?.ready ? '模型已配置' : '模型未配置' }}</span><small :title="config?.model_name">{{ config?.model_name || '请检查模型配置' }}</small><p>小说保存在本地<br />分析时按需调用模型</p></div>
    </aside>
    <div class="app-main">
      <header class="topbar">
        <div class="topbar-title"><h1>{{ currentPage.label }}</h1><span v-if="activeTab === 'graph'" class="topbar-section">/ {{ graphLabel }}</span><span v-if="novel" class="topbar-book" :title="novel.filename">{{ novel.filename.replace(/\.txt$/i, '') }}</span></div>
        <label v-if="activeTab === 'graph'" class="mobile-graph-nav"><span class="sr-only">图谱页面</span><select :value="graphSection" @change="navigate('graph', ($event.target as HTMLSelectElement).value as GraphSection)"><option v-for="section in graphSections" :key="section.id" :value="section.id">{{ section.label }}</option></select></label>
        <button class="button secondary import-button" :disabled="uploading || runActive || embeddingUpdating" @click="fileInput?.click()"><AppIcon name="upload" /><span>{{ uploading ? '正在导入…' : '导入小说' }}</span></button>
      </header>
      <input ref="fileInput" class="sr-only" type="file" aria-label="导入 TXT 小说" accept=".txt,text/plain" :disabled="uploading || runActive || embeddingUpdating" @change="upload(($event.target as HTMLInputElement).files?.[0])" />
      <main>
        <p v-if="error" class="global-error" role="alert">{{ error }}<button class="icon-button" aria-label="关闭提示" @click="error = ''">×</button></p>
        <div v-if="loading" class="empty-state" role="status"><p>正在打开你的书架…</p></div>
        <section v-else-if="activeTab === 'library'" class="library-page">
          <div class="library-toolbar"><div><strong>你的故事收藏</strong><span class="count-chip">{{ novels.length }} 部</span></div><label class="library-search"><AppIcon name="search" /><input v-model="libraryQuery" placeholder="搜索书名" aria-label="搜索书名" /></label></div>
          <div class="book-grid">
            <button v-for="item in filteredNovels" :key="item.novel_id" class="book-card" :class="{ selected: novel?.novel_id === item.novel_id }" :disabled="runActive || uploading || embeddingUpdating" @click="openNovel(item)">
              <div class="book-cover" :class="`cover-${coverVariant(item.novel_id)}`" aria-hidden="true"><AppIcon name="book" /><span>{{ item.filename.replace(/\.txt$/i, '') }}</span><small>NOVEL LENS</small></div>
              <div class="book-card-info"><span class="selected-label">{{ novel?.novel_id === item.novel_id ? '正在阅读' : '书架藏书' }}</span><h3 :title="item.filename">{{ item.filename.replace(/\.txt$/i, '') }}</h3><p>{{ formatNumber(item.character_count) }} 字 · {{ item.section_count }} 章</p><div><span>打开故事</span><AppIcon name="arrow" /></div></div>
            </button>
            <button class="upload-zone import-tile" :class="{ dragging }" :disabled="runActive || uploading || embeddingUpdating" aria-label="选择或拖入 TXT 小说" @click="fileInput?.click()" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="handleDrop"><span class="upload-icon"><AppIcon name="upload" /></span><strong>{{ uploading ? '正在识别章节…' : '添加下一部故事' }}</strong><small>{{ runActive ? '分析结束后可导入' : '选择或拖入 TXT · 最大 50 MiB' }}</small></button>
          </div>
          <div v-if="!filteredNovels.length" class="empty-state"><AppIcon name="book" /><h2>{{ libraryQuery ? '没有找到这部小说' : '书架还是空的' }}</h2><p>{{ libraryQuery ? '换个书名试试。' : '导入第一部小说，开始探索人物、故事和原文。' }}</p></div>
        </section>
        <section v-if="novel && !loading" v-show="activeTab !== 'library'" class="workbench" :class="`page-${activeTab}`">
          <AgentWorkspace v-show="activeTab === 'agent'" :novel="novel" :config="config" :retrieval-revision="retrievalRevision" @running-change="runActive = $event" />
          <StructurePanel v-if="visited.has('structure')" v-show="activeTab === 'structure'" :novel="novel" :active="activeTab === 'structure'" />
          <SearchPanel v-if="visited.has('search')" v-show="activeTab === 'search'" :novel="novel" :retrieval-revision="retrievalRevision" :active="activeTab === 'search'" />
          <KnowledgeGraphPanel v-if="visited.has('graph')" v-show="activeTab === 'graph'" :key="novel.novel_id" :novel="novel" :active="activeTab === 'graph'" :section="graphSection" @navigate="navigate('graph', $event)" />
          <section v-if="activeTab === 'settings'" class="workspace-panel settings-panel">
            <div class="settings-config"><div class="section-kicker">检索方式</div><h2>让问题找到原文</h2><p>关键词检索始终可用。开启语义索引后，也能按意思找到相关段落。</p>
              <label class="embedding-toggle" :class="{ unavailable: !config?.embedding_configured }"><input type="checkbox" :checked="retrievalStatus?.embedding_enabled ?? false" :disabled="!config?.embedding_configured || runActive || uploading || embeddingUpdating" @change="toggleEmbedding" /><span class="toggle-track"><i /></span><span><strong>{{ embeddingLabel }}</strong><small>{{ config?.embedding_model || '请先在后端配置语义检索模型' }}</small></span></label>
              <div class="settings-note"><AppIcon name="book" /><p>索引按当前小说保存。开启时会将小说片段发送给语义检索模型，可能产生用量费用。</p></div>
              <dl class="settings-book"><div><dt>当前小说</dt><dd>{{ novel.filename }}</dd></div><div><dt>正文规模</dt><dd>{{ formatNumber(novel.character_count) }} 字 / {{ novel.chunk_count }} 个文本片段</dd></div></dl>
            </div>
            <RetrievalObservability :novel-id="novel.novel_id" :refresh-key="retrievalRevision" dashboard />
          </section>
        </section>
        <div v-if="!loading && !novel && activeTab !== 'library'" class="empty-state"><p>先从书架选择或导入一部小说。</p><button class="button primary" @click="navigate('library')">前往书架</button></div>
      </main>
    </div>
  </div>
</template>
