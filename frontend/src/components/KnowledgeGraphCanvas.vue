<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Core, StylesheetStyle } from 'cytoscape'
import type { GraphResult } from '../graphTypes'

const props = defineProps<{ result: GraphResult; resetKey: string; labels: Record<string, string>; symmetric: string[] }>()
const emit = defineEmits<{ node: [id: string]; edge: [id: string] }>()
const host = ref<HTMLElement | null>(null)
const error = ref('')
let cy: Core | undefined
let disposed = false
let lastKey = ''
let observer: ResizeObserver | undefined
const colors: Record<string, string> = { person: '#446caa', place: '#4d8873', organization: '#997342', item: '#9076ae', skill: '#b46852', concept: '#71818c' }
function update(reset = false) {
  if (!cy) return
  const positions = new Map(cy.nodes().map(n => [n.id(), n.position()]))
  cy.elements().remove()
  cy.add([
    ...props.result.nodes.map((n, i) => ({ group: 'nodes' as const, data: { id: n.id, label: n.name, color: colors[n.type] || '#71818c' }, position: positions.get(n.id) || { x: (i % 10) * 100, y: Math.floor(i / 10) * 100 } })),
    ...props.result.edges.map(e => ({ group: 'edges' as const, data: { id: e.id, source: e.source, target: e.target,
      label: `${e.polarity === 'denied' ? '否认·' : ''}${props.labels[e.type] || e.type}${e.statement_mode === 'character_claim' ? '（人物说法）' : ''}`,
      arrow: props.symmetric.includes(e.type) ? 'none' : 'triangle' }, classes: e.polarity === 'denied' ? 'denied' : '' })),
  ])
  if (reset || lastKey !== props.resetKey) {
    cy.layout({ name: 'cose', animate: false, randomize: false, nodeRepulsion: () => 12000, idealEdgeLength: () => 110 }).run()
    cy.fit(undefined, 35)
  }
  lastKey = props.resetKey
}
onMounted(async () => {
  try {
    const { default: cytoscape } = await import('cytoscape')
    if (disposed || !host.value) return
    const style: StylesheetStyle[] = [
      { selector: 'node', style: { label: 'data(label)', 'background-color': 'data(color)', color: '#263238', 'font-size': 12, 'text-valign': 'bottom', 'text-margin-y': 8, width: 30, height: 30 } },
      { selector: 'edge', style: { label: 'data(label)', 'line-color': '#adb6c1', 'target-arrow-color': '#adb6c1', 'target-arrow-shape': edge => edge.data('arrow'), 'curve-style': 'bezier', 'font-size': 10, 'text-background-color': '#faf9f6', 'text-background-opacity': 1, width: 1.5 } },
      { selector: '.denied', style: { 'line-style': 'dashed', 'line-color': '#ba6659' } },
      { selector: ':selected', style: { 'overlay-opacity': 0.15, 'overlay-color': '#436ea0' } },
    ]
    cy = cytoscape({ container: host.value, style, minZoom: 0.15, maxZoom: 3 })
    cy.on('tap', 'node', e => emit('node', e.target.id()))
    cy.on('tap', 'edge', e => emit('edge', e.target.id()))
    update(true)
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => { cy?.resize(); cy?.fit(undefined, 35) })
      observer.observe(host.value)
    }
  } catch { error.value = '关系图加载失败，请使用下方结果列表。' }
})
watch(() => props.result, () => update())
onBeforeUnmount(() => { disposed = true; observer?.disconnect(); cy?.destroy() })
</script>

<template>
  <div class="graph-canvas-wrap">
    <div class="graph-canvas-toolbar"><span>拖动节点 · 滚轮缩放 · 点击关系查看证据</span><button class="text-button" @click="update(true)">重置视图</button></div>
    <p v-if="error" role="alert">{{ error }}</p>
    <div ref="host" class="graph-canvas" aria-label="实体关系图；完整内容亦可在下方列表中查看"></div>
  </div>
</template>
