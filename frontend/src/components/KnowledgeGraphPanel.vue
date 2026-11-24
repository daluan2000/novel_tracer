<script setup lang="ts">
import { computed, onBeforeUnmount, ref, toRef, watch } from 'vue'
import { api, ApiError, graphApi } from '../api'
import type { NovelInfo, SectionItem } from '../types'
import type { GraphContext, GraphEdge, GraphFilters, GraphGroup, GraphNode, GraphSpan, GraphUnresolved } from '../graphTypes'
import { useKnowledgeGraph } from '../composables/useKnowledgeGraph'
import KnowledgeGraphCanvas from './KnowledgeGraphCanvas.vue'
import GraphEdits from './GraphEdits.vue'
import GraphBuildPage from './GraphBuildPage.vue'
import InspectorPane from './InspectorPane.vue'
import PanelDrawer from './PanelDrawer.vue'
import type { GraphSection } from '../navigation'

const props = withDefaults(defineProps<{ novel: NovelInfo; section?: GraphSection; active?: boolean }>(), { active: true, section: 'explore' })
const emit = defineEmits<{ navigate: [section: GraphSection] }>()
const queryOpen = ref(false), inspectorOpen = ref(false), mentionOpen = ref(false)
const inspectorTab = ref('relations')
const novelId = computed(() => props.novel.novel_id)
const graph = useKnowledgeGraph(toRef(() => props.novel.novel_id))
const { status, error, busy, loading, running, root, target, candidates, targetCandidates, result } = graph
const queryText = ref(''), targetText = ref(''), mode = ref<'neighbors' | 'paths'>('neighbors'), depth = ref(1)
const entityType = ref(''), relationType = ref(''), start = ref(''), end = ref('')
const chapters = ref<SectionItem[]>([])
const chapterError = ref('')
const selectedEdge = ref<GraphEdge | null>(null)
const groups = ref<GraphGroup[]>([]), evidenceTotal = ref(0), evidenceBusy = ref(false)
const context = ref<GraphContext | null>(null)
const mentionContext = ref<GraphContext | null>(null)
const typeLabels: Record<string, string> = { person: '人物', place: '地点', organization: '组织', item: '物品', skill: '功法', concept: '专有概念' }
const nameFormLabels: Record<string, string> = { full_name: '全名', given_name: '名', address: '称呼', alias: '别名', proper_name: '专名' }
const pendingReasonLabels: Record<string, string> = { competing_identities: '存在多个身份候选', insufficient_identity_evidence: '身份依据不足', context_conflict: '上下文中的所属指代与身份存在冲突，等待进一步确认' }
const labels = computed(() => Object.fromEntries(status.value?.ontology.relation_types.map(r => [r.type, r.label]) || []))
const symmetric = computed(() => status.value?.ontology.relation_types.filter(r => r.symmetric).map(r => r.type) || [])
const rangeError = computed(() => start.value && end.value && chapters.value.findIndex(c => c.chapter_id === start.value) > chapters.value.findIndex(c => c.chapter_id === end.value) ? '起始章节不能晚于结束章节。' : '')
const filters = computed<GraphFilters>(() => ({ startChapterId: start.value || undefined, endChapterId: end.value || undefined,
  entityTypes: entityType.value ? [entityType.value] : [], relationTypes: relationType.value ? [relationType.value] : [] }))
let chapterSequence = 0, evidenceSequence = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
let targetTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const unresolvedItems = ref<GraphUnresolved[]>([]), unresolvedTotal = ref<number | null>(null)
const unresolvedBusy = ref(false), unresolvedError = ref('')
let unresolvedSequence = 0
watch([novelId, () => status.value?.active_version, filters], () => {
  unresolvedSequence++; unresolvedItems.value = []; unresolvedTotal.value = null
  unresolvedBusy.value = false; unresolvedError.value = ''
})
async function loadUnresolved() {
  const version = status.value?.active_version
  if (!version || unresolvedBusy.value) return
  const sequence = ++unresolvedSequence
  unresolvedBusy.value = true; unresolvedError.value = ''
  try {
    const page = await graphApi.unresolved(novelId.value, version, filters.value, unresolvedItems.value.length)
    if (sequence === unresolvedSequence && !disposed) {
      unresolvedItems.value.push(...page.items); unresolvedTotal.value = page.total
    }
  } catch (reason) {
    if (sequence === unresolvedSequence && !disposed) {
      unresolvedError.value = reason instanceof Error ? reason.message : '待确认记录加载失败。'
      if (reason instanceof ApiError && reason.code === 'graph_version_changed') await graph.refresh()
    }
  } finally { if (sequence === unresolvedSequence && !disposed) unresolvedBusy.value = false }
}

watch(novelId, async id => {
  const sequence = ++chapterSequence
  queryOpen.value = false; inspectorOpen.value = false; mentionOpen.value = false
  queryText.value = ''; targetText.value = ''; start.value = ''; end.value = ''
  entityType.value = ''; relationType.value = ''; chapters.value = []; chapterError.value = ''
  try {
    const all: SectionItem[] = []
    while (true) {
      const page = await api.sections(id, all.length, 200)
      if (sequence !== chapterSequence || disposed) return
      all.push(...page.items)
      if (!page.items.length || all.length >= page.total) break
    }
    chapters.value = all
  } catch { if (sequence === chapterSequence && !disposed) chapterError.value = '章节加载失败，请刷新页面后重试。' }
}, { immediate: true })
watch(queryText, value => {
  graph.invalidate()
  root.value = null; result.value = null; candidates.value = []
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { if (!rangeError.value) void graph.search(value, filters.value) }, 300)
})
watch(targetText, value => {
  graph.invalidate()
  target.value = null; targetCandidates.value = []
  if (targetTimer) clearTimeout(targetTimer)
  targetTimer = setTimeout(() => { if (!rangeError.value) void graph.search(value, filters.value, 1) }, 300)
})
watch([mode, depth, filters], () => {
  graph.invalidate()
  selectedEdge.value = null; groups.value = []; context.value = null; evidenceSequence++
  if (!rangeError.value && !chapterError.value) {
    void graph.search(queryText.value, filters.value)
    void graph.search(targetText.value, filters.value, 1)
    void submit()
  } else result.value = null
})
watch(() => result.value, () => { inspectorOpen.value = false; inspectorTab.value = 'relations'; selectedEdge.value = null; groups.value = []; context.value = null; evidenceBusy.value = false; evidenceSequence++ })
watch(() => status.value?.active_version, () => { selectedEdge.value = null; groups.value = []; context.value = null; mentionContext.value = null; mentionOpen.value = false; evidenceSequence++ })
async function submit() {
  if (!rangeError.value && !chapterError.value) await graph.query(depth.value, filters.value, mode.value)
}
function choose(node: GraphNode, slot: 0 | 1 = 0) {
  if (slot === 0) { root.value = node; candidates.value = [] }
  else { target.value = node; targetCandidates.value = [] }
  void submit()
}
function chooseNode(id: string) {
  const node = result.value?.nodes.find(n => n.id === id)
  if (node) choose(node)
}
function clearEdgeSelection() {
  evidenceSequence++; selectedEdge.value = null; groups.value = []; evidenceTotal.value = 0; context.value = null; evidenceBusy.value = false
}
async function selectEdge(edge: GraphEdge) {
  evidenceSequence++
  evidenceBusy.value = false
  inspectorOpen.value = true; inspectorTab.value = 'evidence'
  selectedEdge.value = edge; context.value = null; groups.value = edge.evidence_groups; evidenceTotal.value = edge.evidence_total
}
async function moreEvidence() {
  const edge = selectedEdge.value, version = status.value?.active_version
  if (!edge || !version) return
  const sequence = ++evidenceSequence
  evidenceBusy.value = true
  try {
    const page = await graphApi.evidence(novelId.value, version, edge.id, filters.value, groups.value.length)
    if (sequence === evidenceSequence && !disposed) { groups.value.push(...page.items); evidenceTotal.value = page.total }
  } catch (reason) {
    if (sequence === evidenceSequence && !disposed) {
      if (reason instanceof ApiError && reason.code === 'graph_version_changed') await graph.refresh()
      error.value = reason instanceof Error ? reason.message : '证据加载失败。'
    }
  } finally { if (sequence === evidenceSequence && !disposed) evidenceBusy.value = false }
}
async function readContext(span: GraphSpan) {
  const sequence = ++evidenceSequence, id = novelId.value
  const fromMention = props.section === 'entities'
  evidenceBusy.value = false
  try {
    const chunks = await api.context(id, span.chunk_id)
    if (sequence === evidenceSequence && !disposed) {
      if (fromMention) { mentionContext.value = { span, chunks }; mentionOpen.value = true }
      else context.value = { span, chunks }
    }
  } catch { if (sequence === evidenceSequence && !disposed) error.value = '上下文读取失败。' }
}
const name = (id: string) => result.value?.nodes.find(n => n.id === id)?.name || id
onBeforeUnmount(() => { disposed = true; chapterSequence++; evidenceSequence++; if (searchTimer) clearTimeout(searchTimer); if (targetTimer) clearTimeout(targetTimer) })
</script>

<template>
  <section class="workspace-panel knowledge-graph" :class="`graph-section-${section}`">
    <p v-if="error" class="global-error" role="alert">{{ error }}</p>
    <div v-if="!status" class="empty-state" role="status"><p>正在读取图谱状态…</p><button v-if="error" class="button secondary" @click="graph.refresh()">重新加载</button></div>
    <template v-if="status">
      <GraphBuildPage v-show="section === 'build'" :status="status" :busy="busy" :running="running" :active="active && section === 'build'" @build="graph.build" @stop="graph.stop" @refresh="graph.refresh" />
      <GraphEdits :novel-id="novelId" :version="status.active_version || ''" :root="root" :running="running" :pending-corrections="status.revision?.pending_corrections || 0" :section="section === 'history' ? 'history' : 'entities'" :active="active && (section === 'history' || section === 'entities')" @changed="graph.refresh()"><template #unresolved>      <section class="graph-unresolved"><div class="workspace-toolbar"><strong>待确认的原文提及</strong><span class="toolbar-hint">身份尚未确定的原文记录</span></div>
        <p>这里保留尚无法确定身份的称呼及原文，相关事实暂不进入正式图谱。</p>
        <button type="button" class="text-button" :disabled="unresolvedBusy || (unresolvedTotal !== null && unresolvedItems.length >= unresolvedTotal)" @click="loadUnresolved">{{ unresolvedBusy ? '加载中…' : unresolvedTotal === null ? '查看待确认记录' : '加载更多' }}</button>
        <p v-if="unresolvedError" role="alert">{{ unresolvedError }}</p>
        <p v-if="unresolvedTotal !== null">当前范围 {{ unresolvedTotal }} 处待确认提及</p>
        <ul><li v-for="item in unresolvedItems" :key="item.mention_id"><strong>{{ item.surface }}</strong> · {{ item.anchor.section_title || item.anchor.section_id }}
          <p>{{ pendingReasonLabels[item.reason] || '身份依据不足' }}</p>
          <blockquote>{{ item.contexts[0]?.quote || item.anchor.quote }}</blockquote>
          <p v-if="item.candidates.length">候选：{{ item.candidates.map(c => c.name).join('、') }}</p>
          <button type="button" class="text-button" @click="readContext(item.anchor)">读取原文</button>
        </li></ul>
      </section>
</template></GraphEdits>
      <section v-show="section === 'explore'" class="graph-explore-page">
        <div class="workspace-toolbar graph-explore-toolbar"><div class="toolbar-summary"><span class="status-pill" :class="{ working: running }">{{ running ? '正在构建' : status.available ? '图谱就绪' : '尚未构建' }}</span><span v-if="status.counts" class="toolbar-hint">{{ status.counts.entity_count }} 个实体 · {{ status.counts.relation_count }} 条关系</span></div><div class="toolbar-actions"><button class="button secondary mobile-query-button" @click="queryOpen = true">查询与筛选</button><button v-if="result" class="button secondary mobile-query-button" @click="inspectorOpen = true">关系与证据</button><button class="text-button" @click="emit('navigate', 'build')">{{ running ? '查看构建进度 →' : '构建任务 →' }}</button></div></div>
        <div v-if="!status.available" class="empty-state graph-unbuilt"><span class="empty-illustration">◇</span><h2>让故事里的关系浮现</h2><p>先从原文构建图谱，再探索人物、地点和事件之间的联系。</p><button class="button primary" @click="emit('navigate', 'build')">前往构建图谱</button></div>
        <div v-else class="graph-explore-layout">
          <InspectorPane class="graph-query-pane" :open="active && section === 'explore' && queryOpen" title="查询与筛选" @close="queryOpen = false">      <form class="graph-controls" @submit.prevent="submit">
        <label class="field"><span>查询方式</span><select v-model="mode"><option value="neighbors">查看相关人物与事物</option><option value="paths">查找两者之间的联系</option></select></label>
        <details class="graph-filter-details"><summary>按章节、类型与关联层数筛选</summary><div class="graph-filter-grid">
        <label class="field"><span>实体类型</span><select v-model="entityType"><option value="">全部类型</option><option v-for="type in status.ontology.entity_types" :key="type" :value="type">{{ typeLabels[type] || type }}</option></select></label>
        <label class="field"><span>关系类型</span><select v-model="relationType"><option value="">全部关系</option><option v-for="relation in status.ontology.relation_types" :key="relation.type" :value="relation.type">{{ relation.label }}</option></select></label>
        <label class="field"><span>跳数</span><select v-model="depth"><option v-for="n in [1,2,3]" :key="n" :value="n">{{ n }} 跳</option></select></label>
        <label class="field"><span>起始章节</span><select v-model="start"><option value="">全书开头</option><option v-for="(chapter, i) in chapters" :key="chapter.chapter_id" :value="chapter.chapter_id">{{ i + 1 }} · {{ chapter.title || '无标题区段' }}</option></select></label>
        <label class="field"><span>结束章节</span><select v-model="end"><option value="">全书末尾</option><option v-for="(chapter, i) in chapters" :key="chapter.chapter_id" :value="chapter.chapter_id">{{ i + 1 }} · {{ chapter.title || '无标题区段' }}</option></select></label>
        </div></details>
        <div class="graph-entity-search"><label class="field"><span>搜索实体或别名</span><input v-model="queryText" placeholder="输入人物、地点、组织…" /></label>
          <div v-if="root" class="tags"><span>已选：{{ root.name }} · {{ typeLabels[root.type] }}</span></div>
          <p v-if="root" class="graph-notice">其他名称：{{ root.aliases.length ? root.aliases.join('、') : '暂无已收录别名' }}</p>
          <p v-if="root?.person_name" class="graph-notice">姓：{{ root.person_name.family_name }} · 名：{{ root.person_name.given_name }}</p>
          <p v-if="root?.name_forms?.length" class="graph-notice">名称形式：{{ root.name_forms.map(f => `${f.name}（${nameFormLabels[f.kind]}）`).join('、') }}</p>
          <ul v-if="candidates.length" class="graph-candidates"><li v-for="node in candidates" :key="node.id"><button type="button" @click="choose(node); queryOpen = false">{{ node.name }} · {{ typeLabels[node.type] }} · {{ node.first_mention?.section_title || node.first_mention?.section_id }} 行 {{ node.first_mention?.start_line }}<small v-if="node.aliases.length">其他名称：{{ node.aliases.join('、') }}</small></button></li></ul>
        </div>
        <div v-if="mode === 'paths'" class="graph-entity-search"><label class="field"><span>目标实体</span><input v-model="targetText" placeholder="输入另一个实体名称" /></label>
          <div v-if="target" class="tags"><span>已选：{{ target.name }}</span></div>
          <p v-if="target" class="graph-notice">其他名称：{{ target.aliases.length ? target.aliases.join('、') : '暂无已收录别名' }}</p>
          <ul v-if="targetCandidates.length" class="graph-candidates"><li v-for="node in targetCandidates" :key="node.id"><button type="button" @click="choose(node, 1); queryOpen = false">{{ node.name }} · {{ typeLabels[node.type] }} · {{ node.first_mention?.section_title || node.first_mention?.section_id }} 行 {{ node.first_mention?.start_line }}<small v-if="node.aliases.length">其他名称：{{ node.aliases.join('、') }}</small></button></li></ul>
        </div>
        <button class="button primary" :disabled="loading || !root || (mode === 'paths' && !target) || !!rangeError || !!chapterError">{{ loading ? '检索中…' : '查询关系' }}</button>
      </form>
      <p v-if="rangeError || chapterError" class="field-error" role="alert">{{ rangeError || chapterError }}</p>
      <p class="graph-notice filter-range">范围：{{ start ? chapters.find(c => c.chapter_id === start)?.title || '指定起点' : '全书开头' }} — {{ end ? chapters.find(c => c.chapter_id === end)?.title || '指定终点' : '全书末尾' }}。关系以原文为准，章节顺序未必等于故事发生顺序。</p>
</InspectorPane>
          <div class="graph-stage"><p v-if="result?.truncated" class="graph-warning" role="status">结果已截断或检索未完成（{{ result.truncation_reason }}），请缩小范围。</p>        <KnowledgeGraphCanvas v-if="result?.edges.length" :result="result" :reset-key="`${root?.id}:${mode}`" :root-id="root?.id" :selected-edge-id="selectedEdge?.id" :labels="labels" :symmetric="symmetric" @node="chooseNode" @clear="clearEdgeSelection" @edge="id => { const edge = result?.edges.find(e => e.id === id); if (edge) selectEdge(edge) }" />
        <p v-if="result && !result.edges.length" class="graph-notice">{{ mode === 'paths' && result.paths.length ? '起点与终点是同一实体：零跳路径。' : result.search_complete ? '在当前图谱、范围及跳数内未找到关系。' : '本次预算内未找到关系，检索尚未完成。' }}</p>
        <ol v-if="result?.paths.length" class="graph-paths"><li v-for="(path, i) in result.paths" :key="i">{{ path.node_ids.map(name).join(' — ') }}</li></ol>
<div v-if="!result" class="graph-canvas-empty"><div class="empty-orbit"><i /><i /><i /><span>人物 · 故事 · 联系</span></div><h2>{{ loading ? '正在展开关系…' : '从一个名字，走进故事' }}</h2><p>搜索并选择人物，展开关系与原文依据。</p><button class="button secondary mobile-query-button" @click="queryOpen = true">选择查询实体</button></div></div>
          <InspectorPane :open="active && section === 'explore' && inspectorOpen" title="关系与证据" @close="inspectorOpen = false"><nav class="segmented-tabs" aria-label="图谱检查器"><button :aria-current="inspectorTab === 'relations' ? 'page' : undefined" @click="inspectorTab = 'relations'">关系列表 <span v-if="result">{{ result.edges.length }}</span></button><button :aria-current="inspectorTab === 'evidence' ? 'page' : undefined" @click="inspectorTab = 'evidence'">原文证据</button></nav><template v-if="result">          <section v-show="inspectorTab === 'relations' && result"><ul class="graph-edge-list"><li v-for="edge in result.edges" :key="edge.id"><button type="button" :class="{ selected: selectedEdge?.id === edge.id }" @click="selectEdge(edge)">
            {{ name(edge.source) }} {{ symmetric.includes(edge.type) ? '↔' : '→' }} {{ name(edge.target) }} · {{ labels[edge.type] || edge.type }}
            <strong v-if="edge.polarity === 'denied'">（否认）</strong><span v-if="edge.statement_mode === 'character_claim'">（人物说法）</span><small>{{ edge.evidence_total }} 组原文依据</small>
          </button></li></ul></section>
          <section v-show="inspectorTab === 'evidence'" class="graph-evidence"><p v-if="!selectedEdge">点击关系查看原文。</p><article v-for="group in groups" :key="group.id"><blockquote v-for="(span, i) in group.spans" :key="i"><p>{{ span.quote }}</p><small>{{ span.section_title || span.section_id }} · 行 {{ span.start_line }}–{{ span.end_line }}</small><button class="text-button" @click="readContext(span)">展开原文上下文</button></blockquote></article>
            <button v-if="groups.length < evidenceTotal" class="text-button" :disabled="evidenceBusy" @click="moreEvidence">加载更多证据（{{ groups.length }}/{{ evidenceTotal }}）</button>
            <div v-if="context" class="context-block"><strong>展开原文上下文（可能包含筛选范围外的邻接段）</strong><p v-for="chunk in context.chunks" :key="chunk.chunk_id"><small>Ln {{ chunk.start_line }}–{{ chunk.end_line }}</small>{{ chunk.text }}</p></div>
          </section>
</template><div v-else class="inspector-empty"><span>每条关系，都有出处</span><p>查询后选择一条关系，在这里核对原文。</p></div></InspectorPane>
        </div>
      </section>
      <PanelDrawer :open="active && section === 'entities' && mentionOpen" title="待确认提及的原文" @close="mentionOpen = false"><div v-if="mentionContext" class="context-block source-reader"><p v-for="chunk in mentionContext.chunks" :key="chunk.chunk_id"><span>行 {{ chunk.start_line }}–{{ chunk.end_line }}</span>{{ chunk.text }}</p></div></PanelDrawer>
    </template>
  </section>
</template>
