import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { ApiError, graphApi } from '../api'
import type { GraphFilters, GraphNode, GraphResult, GraphStatus } from '../graphTypes'

export function useKnowledgeGraph(novelId: Ref<string>) {
  const status = ref<GraphStatus | null>(null)
  const error = ref('')
  const busy = ref(false)
  const loading = ref(false)
  const result = ref<GraphResult | null>(null)
  const root = ref<GraphNode | null>(null)
  const target = ref<GraphNode | null>(null)
  const candidates = ref<GraphNode[]>([])
  const targetCandidates = ref<GraphNode[]>([])
  let generation = 0
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller = new AbortController()
  let querySequence = 0
  const searchSequences: [number, number] = [0, 0]
  const running = computed(() => ['queued', 'building', 'stopping'].includes(status.value?.job?.status || ''))

  function clearGraph() {
    root.value = null; target.value = null; result.value = null
    candidates.value = []; targetCandidates.value = []
    querySequence++
    searchSequences[0]++; searchSequences[1]++
    loading.value = false
  }
  function invalidate() {
    querySequence++; searchSequences[0]++; searchSequences[1]++
    result.value = null; loading.value = false
  }
  async function failure(reason: unknown, epoch: number) {
    if (disposed || epoch !== generation || (reason instanceof Error && reason.name === 'AbortError')) return
    if (reason instanceof ApiError && reason.code === 'graph_version_changed') {
      clearGraph()
      await refresh()
      if (epoch !== generation || disposed) return
    }
    error.value = reason instanceof Error ? reason.message : '图谱操作失败。'
  }
  async function refresh() {
    const epoch = generation
    if (timer) clearTimeout(timer)
    try {
      const next = await graphApi.status(novelId.value, controller.signal)
      if (disposed || epoch !== generation) return
      if (status.value?.active_version !== next.active_version) clearGraph()
      status.value = next
      if (running.value) timer = setTimeout(() => { void refresh() }, 1500)
    } catch (reason) { await failure(reason, epoch) }
  }
  async function build(mode: 'start' | 'resume' | 'rebuild') {
    const epoch = generation
    busy.value = true; error.value = ''
    try {
      await graphApi.build(novelId.value, mode, mode === 'resume' ? status.value?.job?.job_id : undefined)
      if (epoch === generation && !disposed) await refresh()
    } catch (reason) { await failure(reason, epoch) }
    finally { if (epoch === generation && !disposed) busy.value = false }
  }
  async function stop() {
    const epoch = generation
    const jobId = status.value?.job?.job_id
    if (!jobId) return
    busy.value = true
    try { await graphApi.stop(novelId.value, jobId); if (epoch === generation && !disposed) await refresh() }
    catch (reason) { await failure(reason, epoch) }
    finally { if (epoch === generation && !disposed) busy.value = false }
  }
  async function search(q: string, filters: GraphFilters, slot: 0 | 1 = 0) {
    const epoch = generation, sequence = ++searchSequences[slot]
    const output = slot === 0 ? candidates : targetCandidates
    if (!q.trim() || !status.value?.active_version) { output.value = []; return }
    try {
      const response = await graphApi.search(novelId.value, status.value.active_version, q.trim(), filters, controller.signal)
      if (!disposed && epoch === generation && sequence === searchSequences[slot]) output.value = response.items
    } catch (reason) { await failure(reason, epoch) }
  }
  async function query(depth: number, filters: GraphFilters, mode: 'neighbors' | 'paths' = 'neighbors') {
    const epoch = generation, sequence = ++querySequence
    result.value = null; loading.value = false
    if (!root.value || !status.value?.active_version || (mode === 'paths' && !target.value)) return
    const version = status.value.active_version
    loading.value = true; error.value = ''; result.value = null
    try {
      const next = mode === 'paths'
        ? await graphApi.paths(novelId.value, version, root.value.id, target.value!.id, depth, filters, controller.signal)
        : await graphApi.neighbors(novelId.value, version, root.value.id, depth, filters, controller.signal)
      if (!disposed && epoch === generation && sequence === querySequence) result.value = next
    } catch (reason) { await failure(reason, epoch) }
    finally { if (epoch === generation && sequence === querySequence && !disposed) loading.value = false }
  }
  watch(novelId, () => {
    generation++; controller.abort(); controller = new AbortController()
    if (timer) clearTimeout(timer)
    status.value = null; error.value = ''; busy.value = false; clearGraph()
    void refresh()
  }, { immediate: true })
  onBeforeUnmount(() => { disposed = true; generation++; controller.abort(); if (timer) clearTimeout(timer) })
  return { status, error, busy, loading, running, root, target, candidates, targetCandidates, result, refresh, build, stop, search, query, invalidate }
}
