<script setup lang="ts">
import { computed, onBeforeUnmount, ref, toRef, watch } from 'vue'
import { api, ApiError, graphApi } from '../api'
import type { NovelInfo, SectionItem } from '../types'
import type { GraphContext, GraphEdge, GraphFilters, GraphGroup, GraphNode, GraphSpan } from '../graphTypes'
import { useKnowledgeGraph } from '../composables/useKnowledgeGraph'
import KnowledgeGraphCanvas from './KnowledgeGraphCanvas.vue'

const props = defineProps<{ novel: NovelInfo }>()
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
const typeLabels: Record<string, string> = { person: '人物', place: '地点', organization: '组织', item: '物品', skill: '功法', concept: '专有概念' }
const phases = { extracting: '抽取实体与关系', resolving: '实体身份消歧', materializing: '保存图谱', publishing: '发布版本' }
const states = { queued: '等待构建', building: '构建中', stopping: '等待当前请求结束', completed: '已完成', failed: '构建失败', stopped: '已停止' }
const buildErrors: Record<string, string> = {
  graph_structured_output_failed: '模型连续返回不符合结构要求的结果，格式重试次数已用尽。已保存完成的批次，点击“继续构建”可重试当前片段。',
  graph_model_timeout: '当前片段的模型请求超时。已完成批次已保存，点击“继续构建”可复用结果，并自动缩小较大的失败片段。',
  graph_model_output_limit: '当前片段的模型输出未完整返回。已保存完成的批次；继续构建会使用片段编号精简抽取，并自动缩小较大的片段。',
  graph_model_authentication_failed: '模型服务鉴权失败，请检查 API Key 后继续构建。',
  graph_model_rate_limited: '模型服务请求受限，请稍后继续构建。',
  graph_model_connection_failed: '无法连接模型服务，请检查网络及服务地址后继续。',
  service_interrupted: '服务已重启，已保存完成的批次，点击“继续构建”可恢复。',
  graph_build_failed: '构建处理发生异常，已保存完成的批次。请查看构建诊断及后端日志后继续。',
  graph_extraction_processing_failed: '抽取结果处理失败，模型成功返回的候选已缓存。修复后继续构建可复用缓存。',
  graph_checkpoint_invalid: '已缓存的片段与当前原文或数据格式不匹配，请检查图谱数据。',
}
const eventLabels: Record<string, string> = {
  graph_structured_output_failed: '模型结构化结果重试耗尽，构建已停止',
  structured_output_failed: '模型结果仍未通过结构校验',
  graph_model_timeout_retry: '模型请求超时，正在有限重试',
  graph_extraction_timeout_split: '超时片段已拆为较小片段',
  graph_extraction_output_split: '模型输出达到上限，已自动缩小片段',
  graph_extraction_compact_fallback: '已切换为精简抽取，使用片段编号引用原文',
  graph_extraction_capacity_split: '实体或关系较密集，已自动拆分片段',
  graph_extraction_capacity_saved: '最小片段实体或关系较密集，已保存所有通过校验的候选',
  graph_build_failed: '构建处理发生异常，请查看后端日志',
  graph_model_output_limit: '模型响应达到输出上限，未形成完整结果',
  graph_model_timeout: '模型请求超时',
  structured_output_retry: '模型返回格式不完整，正在重试',
}
function eventLabel(code: string) {
  if (eventLabels[code]) return eventLabels[code]
  const failure = /^graph_processing_failure:(.+):([A-Za-z_][A-Za-z0-9_]*)$/.exec(code)
  if (failure) return `片段 ${failure[1]} 处理失败（${failure[2]}），候选结果已缓存`
  const rejected = /^(rejected_entities|rejected_relations|rejected_identity_links):(\d+)$/.exec(code)
  if (rejected) {
    const kinds: Record<string, string> = { rejected_entities: '实体', rejected_relations: '关系', rejected_identity_links: '身份线索' }
    return `过滤了 ${rejected[2]} 个未通过校验的${kinds[rejected[1]!]}`
  }
  return code
}
const labels = computed(() => Object.fromEntries(status.value?.ontology.relation_types.map(r => [r.type, r.label]) || []))
const symmetric = computed(() => status.value?.ontology.relation_types.filter(r => r.symmetric).map(r => r.type) || [])
const rangeError = computed(() => start.value && end.value && chapters.value.findIndex(c => c.chapter_id === start.value) > chapters.value.findIndex(c => c.chapter_id === end.value) ? '起始章节不能晚于结束章节。' : '')
const filters = computed<GraphFilters>(() => ({ startChapterId: start.value || undefined, endChapterId: end.value || undefined,
  entityTypes: entityType.value ? [entityType.value] : [], relationTypes: relationType.value ? [relationType.value] : [] }))
let chapterSequence = 0, evidenceSequence = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
let targetTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false

watch(novelId, async id => {
  const sequence = ++chapterSequence
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
watch(() => result.value, () => { selectedEdge.value = null; groups.value = []; context.value = null; evidenceBusy.value = false; evidenceSequence++ })
watch(() => status.value?.active_version, () => { selectedEdge.value = null; groups.value = []; context.value = null; evidenceSequence++ })
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
async function selectEdge(edge: GraphEdge) {
  evidenceSequence++
  evidenceBusy.value = false
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
  evidenceBusy.value = false
  try {
    const chunks = await api.context(id, span.chunk_id)
    if (sequence === evidenceSequence && !disposed) context.value = { span, chunks }
  } catch { if (sequence === evidenceSequence && !disposed) error.value = '上下文读取失败。' }
}
const name = (id: string) => result.value?.nodes.find(n => n.id === id)?.name || id
onBeforeUnmount(() => { disposed = true; chapterSequence++; evidenceSequence++; if (searchTimer) clearTimeout(searchTimer); if (targetTimer) clearTimeout(targetTimer) })
</script>

<template>
  <section class="workspace-panel knowledge-graph">
    <div class="section-heading"><div><p class="eyebrow">知识图谱</p><h2>探索实体与关系</h2></div><span>自动抽取 · 原文可追溯</span></div>
    <p v-if="error" class="global-error" role="alert">{{ error }}</p>
    <div v-if="status" class="graph-status">
      <div class="graph-status-heading"><strong>{{ status.available ? '图谱可用' : '尚无可用图谱' }}</strong><small v-if="status.active_version">版本 {{ status.active_version.slice(0, 8) }}</small></div>
      <p>{{ status.model_name || '聊天模型未配置' }} · {{ status.source_characters.toLocaleString() }} 字 · 预计 {{ status.estimated_extraction_units }} 个抽取批次</p>
      <p v-if="status.max_output_tokens" class="graph-notice">单次输出最多 {{ status.max_output_tokens }} token · 请求超时上限 {{ status.request_timeout_seconds }} 秒。较大片段超时或输出达到上限时自动拆分；其他超时最多额外重试 {{ status.timeout_retries }} 次。</p>
      <p class="graph-notice">点击构建或重建会将全书正文分批发送给已配置的聊天模型，并产生模型用量。重建期间原图谱仍可使用。</p>
      <p v-if="status.error_code" role="alert">图谱暂不可用（{{ status.error_code }}），仍可使用文本检索。</p>
      <p v-if="status.counts">当前图谱：{{ status.counts.entity_count }} 个实体 · {{ status.counts.relation_count }} 条关系</p>
      <div v-if="status.job" class="graph-progress" aria-live="polite">
        <strong>{{ states[status.job.status] }} · {{ phases[status.job.phase] }}</strong>
        <span>抽取 {{ status.job.extraction.completed }}/{{ status.job.extraction.total }} · 消歧 {{ status.job.resolution.completed }}/{{ status.job.resolution.total }} · {{ status.job.elapsed_seconds.toFixed(1) }} 秒</span>
        <small v-if="status.job.extraction.cached">另有 {{ status.job.extraction.cached }} 批成功返回的候选已缓存，继续处理可复用。</small>
        <small>批次可能按文本密度细分；只有发布完成才表示全流程完成。</small>
        <span>{{ status.job.usage.request_count }} 次模型请求 · 已报告输入 {{ status.job.usage.reported_input_tokens }} / 输出 {{ status.job.usage.reported_output_tokens }} token<span v-if="status.job.usage.unknown_request_count"> · {{ status.job.usage.unknown_request_count }} 次请求用量未知</span></span>
        <small v-if="status.job.usage.unknown_request_count">超时或供应商未返回用量的请求无法统计 token，可能已产生消耗；继续构建会复用已完成批次。</small>
        <p v-if="status.job.error_code" role="alert">{{ buildErrors[status.job.error_code] || '构建未能完成，已保存进度。请检查模型配置后继续构建。' }}</p>
      </div>
      <div class="graph-actions">
        <button v-if="running" class="button" :disabled="busy || status.job?.stop_requested" @click="graph.stop()">{{ status.job?.stop_requested ? '正在停止…' : '停止构建' }}</button>
        <template v-else>
          <button v-if="status.job?.can_resume" class="button primary" :disabled="busy" @click="graph.build('resume')">继续构建</button>
          <button class="button" :class="{ primary: !status.available }" :disabled="busy || !status.configured || !!status.error_code" @click="graph.build(status.available || status.job ? 'rebuild' : 'start')">{{ busy ? '提交中…' : status.available || status.job ? '重新构建' : '构建图谱' }}</button>
        </template>
        <button class="text-button" :disabled="busy" @click="graph.refresh()">刷新状态</button>
      </div>
      <details v-if="status.job?.events.length"><summary>构建诊断（最近 {{ status.job.events.length }} 条）</summary><small>过滤项是校验后未采纳的候选；本次停止原因见上方提示。</small><ul><li v-for="(event, i) in status.job.events" :key="i">{{ eventLabel(event.code) }}</li></ul></details>
    </div>
    <div v-else class="empty-state"><p>正在读取图谱状态…</p></div>

    <template v-if="status?.available">
      <form class="graph-controls" @submit.prevent="submit">
        <label class="field"><span>查询方式</span><select v-model="mode"><option value="neighbors">实体邻居</option><option value="paths">两实体间路径</option></select></label>
        <label class="field"><span>实体类型</span><select v-model="entityType"><option value="">全部类型</option><option v-for="type in status.ontology.entity_types" :key="type" :value="type">{{ typeLabels[type] || type }}</option></select></label>
        <label class="field"><span>关系类型</span><select v-model="relationType"><option value="">全部关系</option><option v-for="relation in status.ontology.relation_types" :key="relation.type" :value="relation.type">{{ relation.label }}</option></select></label>
        <label class="field"><span>跳数</span><select v-model="depth"><option v-for="n in [1,2,3]" :key="n" :value="n">{{ n }} 跳</option></select></label>
        <label class="field"><span>起始章节</span><select v-model="start"><option value="">全书开头</option><option v-for="(chapter, i) in chapters" :key="chapter.chapter_id" :value="chapter.chapter_id">{{ i + 1 }} · {{ chapter.title || '无标题区段' }}</option></select></label>
        <label class="field"><span>结束章节</span><select v-model="end"><option value="">全书末尾</option><option v-for="(chapter, i) in chapters" :key="chapter.chapter_id" :value="chapter.chapter_id">{{ i + 1 }} · {{ chapter.title || '无标题区段' }}</option></select></label>
        <div class="graph-entity-search"><label class="field"><span>搜索实体或别名</span><input v-model="queryText" placeholder="输入人物、地点、组织…" /></label>
          <div v-if="root" class="tags"><span>已选：{{ root.name }} · {{ typeLabels[root.type] }}</span></div>
          <ul v-if="candidates.length" class="graph-candidates"><li v-for="node in candidates" :key="node.id"><button type="button" @click="choose(node)">{{ node.name }} · {{ typeLabels[node.type] }} · {{ node.first_mention?.section_title || node.first_mention?.section_id }} Ln {{ node.first_mention?.start_line }}</button></li></ul>
        </div>
        <div v-if="mode === 'paths'" class="graph-entity-search"><label class="field"><span>目标实体</span><input v-model="targetText" placeholder="输入另一个实体名称" /></label>
          <div v-if="target" class="tags"><span>已选：{{ target.name }}</span></div>
          <ul v-if="targetCandidates.length" class="graph-candidates"><li v-for="node in targetCandidates" :key="node.id"><button type="button" @click="choose(node, 1)">{{ node.name }} · {{ typeLabels[node.type] }} · {{ node.first_mention?.section_title || node.first_mention?.section_id }} Ln {{ node.first_mention?.start_line }}</button></li></ul>
        </div>
        <button class="button primary" :disabled="loading || !root || (mode === 'paths' && !target) || !!rangeError || !!chapterError">{{ loading ? '检索中…' : '查询关系' }}</button>
      </form>
      <p v-if="rangeError || chapterError" class="field-error" role="alert">{{ rangeError || chapterError }}</p>
      <p class="graph-notice">范围：{{ start ? chapters.find(c => c.chapter_id === start)?.title || '指定起点' : '全书开头' }} — {{ end ? chapters.find(c => c.chapter_id === end)?.title || '指定终点' : '全书末尾' }}。章节证据不等于剧情时点；多跳连通不自动产生新事实。</p>
      <template v-if="result">
        <p v-if="result.truncated" class="graph-warning" role="status">结果已截断或检索未完成（{{ result.truncation_reason }}），请缩小范围。</p>
        <KnowledgeGraphCanvas v-if="result.edges.length" :result="result" :reset-key="`${root?.id}:${mode}`" :labels="labels" :symmetric="symmetric" @node="chooseNode" @edge="id => { const edge = result?.edges.find(e => e.id === id); if (edge) selectEdge(edge) }" />
        <p v-if="!result.edges.length" class="graph-notice">{{ mode === 'paths' && result.paths.length ? '起点与终点是同一实体：零跳路径。' : result.search_complete ? '在当前图谱、范围及跳数内未找到关系。' : '本次预算内未找到关系，检索尚未完成。' }}</p>
        <ol v-if="result.paths.length" class="graph-paths"><li v-for="(path, i) in result.paths" :key="i">{{ path.node_ids.map(name).join(' — ') }}</li></ol>
        <div class="graph-results">
          <section><h3>关系列表 · {{ result.edges.length }} 条</h3><ul class="graph-edge-list"><li v-for="edge in result.edges" :key="edge.id"><button type="button" :class="{ selected: selectedEdge?.id === edge.id }" @click="selectEdge(edge)">
            {{ name(edge.source) }} {{ symmetric.includes(edge.type) ? '↔' : '→' }} {{ name(edge.target) }} · {{ labels[edge.type] || edge.type }}
            <strong v-if="edge.polarity === 'denied'">（否认）</strong><span v-if="edge.statement_mode === 'character_claim'">（人物说法）</span><small>{{ edge.evidence_total }} 组原文依据</small>
          </button></li></ul></section>
          <section class="graph-evidence"><h3>原文证据</h3><p v-if="!selectedEdge">点击关系查看原文。</p><article v-for="group in groups" :key="group.id"><blockquote v-for="(span, i) in group.spans" :key="i"><p>{{ span.quote }}</p><small>{{ span.section_title || span.section_id }} · Ln {{ span.start_line }}–{{ span.end_line }}</small><button class="text-button" @click="readContext(span)">展开原文上下文</button></blockquote></article>
            <button v-if="groups.length < evidenceTotal" class="text-button" :disabled="evidenceBusy" @click="moreEvidence">加载更多证据（{{ groups.length }}/{{ evidenceTotal }}）</button>
            <div v-if="context" class="context-block"><strong>展开原文上下文（可能包含筛选范围外的邻接段）</strong><p v-for="chunk in context.chunks" :key="chunk.chunk_id"><small>Ln {{ chunk.start_line }}–{{ chunk.end_line }}</small>{{ chunk.text }}</p></div>
          </section>
        </div>
      </template>
      <p v-else-if="!loading" class="graph-notice">搜索并选择实体，开始探索。自动抽取可能遗漏或误判，请结合原文核实。</p>
    </template>
  </section>
</template>
