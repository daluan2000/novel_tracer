<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../api'
import { useRun } from '../composables/useRun'
import { timelineText } from '../timeline'
import type { ConfigStatus, Evidence, NovelChunk, NovelInfo } from '../types'
import FlowGraph from './FlowGraph.vue'
import RetrievalObservability from './RetrievalObservability.vue'

const props = defineProps<{ novel: NovelInfo; config: ConfigStatus | null }>()
const emit = defineEmits<{ 'running-change': [running: boolean] }>()
const question = ref('')
const maxSteps = ref(props.config?.default_max_steps ?? 16)
const localError = ref('')
const contexts = reactive<Record<string, NovelChunk[]>>({})
const openEvidence = ref<string | null>(null)
const { view, isRunning, start, stop } = useRun()
const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
const statusNames: Record<string, string> = {
  idle: '尚未开始', queued: '等待开始', running: '正在分析', stopping: '正在停止',
  completed: '分析完成', cancelled: '已停止', failed: '执行失败',
}
const taskStatusNames: Record<string, string> = {
  pending: '待处理', in_progress: '进行中', completed: '已完成', blocked: '暂时受阻',
}

function nodeName(value: string) {
  return value[0].toUpperCase() + value.slice(1)
}

watch(isRunning, (value) => emit('running-change', value), { immediate: true })
watch(() => props.novel.novel_id, () => {
  question.value = ''
  openEvidence.value = null
})

const answerHtml = computed(() => markdown.render(view.snapshot?.final_answer || ''))
const progress = computed(() => {
  const snapshot = view.snapshot
  if (!snapshot?.max_steps) return 0
  return Math.min(100, Math.round((snapshot.step_count / snapshot.max_steps) * 100))
})
const tokenUsage = computed(() => view.snapshot?.metrics.token_usage)
const tokenUsageByNode = computed(() => Object.entries(tokenUsage.value?.by_node ?? {}))
const runAnomalies = computed(() => {
  const items: Array<{ key: string; level: string; code: string; message: string; timestamp?: string }> = []
  for (const event of view.events) {
    const code = event.detail.code || event.detail.diagnostic_code
    if (!code && event.type !== 'error') continue
    items.push({
      key: `event-${event.sequence}`,
      level: event.detail.level || (event.type === 'error' ? 'error' : 'warning'),
      code: code || 'run_error',
      message: event.detail.label || event.error || '任务执行出现异常。',
      timestamp: event.timestamp,
    })
  }
  const snapshot = view.snapshot
  if (snapshot?.termination_reason && ['max_steps_reached', 'repeated_tool_call'].includes(snapshot.termination_reason)
      && !items.some((item) => item.code === snapshot.termination_reason)) {
    items.push({
      key: `termination-${snapshot.termination_reason}`,
      level: 'warning',
      code: snapshot.termination_reason,
      message: snapshot.termination_reason === 'max_steps_reached'
        ? '达到最大调查步数，结果可能不完整。'
        : '检测到重复工具调用，调查已提前结束。',
    })
  }
  for (const task of snapshot?.plan ?? []) {
    if (task.status === 'blocked') items.push({
      key: `blocked-${task.task_id}`, level: 'warning', code: 'task_blocked',
      message: `${task.task_id} 无法继续：${task.description}`,
    })
  }
  for (const [index, limitation] of (snapshot?.limitations ?? []).entries()) {
    items.push({ key: `limitation-${index}`, level: 'warning', code: 'answer_limitation', message: limitation })
  }
  for (const [key, message] of [['local', localError.value], ['stream', view.error]] as const) {
    if (message && !items.some((item) => item.message === message)) {
      items.push({ key: `${key}-error`, level: 'error', code: `${key}_operation_failed`, message })
    }
  }
  return items.reverse()
})

function formatTime(value?: string) {
  if (!value) return ''
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString('zh-CN', { hour12: false })
}

async function begin() {
  localError.value = ''
  if (!question.value.trim()) {
    localError.value = '请先写下要调查的问题。'
    return
  }
  if (!props.config?.ready) {
    localError.value = '模型配置尚未就绪，请检查后端 .env。'
    return
  }
  try {
    await start(props.novel.novel_id, question.value.trim(), maxSteps.value)
  } catch (cause) {
    localError.value = cause instanceof Error ? cause.message : '任务启动失败。'
  }
}

async function toggleEvidence(evidence: Evidence) {
  if (openEvidence.value === evidence.evidence_id) {
    openEvidence.value = null
    return
  }
  openEvidence.value = evidence.evidence_id
  if (!contexts[evidence.evidence_id]) {
    try {
      contexts[evidence.evidence_id] = await api.context(props.novel.novel_id, evidence.chunk_id)
    } catch (cause) {
      localError.value = cause instanceof Error ? cause.message : '证据上下文加载失败。'
    }
  }
}
</script>

<template>
  <section class="workspace-panel agent-panel">
    <div class="question-box">
      <label class="field grow"><span>想了解什么</span><textarea v-model="question" rows="3" placeholder="例如：分析人物关系如何变化，并给出关键阶段、原文依据和反面证据。" :disabled="isRunning" /></label>
      <label class="field step-field"><span>最多调查步数</span><input v-model.number="maxSteps" type="number" min="1" max="100" :disabled="isRunning" /></label>
      <button v-if="!isRunning" class="button primary start-button" @click="begin">开始分析 <span aria-hidden="true">→</span></button>
      <button v-else class="button danger start-button" :disabled="view.status === 'stopping'" @click="stop">{{ view.status === 'stopping' ? '正在结束当前步骤…' : '停止分析' }}</button>
    </div>
    <p v-if="localError || view.error" class="form-error" role="alert">{{ localError || view.error }}</p>

    <div class="run-header">
      <div><p class="eyebrow">实时分析</p><h2>调查进展</h2></div>
      <div class="run-status" :class="`status-${view.status}`"><span></span>{{ view.status }} · {{ statusNames[view.status] }}</div>
    </div>
    <div class="progress-track" role="progressbar" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${progress}%` }"></span></div>

    <div class="investigation-grid">
      <div class="flow-card">
        <FlowGraph :active-node="view.activeNode" :visited-nodes="view.visitedNodes" :failed="view.status === 'failed'" />
        <div v-if="view.snapshot?.plan.length" class="plan-list">
          <p class="mini-title">分析计划</p>
          <div v-for="task in view.snapshot.plan" :key="task.task_id" class="plan-row" :class="`task-${task.status}`">
            <span>{{ task.task_id }}</span><p>{{ task.description }}</p><small>{{ taskStatusNames[task.status] }}</small>
          </div>
        </div>
        <div v-else class="flow-prompt">开始分析后，这里会显示当前步骤和后续计划。</div>
      </div>
      <aside class="timeline-card" aria-live="polite">
        <div class="timeline-title"><strong>执行记录</strong><span>{{ view.events.length }} events</span></div>
        <ol v-if="view.events.length" class="timeline">
          <li v-for="event in view.events" :key="event.sequence" :class="{ terminal: ['complete', 'cancelled', 'error'].includes(event.type) }">
            <span class="timeline-dot"></span>
            <div><small>#{{ event.sequence }} · {{ event.node ? nodeName(event.node) : event.type }}</small><p>{{ timelineText(event) }}</p></div>
          </li>
        </ol>
        <div v-else class="timeline-empty">尚无执行记录</div>
      </aside>
    </div>

    <div v-if="view.snapshot || runAnomalies.length" class="observability-grid">
      <section class="observability-card model-usage-card">
        <div class="observability-heading"><div><p class="mini-title">模型用量</p><small>每次模型调用完成后更新</small></div><span>{{ view.snapshot?.metrics.model_call_count ?? 0 }} calls</span></div>
        <dl class="usage-grid">
          <div><dt>输入 Token</dt><dd>{{ (tokenUsage?.input_tokens ?? 0).toLocaleString() }}</dd></div>
          <div><dt>输出 Token</dt><dd>{{ (tokenUsage?.output_tokens ?? 0).toLocaleString() }}</dd></div>
          <div><dt>推理 Token</dt><dd>{{ (tokenUsage?.reasoning_tokens ?? 0).toLocaleString() }}</dd></div>
          <div><dt>缓存 Token</dt><dd>{{ (tokenUsage?.cached_tokens ?? 0).toLocaleString() }}</dd></div>
          <div><dt>总 Token</dt><dd>{{ (tokenUsage?.total_tokens ?? 0).toLocaleString() }}</dd></div>
          <div><dt>模型耗时</dt><dd>{{ (tokenUsage?.elapsed_seconds ?? 0).toFixed(2) }} 秒</dd></div>
          <div><dt>已统计用量的调用</dt><dd>{{ tokenUsage?.reported_call_count ?? 0 }}</dd></div>
          <div><dt>未返回用量的调用</dt><dd>{{ tokenUsage?.unknown_call_count ?? 0 }}</dd></div>
          <div><dt>调查步数</dt><dd>{{ view.snapshot?.metrics.step_count ?? 0 }}</dd></div>
          <div><dt>工具调用</dt><dd>{{ view.snapshot?.metrics.tool_call_count ?? 0 }}</dd></div>
          <div><dt>有效证据</dt><dd>{{ view.snapshot?.metrics.evidence_count ?? 0 }}</dd></div>
          <div><dt>任务覆盖</dt><dd>{{ Math.round((view.snapshot?.metrics.evidence_coverage ?? 0) * 100) }}%</dd></div>
          <div><dt>格式重试</dt><dd>{{ view.snapshot?.metrics.structured_retry_count ?? 0 }}</dd></div>
          <div><dt>兼容解析</dt><dd>{{ view.snapshot?.metrics.content_fallback_count ?? 0 }}</dd></div>
        </dl>
        <div v-if="tokenUsageByNode.length" class="usage-table-wrap">
          <table><thead><tr><th>节点</th><th>输入</th><th>输出</th><th>总计</th><th>耗时</th></tr></thead><tbody>
            <tr v-for="([node, usage]) in tokenUsageByNode" :key="node"><td>{{ nodeName(node) }}</td><td>{{ usage.input_tokens ?? 0 }}</td><td>{{ usage.output_tokens ?? 0 }}</td><td>{{ usage.total_tokens ?? 0 }}</td><td>{{ Number(usage.elapsed_seconds ?? 0).toFixed(2) }} 秒</td></tr>
          </tbody></table>
        </div>
      </section>
      <section class="observability-card anomaly-card">
        <div class="observability-heading"><div><p class="mini-title">分析过程中的异常</p><small>包括格式重试、兼容处理、执行失败和提前结束</small></div><span>{{ runAnomalies.length }} 条</span></div>
        <ol v-if="runAnomalies.length" class="anomaly-list">
          <li v-for="item in runAnomalies" :key="item.key" :class="`level-${item.level}`">
            <div><strong>{{ item.message }}</strong><time>{{ formatTime(item.timestamp) }}</time></div><small>{{ item.code }}</small>
          </li>
        </ol>
        <p v-else class="no-anomaly">分析过程正常，暂未发现异常。</p>
      </section>
    </div>

    <RetrievalObservability :novel-id="novel.novel_id" :polling="isRunning" />

    <div v-if="view.snapshot?.final_answer" class="answer-layout answer-only">
      <article class="answer-card">
        <p class="eyebrow">综合结论</p><h2>分析结果</h2>
        <div class="markdown-body" v-html="answerHtml"></div>
        <div v-if="view.snapshot.limitations.length" class="notice"><strong>需要注意</strong><ul><li v-for="item in view.snapshot.limitations" :key="item">{{ item }}</li></ul></div>
      </article>
    </div>

    <div v-if="view.snapshot?.evidence.length" class="evidence-section">
      <div class="section-heading"><div><p class="eyebrow">已经核验</p><h2>原文依据</h2></div><span>{{ view.snapshot.evidence.length }} 条</span></div>
      <div class="evidence-grid">
        <article v-for="evidence in view.snapshot.evidence" :key="evidence.evidence_id" class="evidence-card" :class="{ opposing: !evidence.supports }">
          <div class="evidence-top"><span>{{ evidence.supports ? '支持结论' : '反面证据' }}</span><small>{{ evidence.evidence_id }} · Ln {{ evidence.start_line }}–{{ evidence.end_line }}</small></div>
          <h3>{{ evidence.claim }}</h3><blockquote>“{{ evidence.quote }}”</blockquote><p>{{ evidence.interpretation }}</p>
          <button class="text-button" @click="toggleEvidence(evidence)">{{ openEvidence === evidence.evidence_id ? '收起原文' : '展开原文上下文' }}</button>
          <div v-if="openEvidence === evidence.evidence_id" class="context-block">
            <p v-for="chunk in contexts[evidence.evidence_id]" :key="chunk.chunk_id" :class="{ focused: chunk.chunk_id === evidence.chunk_id }"><span>Ln {{ chunk.start_line }}–{{ chunk.end_line }}</span>{{ chunk.text }}</p>
          </div>
        </article>
      </div>
    </div>
  </section>
</template>
