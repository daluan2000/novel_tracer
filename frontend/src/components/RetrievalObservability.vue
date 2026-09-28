<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import type { RetrievalStatus } from '../types'

const props = withDefaults(defineProps<{ novelId: string; polling?: boolean }>(), {
  polling: false,
})

const status = ref<RetrievalStatus | null>(null)
const statusError = ref('')
let timer: ReturnType<typeof setTimeout> | null = null

const modeLabel = computed(() => status.value?.active_mode === 'hybrid' ? '混合检索' : '仅 BM25')
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
  if (!props.novelId) return
  try {
    status.value = await api.retrievalStatus(props.novelId)
    statusError.value = ''
  } catch (cause) {
    statusError.value = cause instanceof Error ? cause.message : '检索状态读取失败。'
  } finally {
    schedule()
  }
}

function formatTime(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString('zh-CN', { hour12: false })
}

watch(
  [() => props.novelId, () => props.polling],
  ([novelId], [oldNovelId]) => {
    if (novelId !== oldNovelId) status.value = null
    void refresh()
  },
  { immediate: true },
)
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
defineExpose({ refresh })
</script>

<template>
  <section class="observability-card retrieval-observability" aria-live="polite">
    <div class="observability-heading">
      <div><p class="mini-title">Embedding 消耗与检索状态</p><small>Embedding 不提供可靠 token 时，以请求、文本和字符数计量</small></div>
      <span v-if="status" class="mode-chip" :class="`mode-${status.active_mode}`">{{ modeLabel }}</span>
    </div>

    <p v-if="statusError" class="inline-anomaly level-error">{{ statusError }}</p>
    <template v-if="status">
      <dl class="usage-grid">
        <div><dt>状态</dt><dd>{{ status.status }}</dd></div>
        <div><dt>模型</dt><dd>{{ status.embedding_model || '未配置' }}</dd></div>
        <div><dt>Passage</dt><dd>{{ status.passage_count.toLocaleString() }}</dd></div>
        <div><dt>索引缓存</dt><dd>{{ cacheLabel }}</dd></div>
        <div><dt>文档请求</dt><dd>{{ status.metrics.document_request_count }}</dd></div>
        <div><dt>编码文本</dt><dd>{{ status.metrics.document_text_count.toLocaleString() }}</dd></div>
        <div><dt>文档字符</dt><dd>{{ status.metrics.document_input_characters.toLocaleString() }}</dd></div>
        <div><dt>查询请求</dt><dd>{{ status.metrics.query_request_count }}</dd></div>
        <div><dt>查询字符</dt><dd>{{ status.metrics.query_input_characters.toLocaleString() }}</dd></div>
        <div><dt>查询缓存命中</dt><dd>{{ status.metrics.query_cache_hit_count }}</dd></div>
        <div><dt>失败请求</dt><dd>{{ status.metrics.failed_request_count }}</dd></div>
        <div><dt>BM25 降级</dt><dd>{{ status.metrics.fallback_count }}</dd></div>
        <div><dt>最近请求耗时</dt><dd>{{ status.metrics.last_request_elapsed_seconds === null ? '—' : `${status.metrics.last_request_elapsed_seconds.toFixed(2)}s` }}</dd></div>
      </dl>

      <div class="anomaly-block">
        <div class="anomaly-title"><strong>异常与降级</strong><span>最近 {{ status.events.length }} 条</span></div>
        <ol v-if="status.events.length" class="anomaly-list">
          <li v-for="(event, index) in [...status.events].reverse()" :key="`${event.timestamp}-${event.code}-${index}`" :class="`level-${event.level}`">
            <div><code>{{ event.code }}</code><time>{{ formatTime(event.timestamp) }}</time></div>
            <p>{{ event.message }}</p>
            <small>{{ event.operation }}<template v-if="event.fallback"> · 降级：{{ event.fallback.toUpperCase() }}</template></small>
          </li>
        </ol>
        <p v-else class="no-anomaly">暂无 Embedding 异常或降级记录。</p>
      </div>
    </template>
    <p v-else-if="!statusError" class="no-anomaly">正在读取检索状态…</p>
  </section>
</template>
