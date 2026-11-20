<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import type { ConfigStatus, NovelInfo, RetrievalStatus } from './types'
import AgentWorkspace from './components/AgentWorkspace.vue'
import SearchPanel from './components/SearchPanel.vue'
import StructurePanel from './components/StructurePanel.vue'
import AppIcon from './components/AppIcon.vue'
import RetrievalObservability from './components/RetrievalObservability.vue'
const KnowledgeGraphPanel = defineAsyncComponent(() => import('./components/KnowledgeGraphPanel.vue'))

type TabId = 'library' | 'agent' | 'structure' | 'search' | 'graph' | 'settings'
const SELECTED_NOVEL_KEY = 'novel-agent:selected-novel'

const config = ref<ConfigStatus | null>(null)
const novel = ref<NovelInfo | null>(null)
const novels = ref<NovelInfo[]>([])
const activeTab = ref<TabId>(pageFromHash())
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
const pageNumber = computed(() => String(tabs.findIndex(tab => tab.id === activeTab.value) + 1).padStart(2, '0'))
const filteredNovels = computed(() => novels.value.filter(item => item.filename.toLocaleLowerCase().includes(libraryQuery.value.trim().toLocaleLowerCase())))

function pageFromHash(): TabId {
  const page = window.location.hash.slice(2)
  return ['library', 'agent', 'structure', 'search', 'graph', 'settings'].includes(page) ? page as TabId : 'agent'
}
function navigate(page: TabId) {
  activeTab.value = page
  visited.value.add(page)
  if (window.location.hash !== `#/${page}`) window.location.hash = `/${page}`
}
function syncPage() { navigate(pageFromHash()) }
function openNovel(item: NovelInfo) { selectNovel(item); navigate('agent') }

onMounted(async () => {
  window.addEventListener('hashchange', syncPage)
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
      <a class="brand" href="#/library"><span class="brand-mark">
          <AppIcon name="book" />
        </span><span><strong>Novel Lens</strong><small>读懂故事的每一面</small></span></a>
      <div v-if="novel" class="book-strip">
        <label class="book-selector"><span>当前小说</span><select :value="novel.novel_id"
            :disabled="runActive || uploading || embeddingUpdating"
            @change="selectNovelById(($event.target as HTMLSelectElement).value)">
            <option v-for="item in novels" :key="item.novel_id" :value="item.novel_id">{{ item.filename }}</option>
          </select></label>
        <small>{{ formatNumber(novel.character_count) }} 字 · {{ novel.section_count }} 章</small>
      </div>
      <p class="nav-caption">阅读工作台</p>
      <nav class="tabs" aria-label="工作台功能">
        <button v-for="tab in tabs" :key="tab.id" :class="{ active: activeTab === tab.id }"
          :aria-current="activeTab === tab.id ? 'page' : undefined" :disabled="!novel && tab.id !== 'library'"
          @click="navigate(tab.id)">
          <AppIcon :name="tab.id" /><span>{{ tab.label }}</span><i v-if="tab.id === 'agent' && runActive"
            class="running-dot" />
        </button>
      </nav>
      <div class="sidebar-bottom"><span class="model-badge"
          :class="{ ready: config?.ready }"><i />{{ config?.ready ? '模型已配置' : '模型未配置' }}</span><small
          :title="config?.model_name">{{ config?.model_name || '请检查模型配置' }}</small>
        <p>小说保存在本地<br />分析时按需调用模型</p>
      </div>
    </aside>

    <div class="app-main">
      <header class="topbar">
        <div class="breadcrumb">阅读工作台 <span>/</span> <strong>{{ currentPage.label }}</strong></div><button
          class="button secondary import-button" :disabled="uploading || runActive || embeddingUpdating"
          @click="fileInput?.click()">
          <AppIcon name="upload" />{{ uploading ? '正在导入…' : '导入小说' }}
        </button>
      </header>
      <input ref="fileInput" class="sr-only" type="file" aria-label="导入 TXT 小说" accept=".txt,text/plain"
        :disabled="uploading || runActive || embeddingUpdating"
        @change="upload(($event.target as HTMLInputElement).files?.[0])" />
      <main>
        <div class="page-heading">
          <div>
            <p class="eyebrow">NOVEL LENS /
              {{ pageNumber }}
            </p>
            <h1>{{ currentPage.label }}</h1>
            <p>{{ currentPage.description }}</p>
          </div><span v-if="novel && activeTab !== 'library'" class="current-book">
            <AppIcon name="book" />{{ novel.filename }}
          </span><span v-else-if="novels.length" class="count-chip">{{ novels.length }} 部小说</span>
        </div>
        <p v-if="error" class="global-error" role="alert">{{ error }} <button class="text-button" aria-label="关闭提示"
            @click="error = ''">×</button></p>
        <div v-if="loading" class="empty-state" role="status">
          <p>正在打开你的书架…</p>
        </div>
        <section v-else-if="activeTab === 'library'" class="library-page">
          <div class="library-toolbar">
            <h2>已导入的小说 <span>{{ novels.length }}</span></h2><label class="library-search">
              <AppIcon name="search" /><input v-model="libraryQuery" placeholder="搜索书名" aria-label="搜索书名" />
            </label>
          </div>
          <label class="upload-zone" :class="{ dragging, disabled: runActive || uploading || embeddingUpdating }"
            tabindex="0" role="button" aria-label="选择或拖入 TXT 小说"
            :aria-disabled="runActive || uploading || embeddingUpdating" @keydown.enter.prevent="fileInput?.click()"
            @keydown.space.prevent="fileInput?.click()" @click="fileInput?.click()" @dragover.prevent="dragging = true"
            @dragleave.prevent="dragging = false" @drop.prevent="handleDrop"><span class="upload-icon">
              <AppIcon name="upload" />
            </span><span><strong>{{ uploading ? '正在导入，稍等片刻…' : '把下一部故事，放进书架' }}</strong><small>{{ runActive ? '分析结束后可导入新小说' : '点击选择或拖入 TXT 文件，最大 50 MiB' }}</small></span><span
              class="upload-action">{{ uploading ? '识别章节中' : '选择文件 ↗' }}</span></label>
          <div v-if="filteredNovels.length" class="book-grid"><button v-for="(item, index) in filteredNovels"
              :key="item.novel_id" class="book-card" :class="{ selected: novel?.novel_id === item.novel_id }"
              :disabled="runActive || uploading || embeddingUpdating" @click="openNovel(item)">
              <div class="book-cover" :class="`cover-${index % 4}`">
                <AppIcon name="book" /><span>{{ item.filename.replace(/\.txt$/i, '') }}</span><small>NOVEL LENS</small>
              </div>
              <div class="book-card-info"><span v-if="novel?.novel_id === item.novel_id"
                  class="selected-label">当前阅读</span>
                <h3 :title="item.filename">{{ item.filename.replace(/\.txt$/i, '') }}</h3>
                <p>{{ formatNumber(item.character_count) }} 字 <span>·</span> {{ item.section_count }} 章</p>
                <div><span>开始问答</span>
                  <AppIcon name="arrow" />
                </div>
              </div>
            </button></div>
          <div v-else class="empty-state">
            <AppIcon name="book" />
            <h3>{{ libraryQuery ? '没有找到这部小说' : '书架还是空的' }}</h3>
            <p>{{ libraryQuery ? '换个书名试试。' : '导入第一部小说，就可以提问、查原文、看人物关系。' }}</p>
          </div>
        </section>
        <section v-if="novel && !loading" v-show="activeTab !== 'library'" class="workbench"
          :class="{ 'chat-workbench': activeTab === 'agent' }">
          <AgentWorkspace v-show="activeTab === 'agent'" :novel="novel" :config="config"
            :retrieval-revision="retrievalRevision" @running-change="runActive = $event" />
          <StructurePanel v-if="visited.has('structure')" v-show="activeTab === 'structure'" :novel="novel" />
          <SearchPanel v-if="visited.has('search')" v-show="activeTab === 'search'" :novel="novel"
            :retrieval-revision="retrievalRevision" />
          <KnowledgeGraphPanel v-if="visited.has('graph')" v-show="activeTab === 'graph'" :key="novel.novel_id"
            :novel="novel" />
          <section v-if="activeTab === 'settings'" class="workspace-panel settings-panel">
            <div class="settings-intro">
              <div>
                <h2>语义检索</h2>
                <p>按意思查找相关段落，补充关键词搜索。首次开启时会为当前小说建立索引。</p>
              </div><label class="embedding-toggle" :class="{ unavailable: !config?.embedding_configured }"><input
                  type="checkbox" :checked="retrievalStatus?.embedding_enabled ?? false"
                  :disabled="!config?.embedding_configured || runActive || uploading || embeddingUpdating"
                  @change="toggleEmbedding" /><span
                  class="toggle-track"><i /></span><span><strong>{{ embeddingLabel }}</strong><small>{{ config?.embedding_model || '请先在后端配置语义检索模型' }}</small></span></label>
            </div>
            <p class="settings-note">开启后会将小说片段发送给语义检索模型，可能产生用量费用。未开启时仍可使用关键词搜索和小说问答。</p>
            <RetrievalObservability :novel-id="novel.novel_id" :refresh-key="retrievalRevision" />
          </section>
        </section>
        <div v-if="!loading && !novel && activeTab !== 'library'" class="empty-state">
          <p>先从书架选择或导入一部小说。</p><button class="button primary" @click="navigate('library')">前往书架</button>
        </div>
      </main>
    </div>
  </div>
</template>
