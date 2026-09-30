<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import type { RetrievalStatus } from '../types'

const props = withDefaults(defineProps<{ novelId: string; polling?: boolean; refreshKey?: number }>(), {
  polling: false,
  refreshKey: 0,
})

const status = ref<RetrievalStatus | null>(null)
const statusError = ref('')
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
  [() => props.novelId, () => props.polling, () => props.refreshKey],
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
      <div><p class="mini-title">语义检索用量与运行状态</p><small>若服务商未返回准确的 Token 用量，则按请求次数、文本数和字符数统计</small></div>
      <span v-if="status" class="mode-chip" :class="`mode-${status.active_mode}`">{{ modeLabel }}</span>
    </div>

    <p v-if="statusError" class="inline-anomaly level-error">{{ statusError }}</p>
    <template v-if="status">
      <dl class="usage-grid">
        <div><dt>检索状态</dt><dd>{{ statusNames[status.status] }}</dd></div>
        <div><dt>语义编码模型</dt><dd>{{ status.embedding_model || '未配置' }}</dd></div>
        <div><dt>检索片段</dt><dd>{{ status.passage_count.toLocaleString() }}</dd></div>
        <div><dt>索引缓存</dt><dd>{{ cacheLabel }}</dd></div>
        <div><dt>索引编码请求</dt><dd>{{ status.metrics.document_request_count }}</dd></div>
        <div><dt>已编码片段</dt><dd>{{ status.metrics.document_text_count.toLocaleString() }}</dd></div>
        <div><dt>索引输入字符</dt><dd>{{ status.metrics.document_input_characters.toLocaleString() }}</dd></div>
        <div><dt>查询编码请求</dt><dd>{{ status.metrics.query_request_count }}</dd></div>
        <div><dt>查询输入字符</dt><dd>{{ status.metrics.query_input_characters.toLocaleString() }}</dd></div>
        <div><dt>查询缓存命中</dt><dd>{{ status.metrics.query_cache_hit_count }}</dd></div>
        <div><dt>编码失败</dt><dd>{{ status.metrics.failed_request_count }}</dd></div>
        <div><dt>改用关键词检索</dt><dd>{{ status.metrics.fallback_count }}</dd></div>
        <div><dt>最近请求耗时</dt><dd>{{ status.metrics.last_request_elapsed_seconds === null ? '—' : `${status.metrics.last_request_elapsed_seconds.toFixed(2)} 秒` }}</dd></div>
      </dl>

      <div v-if="status.embedding_enabled" class="embedding-progress" aria-live="polite">
        <div>
          <strong>{{ status.status === 'building' ? 'Embedding 构建中' : 'Embedding 已构建完成' }}</strong>
          <span>{{ status.embedding_progress.percentage }}% · {{ status.embedding_progress.completed.toLocaleString() }}/{{ status.embedding_progress.total.toLocaleString() }} 个片段</span>
        </div>
        <div class="embedding-progress-track" role="progressbar" aria-label="Embedding 构建进度" :aria-valuenow="status.embedding_progress.percentage" aria-valuemin="0" aria-valuemax="100">
          <span :style="{ width: `${status.embedding_progress.percentage}%` }"></span>
        </div>
      </div>

      <div class="anomaly-block">
        <div class="anomaly-title"><strong>异常与回退记录</strong><span>最近 {{ status.events.length }} 条</span></div>
        <ol v-if="status.events.length" class="anomaly-list">
          <li v-for="(event, index) in [...status.events].reverse()" :key="`${event.timestamp}-${event.code}-${index}`" :class="`level-${event.level}`">
            <div><strong>{{ event.message }}</strong><time>{{ formatTime(event.timestamp) }}</time></div>
            <small>{{ event.code }}<template v-if="event.fallback"> · 已改用关键词检索</template></small>
          </li>
        </ol>
        <p v-else class="no-anomaly">语义检索运行正常，暂未发现异常。</p>
      </div>
    </template>
    <p v-else-if="!statusError" class="no-anomaly">正在读取检索状态…</p>
  </section>
</template>
