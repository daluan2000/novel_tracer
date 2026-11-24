<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, graphApi } from '../api'
import GraphMergeFlow from './GraphMergeFlow.vue'
import GraphHistoryPage from './GraphHistoryPage.vue'
import PanelDrawer from './PanelDrawer.vue'
import type { GraphCorrection, GraphMergePreview, GraphMergeRequest, GraphNode, GraphRevision } from '../graphTypes'

const props = withDefaults(defineProps<{ novelId: string; version: string; root: GraphNode | null; running: boolean; pendingCorrections: number; section?: 'entities' | 'history'; active?: boolean }>(), { section: 'entities', active: true })
const entityTab = ref('merge'), step = ref(1)
const emit = defineEmits<{ changed: [] }>()
const busy = ref(false), error = ref(''), success = ref('')
const mergeOpen = ref(false)
const selected = ref<GraphNode[]>([]), query = ref(''), canonicalName = ref('')
const choices = ref<GraphNode[]>([])
const preview = ref<GraphMergePreview | null>(null), replacing = ref<string | undefined>()
const history = ref<GraphRevision[]>([]), historyTotal = ref(0), restoreTarget = ref<GraphRevision | null>(null)
const corrections = ref<GraphCorrection[]>([])
const blocked = computed(() => busy.value || props.running || !props.version)
let epoch = 0, disposed = false
let searchSequence = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
let searchController: AbortController | undefined

function cancelSearch() {
  searchSequence++
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = undefined
  searchController?.abort(); searchController = undefined
}
function scheduleSearch() {
  cancelSearch(); choices.value = []
  if (!props.active || !query.value.trim() || !mergeOpen.value || blocked.value) return
  searchTimer = setTimeout(() => { searchTimer = undefined; void search() }, 300)
}
watch(query, scheduleSearch, { flush: 'sync' })
watch([mergeOpen, () => props.running, () => props.active], () => {
  if (props.active && mergeOpen.value && !props.running) scheduleSearch()
  else { cancelSearch(); choices.value = [] }
}, { flush: 'sync' })

watch(() => [props.novelId, props.version], (_, prior) => {
  epoch++; cancelSearch(); step.value = 1; entityTab.value = 'merge'
  busy.value = false; error.value = ''; mergeOpen.value = false
  selected.value = []; choices.value = []; query.value = ''; canonicalName.value = ''; preview.value = null
  history.value = []; historyTotal.value = 0; restoreTarget.value = null; corrections.value = []; replacing.value = undefined
  if (prior && prior[0] !== props.novelId) success.value = ''
})
onBeforeUnmount(() => { disposed = true; epoch++; cancelSearch() })

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
  cancelSearch()
  mergeOpen.value = true; step.value = 1; entityTab.value = 'merge'; preview.value = null; replacing.value = rule?.id; error.value = ''; success.value = ''
  selected.value = !rule && props.root ? [props.root] : []
  query.value = rule?.entity_names?.[0] || rule?.source_name || ''
  canonicalName.value = rule?.target_name || props.root?.name || ''; choices.value = []
  scheduleSearch()
}
async function search() {
  const text = query.value.trim()
  if (!text || blocked.value) return
  const current = epoch, sequence = ++searchSequence
  const controller = new AbortController()
  searchController = controller
  error.value = ''
  try {
    const page = await graphApi.search(props.novelId, props.version, text,
      selected.value[0] ? { entityTypes: [selected.value[0].type] } : {}, controller.signal)
    if (disposed || current !== epoch || searchSequence !== sequence) return
    choices.value = page.items.filter(node => !selected.value.some(item => item.id === node.id))
  } catch (reason) {
    if (!disposed && current === epoch && searchSequence === sequence) {
      error.value = reason instanceof Error ? reason.message : '搜索失败。'
      if (reason instanceof ApiError && reason.code === 'graph_version_changed') emit('changed')
    }
  } finally { if (searchController === controller) searchController = undefined }
}
function choose(node: GraphNode) {
  if (blocked.value || selected.value.some(item => item.id === node.id)) return
  if (selected.value[0] && selected.value[0].type !== node.type) return
  cancelSearch(); preview.value = null
  selected.value.push(node)
  if (!canonicalName.value.trim()) canonicalName.value = node.name
  choices.value = choices.value.filter(item => item.id !== node.id && item.type === node.type)
}
function remove(node: GraphNode) {
  preview.value = null
  selected.value = selected.value.filter(item => item.id !== node.id)
  scheduleSearch()
}
const mergeBody = computed<GraphMergeRequest | null>(() => {
  if (selected.value.length < 2 || !canonicalName.value.trim() || canonicalName.value.trim().length > 100) return null
  return { version: props.version, entity_ids: selected.value.map(node => node.id), canonical_name: canonicalName.value.trim(),
    ...(replacing.value ? { replaces_correction_id: replacing.value } : {}) }
})
async function previewMerge() {
  if (blocked.value || !mergeBody.value) return
  const body = mergeBody.value
  const signature = JSON.stringify(body)
  await perform(() => graphApi.previewMerge(props.novelId, body), value => { if (JSON.stringify(mergeBody.value) === signature) { preview.value = value; step.value = 3 } })
}
async function merge() {
  if (blocked.value || !preview.value || !mergeBody.value) return
  const body = mergeBody.value
  await perform(() => graphApi.merge(props.novelId, body), changed)
}
async function loadHistory(more = false) {
  if (busy.value) return
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
  if (busy.value || !props.version) return
  await perform(() => graphApi.corrections(props.novelId, props.version), page => {
    corrections.value = page.items.filter(rule => rule.status === 'pending')
  })
}
async function skip(rule: GraphCorrection) {
  if (blocked.value) return
  await perform(() => graphApi.skipCorrection(props.novelId, props.version, rule.id), changed)
}
function rename(value: string) { canonicalName.value = value; preview.value = null; if (step.value === 3) step.value = 2 }
watch([() => props.active, () => props.section, () => props.version], () => {
  if (props.active && props.section === 'history') void loadHistory()
}, { immediate: true })
function showCorrections() { entityTab.value = 'corrections'; void loadCorrections() }
</script>
<template>
  <section v-show="active" class="graph-edits" aria-label="人工修正与修改历史">
    <p v-if="running" class="notice">构建期间暂不能合并或回退，历史仍可查看。</p><p v-if="error" role="alert" class="field-error">{{ error }}</p><p v-if="success" role="status" class="success-notice">{{ success }}</p>
    <template v-if="section === 'entities'">
      <nav class="segmented-tabs entity-tabs" aria-label="实体整理任务"><button :aria-current="entityTab === 'merge' ? 'page' : undefined" @click="entityTab = 'merge'">实体合并</button><button :aria-current="entityTab === 'corrections' ? 'page' : undefined" :disabled="busy || !version" @click="showCorrections">待确认人工修正（{{ pendingCorrections }}）</button><button :aria-current="entityTab === 'mentions' ? 'page' : undefined" :disabled="!version" @click="entityTab = 'mentions'">待确认提及</button></nav>
      <div v-show="entityTab === 'merge'" class="entity-task-body">
        <GraphMergeFlow v-if="mergeOpen" :selected="selected" :choices="choices" :query="query" :canonical-name="canonicalName" :preview="preview" :step="step" :blocked="blocked" :busy="busy" :valid="!!mergeBody" :replacing="!!replacing" @update:query="query = $event" @update:canonical-name="rename" @update:step="step = $event" @choose="choose" @remove="remove" @preview="previewMerge" @merge="merge" @cancel="mergeOpen = false" />
        <div v-else class="entity-intro"><div class="merge-illustration"><span>名称 A</span><span>名称 B</span><i>→</i><strong>同一个身份</strong></div><h2>把不同的名字，整理成同一个人</h2><p>对比同类型实体，确定规范名称，再检查合并影响。<br />每次修改都会保存为新版本，可随时追溯。</p><button type="button" class="button primary" :disabled="blocked" @click="beginMerge()">合并实体</button><p v-if="!version" class="muted-caption">请先构建可用图谱，再进行实体整理。</p></div>
      </div>
      <section v-show="entityTab === 'corrections'" class="graph-corrections"><p class="muted-caption">重建后身份无法明确匹配时，不会强行合并。逐条核对后重新选择或跳过。</p><ul class="correction-grid"><li v-for="rule in corrections" :key="rule.id"><span class="status-pill">待确认</span><h3>{{ rule.source_name }} <span>→</span> {{ rule.target_name }}</h3><p>{{ rule.reason }}</p><div class="toolbar-actions"><button class="button secondary" :disabled="blocked" @click="beginMerge(rule)">重新选择实体</button><button class="text-button" :disabled="blocked" @click="skip(rule)">跳过本次应用</button></div></li></ul><div v-if="!corrections.length" class="compact-empty">{{ busy ? '正在读取…' : '暂无待确认的人工修正' }}</div></section>
      <div v-show="entityTab === 'mentions'" class="entity-mentions"><slot name="unresolved" /></div>
    </template>
    <GraphHistoryPage v-else :revisions="history" :total="historyTotal" :busy="busy" :blocked="blocked" @refresh="loadHistory()" @more="loadHistory(true)" @restore="restoreTarget = $event" />
    <PanelDrawer :open="active && section === 'history' && !!restoreTarget" title="恢复图谱版本" kind="modal" @close="restoreTarget = null"><div v-if="restoreTarget" class="graph-restore-confirm"><p>确认恢复到 <strong>{{ restoreTarget.version.slice(0, 8) }}</strong>？</p><p>图谱和生效的人工修正将恢复到该快照，今后重建以此为准。已有历史不会删除。</p><div class="toolbar-actions"><button class="button secondary" :disabled="busy" @click="restoreTarget = null">取消恢复</button><button class="button primary" :disabled="blocked" @click="restore()">确认恢复并保存版本</button></div></div></PanelDrawer>
  </section>
</template>
