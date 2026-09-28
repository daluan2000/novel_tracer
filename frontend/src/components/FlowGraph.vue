<script setup lang="ts">
const props = defineProps<{
  activeNode: string | null
  visitedNodes: string[]
  failed: boolean
}>()

const nodes = [
  ['planner', 'Planner', '明确问题'],
  ['researcher', 'Researcher', '选择查询词与工具'],
  ['tools', 'Tools', '从小说中提取相关片段'],
  ['assessor', 'Assessor', '判断证据是否可靠、充分'],
  ['writer', 'Writer', '根据证据形成回答'],
]

const stateLabels: Record<string, string> = {
  active: '正在执行', done: '已完成', failed: '执行失败', idle: '尚未执行',
}

function stateOf(id: string) {
  if (id === props.activeNode) return props.failed ? 'failed' : 'active'
  if (props.visitedNodes.includes(id)) return 'done'
  return 'idle'
}
</script>

<template>
  <div class="flow" aria-label="分析流程">
    <div class="flow-main">
      <template v-for="(node, index) in nodes" :key="node[0]">
        <div class="flow-node" :class="`is-${stateOf(node[0])}`">
          <span class="node-mark" aria-hidden="true">
            {{ stateOf(node[0]) === 'done' ? '✓' : stateOf(node[0]) === 'failed' ? '!' : index + 1 }}
          </span>
          <span><strong>{{ node[1] }}</strong><small>{{ node[2] }}</small></span>
          <span class="sr-only">{{ stateLabels[stateOf(node[0])] }}</span>
        </div>
        <span v-if="index < nodes.length - 1" class="flow-arrow" aria-hidden="true">→</span>
      </template>
    </div>
    <div class="flow-branch">
      <span aria-hidden="true">↳</span>
      <div class="flow-node" :class="`is-${stateOf('replanner')}`">
        <span class="node-mark" aria-hidden="true">{{ stateOf('replanner') === 'done' ? '✓' : 'R' }}</span>
        <span><strong>Replanner</strong><small>补充或修正检索方向</small></span>
      </div>
      <span class="branch-copy">证据不足时重新制定检索策略</span>
    </div>
  </div>
</template>
