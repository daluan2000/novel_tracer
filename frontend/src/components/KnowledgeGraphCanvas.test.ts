import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Core, CytoscapeOptions } from 'cytoscape'
import type { GraphResult } from '../graphTypes'
import KnowledgeGraphCanvas from './KnowledgeGraphCanvas.vue'

enableAutoUnmount(afterEach)
const graph = vi.hoisted(() => ({ cy: undefined as Core | undefined }))
beforeEach(() => { graph.cy = undefined })
vi.mock('cytoscape', async importOriginal => {
  const original = await importOriginal<typeof import('cytoscape') | { default: typeof import('cytoscape') }>()
  const cytoscape = typeof original === 'function' ? original : original.default
  return { default: (options: CytoscapeOptions) => {
    const cy = cytoscape({ headless: true, styleEnabled: true, style: options?.style })
    vi.spyOn(cy, 'width').mockReturnValue(1100)
    vi.spyOn(cy, 'height').mockReturnValue(480)
    graph.cy = cy
    return cy
  } }
})

const result: GraphResult = {
  graph_version: 'v1', truncated: false, truncation_reason: null, search_complete: true, paths: [],
  nodes: Array.from({ length: 30 }, (_, i) => ({ id: `n${i}`, name: i ? `人物${i}` : '祥子', type: 'person', aliases: [] })),
  edges: Array.from({ length: 29 }, (_, i) => ({ id: `e${i}`, source: 'n0', target: `n${i + 1}`, type: 'friend', polarity: 'affirmed', statement_mode: 'narration', evidence_total: 3, evidence_groups: [] })),
}

async function mountCanvas() {
  const wrapper = mount(KnowledgeGraphCanvas, { props: { result, resetKey: 'test', labels: { friend: '朋友' }, symmetric: ['friend'], rootId: 'n0' } })
  Object.defineProperties(wrapper.get('.graph-canvas').element, { clientWidth: { value: 1100 }, clientHeight: { value: 480 } })
  await vi.waitFor(() => expect(graph.cy).toBeDefined())
  await flushPromises()
  return wrapper
}

describe('graph readability', () => {
  it('zooms around the cursor only with Ctrl and leaves normal wheel scrolling unblocked', async () => {
    const wrapper = await mountCanvas()
    const cy = graph.cy!
    cy.zoom(0.7)
    const host = wrapper.get('.graph-canvas').element
    const canvas = document.createElement('canvas')
    host.appendChild(canvas)
    const nativeWheelHandler = vi.fn()
    host.addEventListener('wheel', nativeWheelHandler)
    const ordinary = new WheelEvent('wheel', { deltaY: -100, bubbles: true, cancelable: true })
    canvas.dispatchEvent(ordinary)
    expect(cy.zoom()).toBe(0.7)
    expect(ordinary.defaultPrevented).toBe(false)
    expect(nativeWheelHandler).not.toHaveBeenCalled()

    const anchor = { x: 200, y: 120 }
    const before = { x: (anchor.x - cy.pan().x) / cy.zoom(), y: (anchor.y - cy.pan().y) / cy.zoom() }
    const upward = new WheelEvent('wheel', { deltaY: -100, ctrlKey: true, clientX: anchor.x, clientY: anchor.y, bubbles: true, cancelable: true })
    canvas.dispatchEvent(upward)
    expect(upward.defaultPrevented).toBe(true)
    expect(cy.zoom()).toBeGreaterThan(0.7)
    expect((anchor.x - cy.pan().x) / cy.zoom()).toBeCloseTo(before.x)
    expect((anchor.y - cy.pan().y) / cy.zoom()).toBeCloseTo(before.y)
    canvas.dispatchEvent(new WheelEvent('wheel', { deltaY: 100, ctrlKey: true, bubbles: true, cancelable: true }))
    expect(cy.zoom()).toBeCloseTo(0.7)
  })

  it('reveals the hovered relationship without the rest of the dense graph obscuring it', async () => {
    const wrapper = await mountCanvas()
    const cy = graph.cy!
    expect(cy.edges('.hide-label')).toHaveLength(result.edges.length)
    cy.zoom(0.4)
    cy.getElementById('e0').emit('mouseover')
    await flushPromises()
    expect(wrapper.get('.graph-inspector').text()).toContain('祥子 ↔ 人物1')
    expect(wrapper.get('.graph-inspector').text()).toContain('朋友 · 3 组原文依据')
    const focused = cy.getElementById('e0')
    expect(Number.parseFloat(focused.style('font-size')) * cy.zoom()).toBeGreaterThanOrEqual(13)
    expect(Number.parseFloat(focused.style('text-background-padding')) * cy.zoom()).toBeGreaterThanOrEqual(8)
    expect(cy.getElementById('e1').hasClass('faded')).toBe(true)
    focused.emit('mouseout')
    expect(cy.elements('.faded')).toHaveLength(0)
  })

  it('shows all labels at a readable zoom and keeps a selected edge visible in automatic mode', async () => {
    const wrapper = await mountCanvas()
    const cy = graph.cy!
    cy.zoom(0.4)
    await wrapper.get('[aria-pressed]').trigger('click')
    expect(cy.zoom()).toBe(1)
    expect(cy.edges('.hide-label')).toHaveLength(0)
    await wrapper.get('[aria-pressed]').trigger('click')
    await wrapper.setProps({ selectedEdgeId: 'e2' })
    expect(cy.getElementById('e2').hasClass('hovered')).toBe(true)
    expect(cy.getElementById('e2').style('text-opacity')).toBe('1')
    expect(wrapper.get('.graph-inspector').text()).toContain('人物3')
  })
})
