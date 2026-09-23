<script setup lang="ts">
const props = defineProps<{
  activeNode: string | null
  visitedNodes: string[]
  failed: boolean
}>()

const nodes = [
  ['planner', 'Planner', '规划'],
  ['researcher', 'Researcher', '调查'],
  ['tools', 'Tools', '检索'],
  ['assessor', 'Assessor', '取证与审查'],
  ['writer', 'Writer', '写作'],
]

function stateOf(id: string) {
  if (id === props.activeNode) return props.failed ? 'failed' : 'active'
  if (props.visitedNodes.includes(id)) return 'done'
  return 'idle'
}
</script>

<template>
  <div class="flow" aria-label="Agent 执行流程">
    <div class="flow-main">
      <template v-for="(node, index) in nodes" :key="node[0]">
        <div class="flow-node" :class="`is-${stateOf(node[0])}`">
          <span class="node-mark" aria-hidden="true">
            {{ stateOf(node[0]) === 'done' ? '✓' : stateOf(node[0]) === 'failed' ? '!' : index + 1 }}
          </span>
          <span><strong>{{ node[1] }}</strong><small>{{ node[2] }}</small></span>
          <span class="sr-only">{{ stateOf(node[0]) }}</span>
        </div>
        <span v-if="index < nodes.length - 1" class="flow-arrow" aria-hidden="true">→</span>
      </template>
    </div>
    <div class="flow-branch">
      <span aria-hidden="true">↳</span>
      <div class="flow-node" :class="`is-${stateOf('replanner')}`">
        <span class="node-mark" aria-hidden="true">{{ stateOf('replanner') === 'done' ? '✓' : 'R' }}</span>
        <span><strong>Replanner</strong><small>重规划后返回调查</small></span>
      </div>
      <span class="branch-copy">证据不足时回到 Researcher</span>
    </div>
  </div>
</template>
