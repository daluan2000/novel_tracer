<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, graphApi } from '../api'
import type { GraphCorrection, GraphMergePreview, GraphMergeRequest, GraphNode, GraphRevision } from '../graphTypes'

const props = defineProps<{ novelId: string; version: string; root: GraphNode | null; running: boolean; pendingCorrections: number }>()
const emit = defineEmits<{ changed: [] }>()
const busy = ref(false), error = ref(''), success = ref('')
const mergeOpen = ref(false), historyOpen = ref(false), correctionsOpen = ref(false)
const first = ref<GraphNode | null>(null), second = ref<GraphNode | null>(null)
const firstQuery = ref(''), secondQuery = ref(''), keep = ref('')
const firstChoices = ref<GraphNode[]>([]), secondChoices = ref<GraphNode[]>([])
const preview = ref<GraphMergePreview | null>(null), replacing = ref<string | undefined>()
const history = ref<GraphRevision[]>([]), historyTotal = ref(0), restoreTarget = ref<GraphRevision | null>(null)
const corrections = ref<GraphCorrection[]>([])
const blocked = computed(() => busy.value || props.running || !props.version)
const operations = { build: '构建图谱', merge: '合并实体', restore: '恢复版本', correction: '跳过本次修正' }
const types: Record<string, string> = { person: '人物', place: '地点', organization: '组织', item: '物品', skill: '功法', concept: '专有概念' }
let epoch = 0, disposed = false
const searchSequence = [0, 0]

watch(() => [props.novelId, props.version], (_, prior) => {
  epoch++; searchSequence[0]!++; searchSequence[1]!++
  busy.value = false; error.value = ''; mergeOpen.value = false; historyOpen.value = false; correctionsOpen.value = false
  first.value = null; second.value = null; firstChoices.value = []; secondChoices.value = []; preview.value = null
  history.value = []; historyTotal.value = 0; restoreTarget.value = null; corrections.value = []; replacing.value = undefined
  if (prior && prior[0] !== props.novelId) success.value = ''
})
onBeforeUnmount(() => { disposed = true; epoch++ })

async function perform<T>(operation: () => Promise<T>, accept: (value: T) => void) {
  const current = epoch
  busy.value = true; error.value = ''
  try {
    const value = await operation()
    if (!disposed && current === epoch) accept(value)
  } catch (reason) {
    if (!disposed && current === epoch) {
      error.value = reason instanceof Error ? reason.message : '图谱修改失败。'
      if (reason instanceof ApiError && reason.code === 'graph_version_changed') emit('changed')
    }
  } finally { if (!disposed && current === epoch) busy.value = false }
}
function changed() {
  success.value = '已保存为新版本，可在修改历史中恢复之前的图谱。'
  mergeOpen.value = false; preview.value = null; restoreTarget.value = null
  emit('changed')
}
function beginMerge(rule?: GraphCorrection) {
  mergeOpen.value = true; preview.value = null; replacing.value = rule?.id; error.value = ''; success.value = ''
  first.value = rule ? null : props.root; second.value = null
  firstQuery.value = rule?.source_name || props.root?.name || ''; secondQuery.value = rule?.target_name || ''
  keep.value = first.value?.id || ''; firstChoices.value = []; secondChoices.value = []
}
function editQuery(slot: 0 | 1) {
  searchSequence[slot]!++; preview.value = null
  if (slot === 0) { searchSequence[1]!++; first.value = null; firstChoices.value = []; second.value = null; secondChoices.value = []; keep.value = '' }
  else { second.value = null; secondChoices.value = [] }
}
async function search(slot: 0 | 1) {
  const query = (slot === 0 ? firstQuery.value : secondQuery.value).trim()
  if (!query) return
  const current = epoch, sequence = ++searchSequence[slot]!
  error.value = ''
  try {
    const page = await graphApi.search(props.novelId, props.version, query,
      slot === 1 && first.value ? { entityTypes: [first.value.type] } : {})
    if (disposed || current !== epoch || searchSequence[slot] !== sequence) return
    if (slot === 0) firstChoices.value = page.items
    else secondChoices.value = page.items.filter(node => node.id !== first.value?.id)
  } catch (reason) {
    if (!disposed && current === epoch && searchSequence[slot] === sequence) {
      error.value = reason instanceof Error ? reason.message : '搜索失败。'
      if (reason instanceof ApiError && reason.code === 'graph_version_changed') emit('changed')
    }
  }
}
function choose(slot: 0 | 1, node: GraphNode) {
  searchSequence[slot]!++; preview.value = null
  if (slot === 0) {
    searchSequence[1]!++
    first.value = node; firstQuery.value = node.name; firstChoices.value = []; keep.value = node.id
    second.value = null; secondChoices.value = []
  } else { second.value = node; secondQuery.value = node.name; secondChoices.value = [] }
}
const mergeBody = computed<GraphMergeRequest | null>(() => {
  if (!first.value || !second.value || first.value.id === second.value.id || first.value.type !== second.value.type) return null
  return { version: props.version, source_id: keep.value === first.value.id ? second.value.id : first.value.id,
    target_id: keep.value === first.value.id ? first.value.id : second.value.id,
    ...(replacing.value ? { replaces_correction_id: replacing.value } : {}) }
})
async function previewMerge() {
  if (blocked.value || !mergeBody.value) return
  const body = mergeBody.value
  await perform(() => graphApi.previewMerge(props.novelId, body), value => { preview.value = value })
}
async function merge() {
  if (blocked.value || !preview.value || !mergeBody.value) return
  const body = mergeBody.value
  await perform(() => graphApi.merge(props.novelId, body), changed)
}
async function loadHistory(more = false) {
  if (busy.value) return
  historyOpen.value = true
  await perform(() => graphApi.history(props.novelId, more ? history.value.length : 0), page => {
    history.value = more ? [...history.value, ...page.items] : page.items; historyTotal.value = page.total
  })
}
async function restore() {
  if (blocked.value || !restoreTarget.value) return
  const target = restoreTarget.value.version
  await perform(() => graphApi.restore(props.novelId, props.version, target), changed)
}
async function loadCorrections() {
  if (busy.value) return
  correctionsOpen.value = true
  await perform(() => graphApi.corrections(props.novelId, props.version), page => {
    corrections.value = page.items.filter(rule => rule.status === 'pending')
  })
}
async function skip(rule: GraphCorrection) {
  if (blocked.value) return
  await perform(() => graphApi.skipCorrection(props.novelId, props.version, rule.id), changed)
}
function summary(revision: GraphRevision) {
  if (revision.operation === 'merge') {
    const source = revision.details.source as { name?: string } | undefined
    const target = revision.details.target as { name?: string } | undefined
    return `${source?.name || '实体'} → ${target?.name || '保留实体'}`
  }
  return revision.operation === 'restore' ? `恢复自 ${revision.restored_from?.slice(0, 8)}` : ''
}
</script>

<template>
  <section class="graph-edits" aria-label="人工修正与修改历史">
    <div class="graph-edit-actions">
      <button type="button" class="button" :disabled="blocked" @click="beginMerge()">合并实体</button>
      <button type="button" class="text-button" :disabled="busy" @click="loadHistory()">查看修改历史</button>
      <button v-if="pendingCorrections" type="button" class="text-button" :disabled="busy" @click="loadCorrections()">待确认人工修正（{{ pendingCorrections }}）</button>
    </div>
    <p v-if="running" class="graph-notice">构建期间暂不能合并或回退，历史仍可查看。</p>
    <p v-if="error" role="alert" class="field-error">{{ error }}</p>
    <p v-if="success" role="status">{{ success }}</p>

    <section v-if="mergeOpen" class="graph-merge-form" aria-label="合并实体表单">
      <h3>{{ replacing ? '重新指定人工合并' : '合并两个实体' }}</h3>
      <p>合并作用于全书，不受当前章节筛选限制。名称、原文提及和关系将一起更新。</p>
      <label class="field"><span>第一个实体</span><input v-model="firstQuery" :disabled="blocked" @input="editQuery(0)" @keydown.enter.prevent="search(0)" /></label>
      <button type="button" class="text-button" :disabled="blocked || !firstQuery.trim()" @click="search(0)">搜索第一个实体</button>
      <ul class="graph-candidates"><li v-for="node in firstChoices" :key="node.id"><button type="button" :disabled="blocked" @click="choose(0, node)">{{ node.name }} · {{ types[node.type] || node.type }} · {{ node.first_mention?.section_title || node.first_mention?.section_id }} {{ node.first_mention?.start_line ? `行 ${node.first_mention.start_line}` : '' }}</button></li></ul>
      <p v-if="first">已选：{{ first.name }}（{{ types[first.type] || first.type }}） · {{ first.aliases.join('、') }}</p>
      <label class="field"><span>第二个实体（同一类型）</span><input v-model="secondQuery" :disabled="blocked || !first" @input="editQuery(1)" @keydown.enter.prevent="search(1)" /></label>
      <button type="button" class="text-button" :disabled="blocked || !first || !secondQuery.trim()" @click="search(1)">搜索第二个实体</button>
      <ul class="graph-candidates"><li v-for="node in secondChoices" :key="node.id"><button type="button" :disabled="blocked" @click="choose(1, node)">{{ node.name }} · {{ node.first_mention?.section_title || node.first_mention?.section_id }} {{ node.first_mention?.start_line ? `行 ${node.first_mention.start_line}` : '' }} · {{ node.aliases.join('、') }}</button></li></ul>
      <p v-if="second">已选：{{ second.name }} · {{ second.aliases.join('、') }}</p>
      <label v-if="first && second" class="field"><span>保留的实体与规范名称</span><select v-model="keep" :disabled="blocked" @change="preview = null"><option :value="first.id">{{ first.name }}</option><option :value="second.id">{{ second.name }}</option></select></label>
      <button type="button" class="button" :disabled="blocked || !mergeBody" @click="previewMerge()">预览合并</button>
      <div v-if="preview" class="graph-merge-preview" role="status">
        <p>{{ preview.source.name }}（{{ preview.source.mention_count }} 处）合入 {{ preview.target.name }}（原 {{ preview.target.mention_count_before }} 处），合并后 {{ preview.target.mention_count }} 处原文提及。</p>
        <p>保留名称：{{ preview.target.name }}；其他名称：{{ preview.target.aliases.join('、') || '无' }}</p>
        <p>全图关系 {{ preview.relation_count_before }} → {{ preview.relation_count_after }} 条；移除 {{ preview.self_relations_removed }} 条自关联，原始事实记录保留。</p>
        <button type="button" class="button primary" :disabled="blocked" @click="merge()">确认合并并保存版本</button>
      </div>
      <button type="button" class="text-button" :disabled="busy" @click="mergeOpen = false">取消合并</button>
    </section>

    <section v-if="historyOpen" class="graph-history" aria-label="图谱修改历史">
      <h3>修改历史</h3><p>构建、人工合并与回退统一保存。恢复历史快照后，之后的记录仍保留，可再次恢复。</p>
      <ol><li v-for="revision in history" :key="revision.version">
        <strong>{{ operations[revision.operation] }}</strong> · {{ new Date(revision.created_at).toLocaleString() }} · {{ revision.version.slice(0, 8) }}
        <span v-if="revision.current">（当前版本）</span><p>{{ summary(revision) }}</p>
        <p>{{ revision.entity_count }} 个实体 · {{ revision.relation_count }} 条关系</p>
        <p v-if="revision.restore_blocked_reason">{{ revision.restore_blocked_reason }}</p>
        <button v-if="revision.can_restore" type="button" class="text-button" :disabled="blocked" @click="restoreTarget = revision">恢复此版本</button>
      </li></ol>
      <button v-if="history.length < historyTotal" type="button" class="text-button" :disabled="busy" @click="loadHistory(true)">加载更多历史</button>
      <div v-if="restoreTarget" class="graph-restore-confirm" role="alert">
        <p>确认恢复到 {{ restoreTarget.version.slice(0, 8) }}？图谱和生效的人工修正将恢复到该快照，今后重建以此为准。已有历史不会删除。</p>
        <button type="button" class="button primary" :disabled="blocked" @click="restore()">确认恢复并保存版本</button>
        <button type="button" class="text-button" :disabled="busy" @click="restoreTarget = null">取消恢复</button>
      </div>
    </section>

    <section v-if="correctionsOpen" class="graph-corrections" aria-label="待确认人工修正">
      <h3>待确认人工修正</h3><p>重建后身份无法明确匹配时，不会强行合并。</p>
      <ul><li v-for="rule in corrections" :key="rule.id"><strong>{{ rule.source_name }} → {{ rule.target_name }}</strong><p>{{ rule.reason }}</p>
        <button type="button" class="text-button" :disabled="blocked" @click="beginMerge(rule)">重新选择实体</button>
        <button type="button" class="text-button" :disabled="blocked" @click="skip(rule)">跳过本次应用</button>
      </li></ul>
    </section>
  </section>
</template>

<style scoped>
.graph-edits { margin: 16px 0; padding: 16px 0; border-block: 1px solid #d9dfd9; }
.graph-edit-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.graph-merge-form, .graph-history, .graph-corrections { margin-top: 18px; }
.graph-merge-form .field { max-width: 520px; }
.graph-merge-preview, .graph-restore-confirm { margin: 14px 0; padding: 14px; border: 1px solid #c7d8cb; background: #f0f5ef; }
.graph-history li, .graph-corrections li { padding: 10px 0; border-bottom: 1px solid #e1e5df; }
</style>
