<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import type { RetrievalStatus } from '../types'

const props = withDefaults(defineProps<{ novelId: string; polling?: boolean; refreshKey?: number; dashboard?: boolean }>(), {
  polling: false,
  refreshKey: 0,
  dashboard: false,
})

const status = ref<RetrievalStatus | null>(null)
const statusError = ref('')
const activeGroup = ref('index')
let generation = 0, disposed = false
let timer: ReturnType<typeof setTimeout> | null = null
const statusNames: Record<string, string> = {
  lexical_ready: '关键词检索可用',
  building: '正在构建语义索引',
  hybrid_ready: '混合检索可用',
  degraded: '已回退到关键词检索',
}

const modeLabel = computed(() => status.value?.active_mode === 'hybrid' ? '混合检索' : '仅关键词检索')
const cacheLabel = computed(() => {
  const hit = status.value?.metrics.index_cache_hit
  return hit === null || hit === undefined ? '尚未检查' : hit ? '命中' : '未命中'
})

function schedule() {
  if (timer) clearTimeout(timer)
  if (status.value?.status === 'building' || props.polling) {
    timer = setTimeout(refresh, 1500)
  }
}

async function refresh() {
  if (!props.novelId || disposed) return
  const current = ++generation
  try {
    const next = await api.retrievalStatus(props.novelId)
    if (current !== generation || disposed) return
    status.value = next
    statusError.value = ''
  } catch (cause) {
    if (current !== generation || disposed) return
    statusError.value = cause instanceof Error ? cause.message : '检索状态读取失败。'
  } finally {
    if (current === generation && !disposed) schedule()
  }
}

function formatTime(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString('zh-CN', { hour12: false })
}

watch(
  [() => props.novelId, () => props.polling, () => props.refreshKey],
  ([novelId], [oldNovelId]) => {
    if (novelId !== oldNovelId) status.value = null
    void refresh()
  },
  { immediate: true },
)
onBeforeUnmount(() => { disposed = true; generation++; if (timer) clearTimeout(timer) })
const indexMetrics = computed(() => {
  const m = status.value?.metrics
  return m ? [['索引编码请求', m.document_request_count], ['已编码片段', m.document_text_count.toLocaleString()], ['索引输入字符', m.document_input_characters.toLocaleString()], ['索引缓存', cacheLabel.value]] : []
})
const queryMetrics = computed(() => {
  const m = status.value?.metrics
  return m ? [['查询编码请求', m.query_request_count], ['查询输入字符', m.query_input_characters.toLocaleString()], ['查询缓存命中', m.query_cache_hit_count], ['最近请求耗时', m.last_request_elapsed_seconds === null ? '—' : `${m.last_request_elapsed_seconds.toFixed(2)} 秒`]] : []
})
defineExpose({ refresh })
</script>

<template>
  <section class="observability-card retrieval-observability" :class="{ dashboard }" aria-live="polite">
    <div class="observability-heading"><div><h2>检索运行状态</h2><p class="muted-caption">语义检索用量与运行状态</p></div><span v-if="status" class="mode-chip" :class="`mode-${status.active_mode}`">{{ modeLabel }}</span></div>
    <p v-if="statusError" class="inline-anomaly level-error" role="alert">{{ statusError }}</p>
    <template v-if="status">
      <dl class="retrieval-overview"><div><dt>当前状态</dt><dd>{{ statusNames[status.status] }}</dd></div><div><dt>可检索片段</dt><dd>{{ status.passage_count.toLocaleString() }}</dd></div><div><dt>查询请求</dt><dd>{{ status.metrics.query_request_count }}</dd></div><div><dt>异常 / 回退</dt><dd>{{ status.metrics.failed_request_count }} / {{ status.metrics.fallback_count }}</dd></div></dl>
      <div v-if="status.embedding_enabled" class="embedding-progress" aria-live="polite">
        <div>
          <strong>{{ status.status === 'building' ? '语义索引构建中' : status.status === 'hybrid_ready' ? '语义索引已就绪' : '语义索引暂不可用' }}</strong>
          <span>{{ status.embedding_progress.percentage }}% · {{ status.embedding_progress.completed.toLocaleString() }}/{{ status.embedding_progress.total.toLocaleString() }} 个片段</span>
        </div>
        <div class="embedding-progress-track" role="progressbar" aria-label="语义索引构建进度" :aria-valuenow="status.embedding_progress.percentage" aria-valuemin="0" aria-valuemax="100">
          <span :style="{ width: `${status.embedding_progress.percentage}%` }"></span>
        </div>
      </div>


      <nav class="segmented-tabs" aria-label="检索统计"><button v-for="item in [{id:'index',label:'索引用量'},{id:'query',label:'查询统计'},{id:'events',label:'异常记录'}]" :key="item.id" :aria-current="activeGroup === item.id ? 'page' : undefined" @click="activeGroup = item.id">{{ item.label }}<span v-if="item.id === 'events' && status.events.length">{{ status.events.length }}</span></button></nav>
      <div class="retrieval-detail"><div v-show="activeGroup !== 'events'"><dl class="usage-grid"><div v-for="[label, value] in activeGroup === 'index' ? indexMetrics : queryMetrics" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl><p v-if="activeGroup === 'index'" class="muted-caption">语义编码模型：{{ status.embedding_model || '未配置' }}</p><p class="muted-caption">服务商未返回准确 Token 用量时，按请求次数、文本数和字符数统计。</p></div><div v-show="activeGroup === 'events'">      <div class="anomaly-block">
        <div class="anomaly-title"><strong>异常与回退记录</strong><span>最近 {{ status.events.length }} 条</span></div>
        <ol v-if="status.events.length" class="anomaly-list">
          <li v-for="(event, index) in [...status.events].reverse()" :key="`${event.timestamp}-${event.code}-${index}`" :class="`level-${event.level}`">
            <div><strong>{{ event.message }}</strong><time>{{ formatTime(event.timestamp) }}</time></div>
            <small>{{ event.code }}<template v-if="event.fallback"> · 已改用关键词检索</template></small>
          </li>
        </ol>
        <p v-else class="no-anomaly">暂无异常记录。</p>
      </div>
</div></div>
    </template><p v-else-if="!statusError" class="compact-empty">正在读取检索状态…</p>
  </section>
</template>
