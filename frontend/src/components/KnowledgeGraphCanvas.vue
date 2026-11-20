<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Core, StylesheetStyle } from 'cytoscape'
import type { GraphResult } from '../graphTypes'

const props = defineProps<{ result: GraphResult; resetKey: string; labels: Record<string, string>; symmetric: string[]; rootId?: string; selectedEdgeId?: string }>()
const emit = defineEmits<{ node: [id: string]; edge: [id: string] }>()
const host = ref<HTMLElement | null>(null)
const error = ref('')
const zoom = ref(100)
let cy: Core | undefined
let disposed = false
let lastKey = ''
let observer: ResizeObserver | undefined
const types: Record<string, { label: string; color: string; background: string }> = {
  person: { label: '人物', color: '#5476a5', background: '#eef3fa' },
  place: { label: '地点', color: '#4e8877', background: '#edf6f1' },
  organization: { label: '组织', color: '#a38250', background: '#faf4e9' },
  item: { label: '物品', color: '#9275a6', background: '#f5f0f9' },
  skill: { label: '功法', color: '#b57360', background: '#fbf0eb' },
  concept: { label: '专有概念', color: '#73828a', background: '#f0f3f4' },
}
const legend = computed(() => [...new Set(props.result.nodes.map(node => node.type))].map(type => types[type] || { label: type, color: '#73828a' }))
function fit() {
  if (!cy || !cy.nodes().length) return
  cy.fit(undefined, cy.width() < 500 ? 24 : 48)
  // Keep small results readable without enlarging their text to fill the viewport.
  if (cy.zoom() > 1) cy.zoom(1)
  cy.center()
}
function changeZoom(factor: number) {
  if (!cy) return
  cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
}
function selectEdge() {
  if (!cy) return
  cy.edges().removeClass('active')
  cy.edges().filter(edge => edge.id() === props.selectedEdgeId).addClass('active')
}
function update(reset = false) {
  if (!cy) return
  const positions = new Map(cy.nodes().map(n => [n.id(), { ...n.position() }]))
  const key = JSON.stringify([props.resetKey, props.rootId, props.result.nodes.map(node => node.id).sort(), props.result.edges.map(edge => [edge.id, edge.source, edge.target]).sort()])
  cy.startBatch()
  cy.elements().remove()
  cy.add([
    ...props.result.nodes.map((n, i) => {
      const type = types[n.type] || types.concept!
      const angle = i * 2 * Math.PI / Math.max(props.result.nodes.length, 1)
      return { group: 'nodes' as const, data: { id: n.id, label: n.name, color: type.color, background: type.background, width: Math.min(176, Math.max(104, n.name.length * 14 + 36)) },
        classes: n.id === props.rootId ? 'root' : '', position: positions.get(n.id) || { x: Math.cos(angle) * 240, y: Math.sin(angle) * 240 } }
    }),
    ...props.result.edges.map(e => ({ group: 'edges' as const, data: { id: e.id, source: e.source, target: e.target,
      label: `${e.polarity === 'denied' ? '否认 · ' : ''}${props.labels[e.type] || e.type}${e.statement_mode === 'character_claim' ? '\n（人物说法）' : ''}`,
      arrow: props.symmetric.includes(e.type) ? 'none' : 'triangle' }, classes: e.polarity === 'denied' ? 'denied' : '' })),
  ])
  cy.endBatch()
  if (reset || lastKey !== key) {
    if (cy.nodes().length <= 18) {
      const root = cy.nodes().filter(node => node.id() === props.rootId)[0] || cy.nodes().sort((a, b) => b.degree() - a.degree())[0]
      const distances = new Map<string, number>()
      if (root) cy.elements().bfs({ roots: root, directed: false, visit: (node, _edge, _previous, _index, depth) => { distances.set(node.id(), depth) } })
      const count = cy.nodes().length
      cy.layout({ name: 'concentric', animate: false, fit: false, avoidOverlap: true, nodeDimensionsIncludeLabels: true,
        minNodeSpacing: 90, startAngle: -Math.PI / 2, concentric: node => count - (distances.get(node.id()) ?? count), levelWidth: () => 1 }).run()
    } else {
      cy.layout({ name: 'cose', animate: false, fit: false, randomize: false, nodeDimensionsIncludeLabels: true,
        nodeRepulsion: () => 80000, idealEdgeLength: () => 180, nodeOverlap: 30, componentSpacing: 140, gravity: 0.25 }).run()
    }
    fit()
  }
  lastKey = key
  selectEdge()
}
onMounted(async () => {
  try {
    const { default: cytoscape } = await import('cytoscape')
    if (disposed || !host.value) return
    const style: StylesheetStyle[] = [
      { selector: 'node', style: { label: 'data(label)', shape: 'round-rectangle', 'background-color': 'data(background)',
        'border-color': 'data(color)', 'border-width': 1.5, color: '#293b43', 'font-family': '"Noto Sans SC", system-ui, sans-serif',
        'font-size': 14, 'font-weight': 500, 'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'wrap', 'text-max-width': '148px',
        width: 'data(width)', height: 56, 'overlay-opacity': 0 } },
      { selector: 'node.root', style: { 'border-width': 2.5, 'underlay-color': 'data(color)', 'underlay-opacity': 0.1, 'underlay-padding': 7, 'underlay-shape': 'round-rectangle', 'font-weight': 700 } },
      { selector: 'edge', style: { label: 'data(label)', 'line-color': '#b6c2cc', 'target-arrow-color': '#96a6b4', 'target-arrow-shape': edge => edge.data('arrow'),
        'curve-style': 'bezier', 'control-point-step-size': 64, 'arrow-scale': 0.85, width: 1.5, color: '#667681', 'font-size': 11,
        'font-family': '"Noto Sans SC", system-ui, sans-serif', 'text-wrap': 'wrap', 'text-background-color': '#ffffff',
        'text-background-opacity': 1, 'text-background-padding': '5px', 'text-background-shape': 'roundrectangle',
        'text-border-width': 1, 'text-border-color': '#e5e9ec', 'text-border-opacity': 1, 'text-rotation': 'none', 'text-margin-y': -10, 'overlay-opacity': 0 } },
      { selector: 'edge.denied', style: { 'line-style': 'dashed', 'line-color': '#ba7566', 'target-arrow-color': '#ba7566', color: '#a35c4e' } },
      { selector: 'edge.active', style: { width: 2.5, 'line-color': '#c44b32', 'target-arrow-color': '#c44b32', color: '#a13e2b', 'text-border-color': '#e5b5a9', 'z-index': 10 } },
      { selector: 'node:selected', style: { 'border-width': 3 } },
    ]
    cy = cytoscape({ container: host.value, style, minZoom: 0.1, maxZoom: 2 })
    cy.on('tap', 'node', e => emit('node', e.target.id()))
    cy.on('tap', 'edge', e => emit('edge', e.target.id()))
    cy.on('zoom', () => { zoom.value = Math.round((cy?.zoom() || 1) * 100) })
    cy.on('mouseover', 'node, edge', () => { if (host.value) host.value.style.cursor = 'pointer' })
    cy.on('mouseout', 'node, edge', () => { if (host.value) host.value.style.cursor = '' })
    update(true)
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => { cy?.resize(); fit() })
      observer.observe(host.value)
    }
  } catch { error.value = '关系图加载失败，请使用下方结果列表。' }
})
watch(() => [props.result, props.resetKey, props.rootId, props.labels, props.symmetric], () => update())
watch(() => props.selectedEdgeId, selectEdge)
onBeforeUnmount(() => { disposed = true; observer?.disconnect(); cy?.destroy() })
</script>

<template>
  <div class="graph-canvas-wrap">
    <div class="graph-canvas-toolbar">
      <div class="graph-canvas-title"><strong>关系网络</strong><span>{{ result.nodes.length }} 个实体<span class="graph-count-divider">/</span>{{ result.edges.length }} 条关系</span></div>
      <button type="button" class="graph-tool-button" @click="update(true)"><span aria-hidden="true">↻</span> 重置布局</button>
    </div>
    <p v-if="error" class="graph-canvas-error" role="alert">{{ error }}</p>
    <div ref="host" class="graph-canvas" aria-label="实体关系图；完整内容亦可在下方列表中查看"></div>
    <div class="graph-canvas-footer">
      <div class="graph-legend"><span v-for="type in legend" :key="type.label"><i :style="{ background: type.color }"></i>{{ type.label }}</span><span v-if="rootId" class="graph-root-hint">光晕标记查询实体</span></div>
      <div class="graph-zoom-controls">
        <button type="button" aria-label="缩小关系图" @click="changeZoom(1 / 1.2)">−</button><output aria-label="当前缩放">{{ zoom }}%</output><button type="button" aria-label="放大关系图" @click="changeZoom(1.2)">+</button><button type="button" class="graph-fit-button" @click="fit">适应画布</button>
      </div>
    </div>
    <p class="graph-canvas-help">拖动节点调整位置 · 滚轮缩放 · 点击关系查看原文证据</p>
  </div>
</template>
