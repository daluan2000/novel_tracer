<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Core, NodeSingular, StylesheetStyle } from 'cytoscape'
import type { GraphResult } from '../graphTypes'
import { graphNodeWidth, layoutGraph } from '../graphLayout'

const props = defineProps<{ result: GraphResult; resetKey: string; labels: Record<string, string>; symmetric: string[]; rootId?: string; selectedEdgeId?: string }>()
const emit = defineEmits<{ node: [id: string]; edge: [id: string]; clear: [] }>()
const host = ref<HTMLElement | null>(null)
const error = ref('')
const zoom = ref(100)
const showAllLabels = ref(false)
const hoveredEdge = ref<string | null>(null)
const hoveredNode = ref<string | null>(null)
const dense = computed(() => props.result.nodes.length > 24 || props.result.edges.length > 32)
const inspectedEdge = computed(() => props.result.edges.find(edge => edge.id === (hoveredEdge.value || props.selectedEdgeId)))
const inspectedNode = computed(() => props.result.nodes.find(node => node.id === hoveredNode.value))
const nodeName = (id: string) => props.result.nodes.find(node => node.id === id)?.name || id
const relationText = computed(() => {
  const edge = inspectedEdge.value
  if (!edge) return ''
  return `${edge.polarity === 'denied' ? '否认 · ' : ''}${props.labels[edge.type] || edge.type}${edge.statement_mode === 'character_claim' ? '（人物说法）' : ''} · ${edge.evidence_total} 组原文依据`
})
let cy: Core | undefined
let disposed = false
let lastKey = ''
let observer: ResizeObserver | undefined
let viewport = { width: 0, height: 0 }
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
function originalSize() {
  if (!cy) return
  cy.zoom(1)
  const root = cy.getElementById(props.rootId || '')
  if (root.length) cy.center(root)
  else cy.center()
}
function refreshLabels() {
  if (!cy) return
  cy.edges().toggleClass('hide-label', !showAllLabels.value && (dense.value || cy.zoom() < 0.7))
  cy.edges('.hovered, .active').style({ 'font-size': Math.max(16, 13 / cy.zoom()), 'text-background-padding': `${8 / cy.zoom()}px` })
}
function clearFocus() {
  if (!cy) return
  cy.elements().removeClass('faded hovered')
  cy.edges().removeStyle('font-size text-background-padding')
}
function focusEdge(id: string) {
  if (!cy) return
  clearFocus()
  const edge = cy.getElementById(id)
  if (!edge.length) return
  cy.elements().addClass('faded')
  edge.removeClass('faded').addClass('hovered').style({ 'font-size': Math.max(16, 13 / cy.zoom()), 'text-background-padding': `${8 / cy.zoom()}px` })
  edge.connectedNodes().removeClass('faded')
}
function arrange() {
  if (!cy || !host.value?.clientWidth || !host.value.clientHeight) return
  viewport = { width: cy.width(), height: cy.height() }
  const padding = cy.width() < 500 ? 24 : 48
  const positions = layoutGraph(props.result, { width: Math.max(1, viewport.width - padding * 2), height: Math.max(1, viewport.height - padding * 2) }, props.rootId)
  cy.nodes().positions(node => positions.get(node.id()) || { x: 0, y: 0 })
  fit()
}
function changeZoom(factor: number) {
  if (!cy) return
  cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
}
function handleWheel(event: WheelEvent) {
  // Handle wheels before Cytoscape so normal scrolling can reach the page's
  // native scroll container without changing the graph or swallowing scroll.
  event.stopImmediatePropagation()
  if (!event.ctrlKey || !cy || !host.value) return
  event.preventDefault()
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? cy.height() : 1
  const delta = Math.max(-200, Math.min(200, event.deltaY * unit))
  const level = Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * Math.exp(-delta * 0.002)))
  const bounds = host.value.getBoundingClientRect()
  cy.zoom({ level, renderedPosition: { x: event.clientX - bounds.left, y: event.clientY - bounds.top } })
}
function selectEdge() {
  if (!cy) return
  cy.edges().removeClass('active')
  cy.edges().filter(edge => edge.id() === props.selectedEdgeId).addClass('active')
  if (props.selectedEdgeId) focusEdge(props.selectedEdgeId)
  else clearFocus()
}
function update(reset = false) {
  if (!cy) return
  const positions = new Map(cy.nodes().map(n => [n.id(), { ...n.position() }]))
  const key = JSON.stringify([props.resetKey, props.rootId, props.result.nodes.map(node => node.id).sort(), props.result.edges.map(edge => [edge.id, edge.source, edge.target]).sort()])
  cy.startBatch()
  cy.elements().remove()
  cy.add([
    ...props.result.nodes.map(n => {
      const type = types[n.type] || types.concept!
      return { group: 'nodes' as const, data: { id: n.id, label: n.name, color: type.color, background: type.background, width: graphNodeWidth(n.name) },
        classes: n.id === props.rootId ? 'root' : '', position: positions.get(n.id) || { x: 0, y: 0 } }
    }),
    ...props.result.edges.map(e => ({ group: 'edges' as const, data: { id: e.id, source: e.source, target: e.target,
      label: `${e.polarity === 'denied' ? '否认 · ' : ''}${props.labels[e.type] || e.type}${e.statement_mode === 'character_claim' ? '\n（人物说法）' : ''}`,
      arrow: props.symmetric.includes(e.type) ? 'none' : 'triangle' }, classes: e.polarity === 'denied' ? 'denied' : '' })),
  ])
  cy.endBatch()
  if (reset || lastKey !== key) arrange()
  refreshLabels()
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
        'font-size': 20, 'font-weight': 500, 'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'ellipsis', 'text-max-width': (node: NodeSingular) => `${Math.min(136, node.data('width') - 12)}px`,
        width: 'data(width)', height: 48, 'overlay-opacity': 0 } },
      { selector: 'node.root', style: { 'border-width': 2.5, 'underlay-color': 'data(color)', 'underlay-opacity': 0.1, 'underlay-padding': 7, 'underlay-shape': 'round-rectangle', 'font-weight': 700 } },
      { selector: 'edge', style: { label: 'data(label)', 'line-color': '#b6c2cc', 'target-arrow-color': '#96a6b4', 'target-arrow-shape': edge => edge.data('arrow'),
        'curve-style': 'bezier', 'control-point-step-size': 42, 'arrow-scale': 0.85, width: 1.3, color: '#3d5260', 'font-size': 14,
        'font-family': '"Noto Sans SC", system-ui, sans-serif', 'text-wrap': 'wrap', 'text-background-color': '#ffffff',
        'text-background-opacity': 1, 'text-background-padding': '5px', 'text-background-shape': 'roundrectangle',
        'text-border-width': 1, 'text-border-color': '#e5e9ec', 'text-border-opacity': 1, 'text-rotation': 'none', 'text-margin-y': -8, 'overlay-opacity': 0, 'text-events': 'yes' } },
      { selector: 'edge.hide-label', style: { 'text-opacity': 0 } },
      { selector: '.faded', style: { opacity: 0.12 } },
      { selector: 'edge.denied', style: { 'line-style': 'dashed', 'line-color': '#ba7566', 'target-arrow-color': '#ba7566', color: '#a35c4e' } },
      { selector: 'edge.active, edge.hovered', style: { width: 2.5, 'line-color': '#39715b', 'target-arrow-color': '#39715b', color: '#214c39', 'text-border-color': '#bfd1c6', 'text-opacity': 1, 'font-size': 16, 'text-margin-y': -14, opacity: 1, 'z-index': 100, 'z-index-compare': 'manual' } },
      { selector: 'node:selected', style: { 'border-width': 3 } },
    ]
    cy = cytoscape({ container: host.value, style, minZoom: 0.15, maxZoom: 3, wheelSensitivity: 0.2 })
    cy.on('tap', 'node', e => emit('node', e.target.id()))
    cy.on('tap', 'edge', e => emit('edge', e.target.id()))
    cy.on('tap', event => { if (event.target === cy) emit('clear') })
    cy.on('zoom', () => { zoom.value = Math.round((cy?.zoom() || 1) * 100); refreshLabels() })
    cy.on('mouseover', 'edge', event => {
      hoveredNode.value = null
      hoveredEdge.value = event.target.id()
      focusEdge(event.target.id())
    })
    cy.on('mouseover', 'node', event => {
      hoveredEdge.value = null
      hoveredNode.value = event.target.id()
      clearFocus()
      cy?.elements().addClass('faded')
      event.target.closedNeighborhood().removeClass('faded')
    })
    cy.on('mouseout', 'node, edge', () => {
      hoveredEdge.value = null
      hoveredNode.value = null
      selectEdge()
    })
    cy.on('mouseover', 'node, edge', () => { if (host.value) host.value.style.cursor = 'pointer' })
    cy.on('mouseout', 'node, edge', () => { if (host.value) host.value.style.cursor = '' })
    update(true)
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => {
        if (!cy || !host.value?.clientWidth || !host.value.clientHeight) return
        cy.resize()
        if (Math.abs(cy.width() - viewport.width) < 2 && Math.abs(cy.height() - viewport.height) < 2) return
        arrange()
      })
      observer.observe(host.value)
    }
  } catch { error.value = '关系图加载失败，请使用关系检查器中的列表。' }
})
watch(() => [props.result, props.resetKey, props.rootId, props.labels, props.symmetric], () => update())
watch(() => props.selectedEdgeId, selectEdge)
watch(showAllLabels, enabled => {
  if (enabled && cy && cy.zoom() < 0.85) originalSize()
  refreshLabels()
})
onBeforeUnmount(() => { disposed = true; observer?.disconnect(); cy?.destroy() })
</script>

<template>
  <div class="graph-canvas-wrap">
    <div class="graph-canvas-toolbar">
      <div class="graph-canvas-title"><strong>关系网络</strong><span>{{ result.nodes.length }} 个实体<span class="graph-count-divider">/</span>{{ result.edges.length }} 条关系</span></div>
      <div class="graph-canvas-actions">
        <button type="button" class="graph-tool-button" :aria-pressed="showAllLabels" @click="showAllLabels = !showAllLabels">{{ showAllLabels ? '关系文字：全部显示' : '关系文字：自动' }}</button>
        <button type="button" class="graph-tool-button" @click="update(true)"><span aria-hidden="true">↻</span> 重置布局</button>
      </div>
    </div>
    <p v-if="error" class="graph-canvas-error" role="alert">{{ error }}</p>
    <div ref="host" class="graph-canvas" aria-label="实体关系图；Ctrl 加滚轮缩放，完整内容亦可在关系检查器中查看" @wheel.capture="handleWheel"></div>
    <div class="graph-canvas-footer">
      <div class="graph-legend"><span v-for="type in legend" :key="type.label"><i :style="{ background: type.color }"></i>{{ type.label }}</span><span v-if="rootId" class="graph-root-hint">光晕标记查询实体</span></div>
      <div class="graph-zoom-controls">
        <button type="button" aria-label="缩小关系图" @click="changeZoom(1 / 1.2)">−</button><output aria-label="当前缩放">{{ zoom }}%</output><button type="button" aria-label="放大关系图" @click="changeZoom(1.2)">+</button><button type="button" class="graph-fit-button" @click="originalSize">原大查看</button><button type="button" class="graph-fit-button" @click="fit">适应画布</button>
      </div>
    </div>
    <div class="graph-inspector" aria-live="polite">
      <template v-if="inspectedNode"><strong>{{ inspectedNode.name }}</strong><span>{{ types[inspectedNode.type]?.label || inspectedNode.type }}<template v-if="inspectedNode.aliases.length"> · 又名 {{ inspectedNode.aliases.join('、') }}</template></span></template>
      <template v-else-if="inspectedEdge"><strong>{{ nodeName(inspectedEdge.source) }} {{ symmetric.includes(inspectedEdge.type) ? '↔' : '→' }} {{ nodeName(inspectedEdge.target) }}</strong><span>{{ relationText }}</span></template>
      <span v-else>{{ dense ? '关系较多，悬停或点选连线可清楚查看关系文字；也可切换为全部显示。' : '悬停查看完整名称与关系，点击连线查看原文证据。' }}</span>
    </div>
    <p class="graph-canvas-help">拖动画布平移 · 拖动节点调整位置 · Ctrl + 滚轮缩放 · 原大查看可按实际字号阅读</p>
  </div>
</template>
