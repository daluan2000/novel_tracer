<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { GraphRevision } from '../graphTypes'
const props = defineProps<{ revisions: GraphRevision[]; busy: boolean; blocked: boolean; total: number }>()
const emit = defineEmits<{ more: []; restore: [revision: GraphRevision]; refresh: [] }>()
const selectedVersion = ref('')
const selected = computed(() => props.revisions.find(item => item.version === selectedVersion.value) ?? props.revisions[0])
watch(() => props.revisions, () => { if (!props.revisions.some(item => item.version === selectedVersion.value)) selectedVersion.value = props.revisions[0]?.version || '' })
const operations = { build: '构建图谱', merge: '合并实体', restore: '恢复版本', correction: '跳过本次修正' }
function summary(revision: GraphRevision) {
  if (revision.operation === 'merge') {
    const entities = revision.details.entities as Array<{ name: string }> | undefined
    const source = revision.details.source as { name?: string } | undefined
    const target = revision.details.target as { name?: string } | undefined
    return `${entities?.map(entity => entity.name).join('、') || source?.name || '实体'} → ${target?.name || '合并后实体'}`
  }
  return revision.operation === 'restore' ? `恢复自 ${revision.restored_from?.slice(0, 8)}` : '基于原文构建的图谱快照'
}
</script>
<template>
  <section class="graph-history" aria-label="图谱修改历史">
    <div class="workspace-toolbar"><span class="toolbar-hint">{{ total }} 个版本 · 历史快照始终保留</span><button class="button secondary" :disabled="busy" @click="emit('refresh')">刷新历史</button></div>
    <div class="history-browser"><div class="revision-list"><button v-for="revision in revisions" :key="revision.version" class="revision-item" :class="{ selected: selected?.version === revision.version }" @click="selectedVersion = revision.version"><span class="revision-mark">{{ revision.operation === 'merge' ? '⇢' : revision.operation === 'restore' ? '↶' : '◇' }}</span><span><strong>{{ operations[revision.operation] }}</strong><small>{{ new Date(revision.created_at).toLocaleString() }}</small><code>{{ revision.version.slice(0, 8) }}</code></span><span v-if="revision.current" class="status-pill">当前</span></button><button v-if="revisions.length < total" class="text-button" :disabled="busy" @click="emit('more')">加载更多历史</button><p v-if="!revisions.length" class="compact-empty">{{ busy ? '正在读取版本…' : '暂无修改历史' }}</p></div>
      <article v-if="selected" class="revision-detail"><span class="section-kicker">版本快照 / {{ selected.version.slice(0, 8) }}</span><h2>{{ operations[selected.operation] }}</h2><p>{{ summary(selected) }}</p><dl class="merge-impact"><div><dt>实体</dt><dd>{{ selected.entity_count }}</dd></div><div><dt>关系</dt><dd>{{ selected.relation_count }}</dd></div></dl><p class="muted-caption">恢复后生成新版本，之后的历史仍会保留，可再次恢复。</p><p v-if="selected.restore_blocked_reason" class="notice">{{ selected.restore_blocked_reason }}</p><span v-if="selected.current" class="status-pill">当前正在使用此版本</span><button v-if="selected.can_restore" class="button secondary" :disabled="blocked" @click="emit('restore', selected)">恢复此版本</button></article>
    </div>
  </section>
</template>
