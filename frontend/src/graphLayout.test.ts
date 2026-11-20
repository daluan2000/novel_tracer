import { describe, expect, it } from 'vitest'
import { graphNodeWidth, layoutGraph } from './graphLayout'
import type { GraphResult } from './graphTypes'

function hub(count: number): GraphResult {
  const nodes = Array.from({ length: count }, (_, index) => ({ id: String(index), name: index ? `人物${index}` : '祥子', type: 'person', aliases: [] }))
  return {
    graph_version: 'v1', truncated: false, truncation_reason: null, search_complete: true, paths: [], nodes,
    edges: nodes.slice(1).map(node => ({ id: `e${node.id}`, source: '0', target: node.id, type: 'knows', polarity: 'affirmed', statement_mode: 'narration', evidence_total: 1, evidence_groups: [] })),
  }
}

function bounds(result: GraphResult, positions: ReturnType<typeof layoutGraph>) {
  return {
    width: Math.max(...result.nodes.map(node => positions.get(node.id)!.x + graphNodeWidth(node.name) / 2)) - Math.min(...result.nodes.map(node => positions.get(node.id)!.x - graphNodeWidth(node.name) / 2)),
    height: Math.max(...[...positions.values()].map(point => point.y)) - Math.min(...[...positions.values()].map(point => point.y)) + 48,
  }
}

describe('relationship graph layout', () => {
  it('uses a wide canvas for the 73-node hub instead of producing a tall graph', () => {
    const result = hub(73)
    const positions = layoutGraph(result, { width: 1100, height: 480 }, '0')
    const box = bounds(result, positions)
    expect(box.width / box.height).toBeCloseTo(1100 / 480, 1)
    expect(positions.get('0')).toEqual({ x: 0, y: 0 })
    expect(positions.size).toBe(73)
    for (let i = 0; i < result.nodes.length; i++) {
      for (let j = i + 1; j < result.nodes.length; j++) {
        const a = result.nodes[i]!, b = result.nodes[j]!
        const pa = positions.get(a.id)!, pb = positions.get(b.id)!
        const horizontalGap = Math.abs(pa.x - pb.x) - (graphNodeWidth(a.name) + graphNodeWidth(b.name)) / 2
        const verticalGap = Math.abs(pa.y - pb.y) - 48
        expect(horizontalGap >= 41 || verticalGap >= 41).toBe(true)
      }
    }
  })

  it('adapts to portrait screens and is deterministic across result ordering', () => {
    const result = hub(73)
    const viewport = { width: 360, height: 480 }
    const first = layoutGraph(result, viewport, '0')
    const second = layoutGraph({ ...result, nodes: [...result.nodes].reverse(), edges: [...result.edges].reverse() }, viewport, '0')
    expect(second).toEqual(first)
    const box = bounds(result, first)
    expect(box.width / box.height).toBeCloseTo(0.75, 1)
  })

  it('handles disconnected nodes and long names without modifying the graph', () => {
    const result = hub(20)
    result.nodes[3]!.name = '这是一段非常长的人物或物品名称，用于检查边界'
    result.edges = []
    const original = JSON.stringify(result)
    const positions = layoutGraph(result, { width: 1100, height: 480 })
    expect([...positions.values()].every(point => Number.isFinite(point.x) && Number.isFinite(point.y))).toBe(true)
    expect(JSON.stringify(result)).toBe(original)
    expect(graphNodeWidth(result.nodes[3]!.name)).toBe(164)
    expect(layoutGraph(hub(0), { width: 0, height: 0 }).size).toBe(0)
  })
})
