<script setup lang="ts">
import { computed, ref } from 'vue'
import type { GraphStatus } from '../graphTypes'
import PanelDrawer from './PanelDrawer.vue'
const props = withDefaults(defineProps<{ status: GraphStatus; busy: boolean; running: boolean; active?: boolean }>(), { active: true })
const emit = defineEmits<{ build: [mode: 'start' | 'resume' | 'rebuild']; stop: []; refresh: [] }>()
const tab = ref('usage'), optionsOpen = ref(false)
const typeLabels: Record<string, string> = { person: '人物', place: '地点', organization: '组织', item: '物品', skill: '功法', concept: '专有概念' }
const phases = { extracting: '收集原文提及与事实候选', resolving: '核对人物与事物的身份', cataloging: '建立全书身份目录', linking: '绑定具体提及', auditing: '复核身份与规范名称', materializing: '保存图谱', publishing: '发布版本' }
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
  entity_same_name_reused: '同名唯一候选缺少异人依据，已复用现有实体',
  graph_structured_output_failed: '模型结构化结果重试耗尽，构建已停止',
  structured_output_failed: '模型结果仍未通过结构校验',
  graph_model_timeout_retry: '模型请求超时，正在有限重试',
  graph_model_rate_limit_retry: '模型请求受到限流，正在等待后重试',
  graph_extraction_timeout_split: '超时片段已拆为较小片段',
  graph_extraction_output_split: '模型输出达到上限，已自动缩小片段',
  graph_extraction_compact_fallback: '已切换为精简抽取，使用片段编号引用原文',
  graph_extraction_capacity_split: '实体或关系较密集，已自动拆分片段',
  graph_extraction_capacity_saved: '最小片段实体或关系较密集，已保存所有通过校验的候选',
  graph_build_failed: '构建处理发生异常，请查看后端日志',
  graph_model_output_limit: '模型响应达到输出上限，未形成完整结果',
  graph_model_timeout: '模型请求超时',
  structured_output_retry: '模型返回格式不完整，正在重试',
  entity_identity_ambiguous: '身份依据不足，已保留为独立实体',
  entity_identity_evidence_rejected: '身份线索缺少明确同一身份依据，已拒绝合并',
  entity_identity_conflict_rejected: '身份线索与已明确区分的人物冲突，已拒绝合并',
  graph_resolution_batch_split: '消歧批次超时或输出达到上限，已拆小并保存进度',
  catalog_proposal_rejected: '身份目录提议缺少原文依据或存在身份冲突，相关提及保留待确认',
}
const quality = computed(() => props.status?.job?.quality_stats)
const filterLabels: Record<string, string> = {
  generic_name: '泛化称谓', low_frequency_background: '低频背景实体', identity_evidence: '缺少明确身份依据的线索',
  low_frequency_after_resolution: '归并后仍低频的背景实体',
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
const progressStages = computed(() => {
  const job = props.status.job
  if (!job) return []
  return [
    { label: '原文抽取', progress: job.extraction }, { label: '身份消歧', progress: job.resolution },
    { label: '身份目录', progress: job.catalog }, { label: '提及绑定', progress: job.binding }, { label: '身份复核', progress: job.audit },
  ].filter((item): item is { label: string; progress: { completed: number; total: number } } => !!item.progress && item.progress.total > 0)
})
const stageOrder = ['extracting', 'cataloging', 'linking', 'auditing', 'resolving', 'materializing', 'publishing'] as const
</script>
<template>
  <section class="graph-build-page">
    <div class="build-overview"><div><span class="section-kicker">{{ status.available ? '当前图谱可用' : '从原文建立关系网络' }}</span><h2>{{ status.job ? states[status.job.status] : '准备构建图谱' }}</h2><p>{{ status.counts ? `${status.counts.entity_count} 个实体 · ${status.counts.relation_count} 条关系` : '完成构建后，即可按人物和原文探索关系。' }}</p><p v-if="status.error_code" class="field-error" role="alert">图谱暂不可用（{{ status.error_code }}），仍可使用文本检索。</p></div>
      <div class="build-overview-actions">      <div class="graph-actions">
        <button v-if="running" class="button" :disabled="busy || status.job?.stop_requested" @click="emit('stop')">{{ status.job?.stop_requested ? '正在停止…' : '停止构建' }}</button>
        <template v-else>
          <button v-if="status.job?.can_resume" class="button primary" :disabled="busy" @click="emit('build', 'resume')">继续构建</button>
          <button class="button" :class="{ primary: !status.available }" :disabled="busy || !status.configured || !!status.error_code" @click="emit('build', status.available || status.job ? 'rebuild' : 'start')">{{ busy ? '提交中…' : status.available || status.job ? '重新构建' : '构建图谱' }}</button>
        </template>
        <button class="text-button" :disabled="busy" @click="emit('refresh')">刷新状态</button>
      </div>
<button class="text-button" @click="optionsOpen = true">构建说明与参数</button></div></div>
    <p class="cost-note">构建或重建会将全书正文分批发送给聊天模型，并产生模型用量。重建期间原图谱仍可使用。</p>
    <p v-if="status.job?.error_code" class="notice warning" role="alert">{{ buildErrors[status.job.error_code] || '构建未能完成，已保存进度。请检查模型配置后继续构建。' }}</p>
    <ol v-if="status.job" class="build-stage-track"><li v-for="phase in stageOrder" :key="phase" :class="{ active: status.job.phase === phase, done: status.job.status === 'completed' || stageOrder.indexOf(phase) < stageOrder.indexOf(status.job.phase) }"><i />{{ phases[phase] }}</li></ol>
    <nav class="segmented-tabs" aria-label="构建详情"><button v-for="item in [{id:'usage',label:'进度与用量'},{id:'quality',label:'质量校验'},{id:'diagnostics',label:'异常诊断'}]" :key="item.id" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id">{{ item.label }}</button></nav>
    <div class="build-detail-scroll"><div v-show="tab === 'usage'" class="build-usage"><template v-if="status.job">      <div class="graph-progress" aria-live="polite">
        <dl class="build-metric-grid"><div><dt>模型请求</dt><dd>{{ status.job.usage.request_count.toLocaleString() }}<small> 次</small></dd></div><div><dt>累计耗时</dt><dd>{{ status.job.elapsed_seconds.toFixed(1) }}<small> 秒</small></dd></div><div><dt>已报告输入</dt><dd>{{ status.job.usage.reported_input_tokens.toLocaleString() }}<small> Token</small></dd></div><div><dt>已报告输出</dt><dd>{{ status.job.usage.reported_output_tokens.toLocaleString() }}<small> Token</small></dd></div></dl>
        <div class="build-progress-grid"><div v-for="stage in progressStages" :key="stage.label" class="build-progress-card"><div><strong>{{ stage.label }}</strong><span>{{ stage.progress.completed }} / {{ stage.progress.total }}</span></div><div class="build-progress-track" role="progressbar" :aria-label="stage.label" :aria-valuenow="stage.progress.completed" :aria-valuemax="stage.progress.total" aria-valuemin="0"><span :style="{width: `${Math.min(100, stage.progress.completed / stage.progress.total * 100)}%`}" /></div></div></div>
        <p class="muted-caption">批次可能按文本密度细分；只有发布完成才表示全流程完成。</p>
        <p v-if="status.job.extraction.cached" class="notice">另有 {{ status.job.extraction.cached }} 批成功返回的候选已缓存，继续处理可复用。</p>
        <p v-if="status.job.usage.unknown_request_count" class="notice">{{ status.job.usage.unknown_request_count }} 次请求用量未知。超时或供应商未返回用量的请求无法统计 token，可能已产生消耗；继续构建会复用已完成批次。</p>
        <details class="build-time-details"><summary>阶段耗时明细</summary><div class="phase-time-grid"><div><h3>阶段累计耗时：</h3><p v-if="!status.job.phase_elapsed_seconds || !Object.keys(status.job.phase_elapsed_seconds).length" class="muted-caption">历史阶段耗时未知。</p><dl v-else><div v-for="(seconds, phase) in status.job.phase_elapsed_seconds" :key="phase"><dt>{{ phases[phase] }}</dt><dd>{{ seconds?.toFixed(1) }} 秒</dd></div></dl></div><div><h3>模型请求累计耗时：</h3><dl><div v-for="(seconds, phase) in status.job.phase_model_seconds" :key="phase"><dt>{{ phases[phase] }}</dt><dd>{{ seconds?.toFixed(1) }} 秒</dd></div></dl><p class="muted-caption">并发时可大于实际耗时。</p></div></div></details>
      </div>
</template><div v-else class="compact-empty">还没有构建任务。点击“构建图谱”开始。</div></div><div v-show="tab === 'quality'">      <section v-if="quality" class="build-quality"><h3>实体筛选与身份校验</h3>
        <p>地点、物品、功法及概念默认至少保留 {{ quality.minimum_mentions }} 处已定位提及，低频但有剧情重要性依据的实体仍可保留。人物和组织不按频次删除。</p>
        <ul><li v-for="(count, type) in quality.candidates_before" :key="type">{{ typeLabels[type] || type }}：候选记录 {{ count }} → {{ quality.candidates_after[type] || 0 }}<template v-if="quality.entities_after"> · 最终实体 {{ quality.entities_after[type] || 0 }}</template></li></ul>
        <p>候选记录可包含同一实体的多次提及；频次按原文位置去重，是已抽取提及数。</p>
        <ul><li v-for="(count, reason) in quality.filtered_reasons" :key="reason">{{ filterLabels[reason] || reason }}：过滤 {{ count }} 项</li></ul>
        <p>拒绝身份线索 {{ quality.identity_rejected_count }} 条 · 身份不确定 {{ quality.ambiguous_count }} 次 · 批量消歧请求 {{ quality.batch_request_count }} 次</p>
        <p v-if="quality.mention_count !== undefined">本次构建：已绑定 {{ quality.resolved_mentions }}/{{ quality.mention_count }} 处提及 · 待确认 {{ quality.pending_mentions }} 处 · 待确认事实 {{ quality.pending_facts }} 条。待确认记录不单独生成图节点。</p>
      </section>
<p v-if="!quality" class="compact-empty">完成抽取后，这里会显示实体筛选和身份校验结果。</p></div><div v-show="tab === 'diagnostics'">      <section v-if="status.job?.events.length" class="build-diagnostics"><h3>构建诊断（最近 {{ status.job.events.length }} 条）</h3><small>过滤项是校验后未采纳的候选；本次停止原因见上方提示。</small><ul><li v-for="(event, i) in status.job.events" :key="i">{{ eventLabel(event.code) }}</li></ul></section>
<p v-if="!status.job?.events.length" class="compact-empty">暂无构建诊断记录。</p></div></div>
    <PanelDrawer :open="active && optionsOpen" title="构建说明与参数" @close="optionsOpen = false">      <p>{{ status.model_name || '聊天模型未配置' }} · {{ status.source_characters.toLocaleString() }} 字 · 预计 {{ status.estimated_extraction_units }} 个抽取批次</p>
      <div class="graph-build-options">
      <p v-if="status.max_output_tokens" class="graph-notice">单次输出最多 {{ status.max_output_tokens }} token · 请求超时上限 {{ status.request_timeout_seconds }} 秒。较大片段超时或输出达到上限时自动拆分；其他超时最多额外重试 {{ status.timeout_retries }} 次。</p>
      <p v-if="status.extraction_concurrency" class="graph-notice">抽取最多 {{ status.extraction_concurrency }} 路并发 · 消歧最多 {{ status.resolution_concurrency }} 路并发；限流后最多额外重试 {{ status.rate_limit_retries }} 次。</p>
      <p class="graph-notice">点击构建或重建会将全书正文分批发送给已配置的聊天模型，并产生模型用量。重建期间原图谱仍可使用。</p>
      </div>
</PanelDrawer>
  </section>
</template>
