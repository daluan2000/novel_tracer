import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { api } from '../api'
import { applyRunEvent, emptyRunView } from '../runState'
import type { RunEvent } from '../types'

export function useRun() {
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

  async function start(novelId: string, question: string, maxSteps: number) {
    closeSource()
    replaceView({ ...emptyRunView(), status: 'queued' })
    const created = await api.createRun(novelId, question, maxSteps)
    runId.value = created.run_id
    source = new EventSource(`/api/runs/${encodeURIComponent(created.run_id)}/events`)
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as RunEvent
      Object.assign(view, applyRunEvent({ ...view }, event))
      if (['complete', 'cancelled', 'error'].includes(event.type)) closeSource()
    }
    source.onerror = () => {
      if (!['completed', 'cancelled', 'failed'].includes(view.status)) {
        view.status = 'failed'
        view.error = '执行进度连接已断开，请确认后端服务仍在运行。'
      }
      closeSource()
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
  return { view, runId, isRunning, start, stop }
}
