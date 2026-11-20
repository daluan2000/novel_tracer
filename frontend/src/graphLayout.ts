import type { GraphResult } from './graphTypes'

export interface GraphPosition { x: number; y: number }

export function graphNodeWidth(name: string) {
  const textWidth = [...name].reduce((width, char) => width + (/[^\u0000-\u007f]/.test(char) ? 16 : 8), 0)
  return Math.min(164, Math.max(76, textWidth + 28))
}

/** Deterministic, viewport-shaped spiral. Keep the query at the centre and
 * reserve space for actual node rectangles rather than treating them as points. */
export function layoutGraph(result: GraphResult, viewport: { width: number; height: number }, rootId?: string) {
  const positions = new Map<string, GraphPosition>()
  if (!result.nodes.length) return positions
  const aspect = Math.min(4, Math.max(0.5, viewport.width / Math.max(1, viewport.height)))
  const adjacency = new Map(result.nodes.map(node => [node.id, new Set<string>()]))
  for (const edge of result.edges) {
    adjacency.get(edge.source)?.add(edge.target)
    adjacency.get(edge.target)?.add(edge.source)
  }
  const root = result.nodes.find(node => node.id === rootId)
    ?? [...result.nodes].sort((a, b) => (adjacency.get(b.id)?.size ?? 0) - (adjacency.get(a.id)?.size ?? 0) || a.id.localeCompare(b.id))[0]!
  const distances = new Map([[root.id, 0]])
  const queue = [root.id]
  for (let index = 0; index < queue.length; index++) {
    for (const neighbor of adjacency.get(queue[index]!) ?? []) {
      if (!adjacency.has(neighbor) || distances.has(neighbor)) continue
      distances.set(neighbor, distances.get(queue[index]!)! + 1)
      queue.push(neighbor)
    }
  }
  const nodes = [...result.nodes].sort((a, b) =>
    (distances.get(a.id) ?? Infinity) - (distances.get(b.id) ?? Infinity)
    || (adjacency.get(b.id)?.size ?? 0) - (adjacency.get(a.id)?.size ?? 0)
    || a.type.localeCompare(b.type) || a.id.localeCompare(b.id))
  const gap = nodes.length > 24 ? 42 : 90
  const height = 48
  const averageWidth = nodes.reduce((sum, node) => sum + graphNodeWidth(node.name), 0) / nodes.length
  const spacing = Math.sqrt((averageWidth + gap) * (height + gap) / Math.PI)
  const goldenAngle = Math.PI * (3 - Math.sqrt(5))
  const boxes = nodes.map((node, index) => ({
    id: node.id, width: graphNodeWidth(node.name),
    x: Math.sqrt(index) * Math.cos(index * goldenAngle) * spacing * Math.sqrt(aspect),
    y: Math.sqrt(index) * Math.sin(index * goldenAngle) * spacing / Math.sqrt(aspect),
  }))
  // Resolve rectangle overlaps without letting the hub drift or reintroducing
  // the unconstrained force layout's tall, narrow shape.
  for (let iteration = 0; iteration < 160; iteration++) {
    let collisions = 0
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i]!, b = boxes[j]!
        const dx = b.x - a.x, dy = b.y - a.y
        const overlapX = (a.width + b.width) / 2 + gap - Math.abs(dx)
        const overlapY = height + gap - Math.abs(dy)
        if (overlapX <= 0 || overlapY <= 0) continue
        collisions++
        const axis = overlapX < overlapY ? 'x' : 'y'
        const shift = ((axis === 'x' ? dx : dy) >= 0 ? 1 : -1) * ((axis === 'x' ? overlapX : overlapY) + 0.1)
        if (i === 0) b[axis] += shift
        else { a[axis] -= shift / 2; b[axis] += shift / 2 }
      }
    }
    if (!collisions) break
  }
  if (boxes.length > 1) {
    for (let pass = 0; pass < 6; pass++) {
      const width = Math.max(...boxes.map(box => box.x + box.width / 2)) - Math.min(...boxes.map(box => box.x - box.width / 2))
      const boxHeight = Math.max(...boxes.map(box => box.y)) - Math.min(...boxes.map(box => box.y)) + height
      const ratio = width / boxHeight
      // Only expand: shrinking an axis could undo the guaranteed clearances.
      if (ratio < aspect) boxes.forEach(box => { box.x *= aspect / ratio })
      else boxes.forEach(box => { box.y *= ratio / aspect })
    }
  }
  for (const box of boxes) positions.set(box.id, { x: box.x, y: box.y })
  return positions
}
