import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { api } from '../api'
import { applyRunEvent, emptyRunView } from '../runState'
import type { ConversationTurn, RunEvent } from '../types'

export function useRun(onTerminal?: () => void | Promise<void>) {
  const view = reactive(emptyRunView())
  const runId = ref<string | null>(null)
  let source: EventSource | null = null

  const isRunning = computed(() => ['queued', 'running', 'stopping'].includes(view.status))

  function replaceView(next: ReturnType<typeof emptyRunView>) {
    Object.assign(view, next)
  }

  function closeSource() {
    source?.close()
    source = null
  }

  function connect(
    activeRunId: string,
    afterSequence = 0,
    historicalTerminalSequence = 0,
  ) {
    source = new EventSource(
      `/api/runs/${encodeURIComponent(activeRunId)}/events?after=${afterSequence}`,
    )
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as RunEvent
      Object.assign(view, applyRunEvent({ ...view }, event))
      if (
        event.sequence > historicalTerminalSequence
        && ['complete', 'cancelled', 'error'].includes(event.type)
      ) {
        closeSource()
        void onTerminal?.()
      }
    }
    source.onerror = () => {
      if (!['completed', 'cancelled', 'failed'].includes(view.status)) {
        view.status = 'failed'
        view.error = '执行进度连接已断开，请确认后端服务仍在运行。'
      }
      closeSource()
    }
  }

  async function start(novelId: string, question: string, maxSteps: number) {
    closeSource()
    replaceView({ ...emptyRunView(), status: 'queued' })
    const created = await api.createRun(novelId, question, maxSteps)
    runId.value = created.run_id
    connect(created.run_id)
  }

  async function startConversation(
    conversationId: string,
    question: string,
    maxSteps: number,
  ) {
    closeSource()
    replaceView({ ...emptyRunView(), status: 'queued' })
    const created = await api.createConversationRun(conversationId, question, maxSteps)
    runId.value = created.run_id
    connect(created.run_id)
    return created
  }

  function restore(turn: ConversationTurn | null) {
    closeSource()
    runId.value = turn?.run_id ?? null
    if (!turn) {
      replaceView(emptyRunView())
      return
    }
    let restored = emptyRunView()
    for (const event of turn.events) restored = applyRunEvent(restored, event)
    restored.status = turn.status
    restored.error = turn.error
    if (Object.keys(turn.snapshot).length) {
      restored.snapshot = turn.snapshot as NonNullable<typeof restored.snapshot>
    }
    replaceView(restored)
    if (['queued', 'running', 'stopping'].includes(turn.status)) {
      const sequence = turn.events[turn.events.length - 1]?.sequence ?? 0
      connect(turn.run_id, sequence, sequence)
    }
  }

  async function retry() {
    if (!runId.value || view.status !== 'failed') return
    closeSource()
    const previous = { ...view }
    const historicalTerminalSequence = view.events[view.events.length - 1]?.sequence ?? 0
    view.status = 'queued'
    view.error = null
    try {
      await api.retryRun(runId.value)
      replaceView({ ...emptyRunView(), status: 'queued' })
      connect(runId.value, historicalTerminalSequence, historicalTerminalSequence)
    } catch (error) {
      replaceView(previous)
      view.error = error instanceof Error ? error.message : '从失败节点重试失败。'
    }
  }

  async function stop() {
    if (!runId.value || !isRunning.value) return
    view.status = 'stopping'
    try {
      await api.cancelRun(runId.value)
    } catch (error) {
      view.status = 'failed'
      view.error = error instanceof Error ? error.message : '停止任务失败。'
    }
  }

  onBeforeUnmount(closeSource)
  return {
    view,
    runId,
    isRunning,
    start,
    startConversation,
    restore,
    retry,
    stop,
  }
}
