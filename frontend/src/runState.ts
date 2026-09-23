import type { RunEvent, RunSnapshot, RunStatus } from './types'

export interface RunViewState {
  status: RunStatus
  activeNode: string | null
  visitedNodes: string[]
  events: RunEvent[]
  snapshot: RunSnapshot | null
  error: string | null
}

export function emptyRunView(): RunViewState {
  return {
    status: 'idle',
    activeNode: null,
    visitedNodes: [],
    events: [],
    snapshot: null,
    error: null,
  }
}

export function applyRunEvent(current: RunViewState, event: RunEvent): RunViewState {
  const visited = new Set(current.visitedNodes)
  if (current.activeNode && event.node !== current.activeNode) visited.add(current.activeNode)
  if (event.type === 'complete' && current.activeNode) visited.add(current.activeNode)

  const status: RunStatus =
    event.type === 'complete'
      ? 'completed'
      : event.type === 'cancelled'
        ? 'cancelled'
        : event.type === 'error'
          ? 'failed'
          : event.status

  return {
    status,
    activeNode: event.node ?? current.activeNode,
    visitedNodes: [...visited],
    events: [...current.events, event],
    snapshot: Object.keys(event.snapshot).length ? (event.snapshot as RunSnapshot) : current.snapshot,
    error: event.error ?? current.error,
  }
}
