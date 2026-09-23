<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../api'
import { useRun } from '../composables/useRun'
import type { ConfigStatus, Evidence, NovelChunk, NovelInfo, RunEvent } from '../types'
import FlowGraph from './FlowGraph.vue'

const props = defineProps<{ novel: NovelInfo; config: ConfigStatus | null }>()
const emit = defineEmits<{ 'running-change': [running: boolean] }>()
const question = ref('')
const maxSteps = ref(props.config?.default_max_steps ?? 16)
const localError = ref('')
const contexts = reactive<Record<string, NovelChunk[]>>({})
const openEvidence = ref<string | null>(null)
const { view, isRunning, start, stop } = useRun()
const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })

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

function timelineText(event: RunEvent) {
  const detail = event.detail
  if (detail.tool_calls?.length) return `准备调用 ${detail.tool_calls.map((call) => call.name).join('、')}`
  if (detail.tools?.length) return `已执行 ${detail.tools.join('、')}`
  if (detail.evidence_count !== undefined) return `当前已校验 ${detail.evidence_count} 条证据`
  if (detail.rationale) return detail.rationale
  return detail.label || event.type
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
      <label class="field grow"><span>调查问题</span><textarea v-model="question" rows="3" placeholder="例如：分析人物关系的变化，给出关键阶段、原文依据和反面证据。" :disabled="isRunning" /></label>
      <label class="field step-field"><span>最大步数</span><input v-model.number="maxSteps" type="number" min="1" max="100" :disabled="isRunning" /></label>
      <button v-if="!isRunning" class="button primary start-button" @click="begin">开始调查 <span aria-hidden="true">→</span></button>
      <button v-else class="button danger start-button" :disabled="view.status === 'stopping'" @click="stop">{{ view.status === 'stopping' ? '等待节点结束…' : '停止任务' }}</button>
    </div>
    <p v-if="localError || view.error" class="form-error" role="alert">{{ localError || view.error }}</p>

    <div class="run-header">
      <div><p class="eyebrow">LIVE INVESTIGATION</p><h2>Agent 调查现场</h2></div>
      <div class="run-status" :class="`status-${view.status}`"><span></span>{{ view.status }}</div>
    </div>
    <div class="progress-track" role="progressbar" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${progress}%` }"></span></div>

    <div class="investigation-grid">
      <div class="flow-card">
        <FlowGraph :active-node="view.activeNode" :visited-nodes="view.visitedNodes" :failed="view.status === 'failed'" />
        <div v-if="view.snapshot?.plan.length" class="plan-list">
          <p class="mini-title">调查计划</p>
          <div v-for="task in view.snapshot.plan" :key="task.task_id" class="plan-row" :class="`task-${task.status}`">
            <span>{{ task.task_id }}</span><p>{{ task.description }}</p><small>{{ task.status }}</small>
          </div>
        </div>
        <div v-else class="flow-prompt">开始调查后，这里会实时显示节点流转与任务计划。</div>
      </div>
      <aside class="timeline-card" aria-live="polite">
        <div class="timeline-title"><strong>执行时间线</strong><span>{{ view.events.length }} events</span></div>
        <ol v-if="view.events.length" class="timeline">
          <li v-for="event in view.events" :key="event.sequence" :class="{ terminal: ['complete', 'cancelled', 'error'].includes(event.type) }">
            <span class="timeline-dot"></span>
            <div><small>#{{ event.sequence }} · {{ event.node || event.type }}</small><p>{{ timelineText(event) }}</p></div>
          </li>
        </ol>
        <div v-else class="timeline-empty">尚无执行事件</div>
      </aside>
    </div>

    <div v-if="view.snapshot?.final_answer" class="answer-layout">
      <article class="answer-card">
        <p class="eyebrow">FINAL SYNTHESIS</p><h2>证据型解读</h2>
        <div class="markdown-body" v-html="answerHtml"></div>
        <div v-if="view.snapshot.limitations.length" class="notice"><strong>分析限制</strong><ul><li v-for="item in view.snapshot.limitations" :key="item">{{ item }}</li></ul></div>
      </article>
      <aside class="metrics-card">
        <p class="mini-title">运行指标</p>
        <dl>
          <div><dt>调查步数</dt><dd>{{ view.snapshot.metrics.step_count ?? 0 }}</dd></div>
          <div><dt>工具调用</dt><dd>{{ view.snapshot.metrics.tool_call_count ?? 0 }}</dd></div>
          <div><dt>有效证据</dt><dd>{{ view.snapshot.metrics.evidence_count ?? 0 }}</dd></div>
          <div><dt>任务覆盖</dt><dd>{{ Math.round((view.snapshot.metrics.evidence_coverage ?? 0) * 100) }}%</dd></div>
        </dl>
      </aside>
    </div>

    <div v-if="view.snapshot?.evidence.length" class="evidence-section">
      <div class="section-heading"><div><p class="eyebrow">VERIFIED EVIDENCE</p><h2>原文证据</h2></div><span>{{ view.snapshot.evidence.length }} 条</span></div>
      <div class="evidence-grid">
        <article v-for="evidence in view.snapshot.evidence" :key="evidence.evidence_id" class="evidence-card" :class="{ opposing: !evidence.supports }">
          <div class="evidence-top"><span>{{ evidence.supports ? '支持证据' : '反面证据' }}</span><small>{{ evidence.evidence_id }} · Ln {{ evidence.start_line }}–{{ evidence.end_line }}</small></div>
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
