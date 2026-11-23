import type {
  ConfigStatus,
  Conversation,
  ConversationPage,
  NovelChunk,
  NovelInfo,
  NovelPage,
  RetrievalStatus,
  SearchHit,
  SectionPage,
} from './types'
import type { GraphFilters, GraphGroup, GraphNode, GraphPage, GraphResult, GraphStatus, GraphJob, GraphUnresolved } from './graphTypes'

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(message)
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    let code: string | undefined
    try {
      const body = (await response.json()) as { detail?: unknown; code?: string }
      message = typeof body.detail === 'string' ? body.detail : message
      code = body.code
    } catch {
      // Keep the status-based fallback when the response is not JSON.
    }
    throw new ApiError(message, response.status, code)
  }
  return response.json() as Promise<T>
}

function graphParams(version: string, filters: GraphFilters = {}, extra: Record<string, string> = {}) {
  const params = new URLSearchParams({ version, ...extra })
  if (filters.startChapterId) params.set('startChapterId', filters.startChapterId)
  if (filters.endChapterId) params.set('endChapterId', filters.endChapterId)
  filters.entityTypes?.forEach(type => params.append('entity_type', type))
  filters.relationTypes?.forEach(type => params.append('relation_type', type))
  return params
}
const graphBase = (id: string) => `/api/novels/${encodeURIComponent(id)}/graph`
export const graphApi = {
  unresolved: (id: string, version: string, filters: GraphFilters, offset = 0) => request<GraphPage<GraphUnresolved>>(
    `${graphBase(id)}/unresolved?${graphParams(version, filters, { offset: String(offset) })}`),
  status: (id: string, signal?: AbortSignal) => request<GraphStatus>(`${graphBase(id)}/status`, { signal }),
  build: (id: string, mode: 'start' | 'resume' | 'rebuild', jobId?: string) => request<GraphJob>(`${graphBase(id)}/builds`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode, ...(jobId ? { job_id: jobId } : {}) }),
  }),
  stop: (id: string, jobId: string) => request<GraphJob>(`${graphBase(id)}/builds/${encodeURIComponent(jobId)}/stop`, { method: 'POST' }),
  search: (id: string, version: string, q: string, filters: GraphFilters, signal?: AbortSignal) => request<GraphPage<GraphNode>>(
    `${graphBase(id)}/entities?${graphParams(version, filters, { q, ...(filters.entityTypes?.length === 1 ? { type: filters.entityTypes[0]! } : {}) })}`, { signal }),
  neighbors: (id: string, version: string, entityId: string, depth: number, filters: GraphFilters, signal?: AbortSignal) => request<GraphResult>(
    `${graphBase(id)}/neighbors?${graphParams(version, filters, { entity_id: entityId, depth: String(depth) })}`, { signal }),
  paths: (id: string, version: string, from: string, to: string, depth: number, filters: GraphFilters, signal?: AbortSignal) => request<GraphResult>(
    `${graphBase(id)}/paths?${graphParams(version, filters, { from_id: from, to_id: to, max_hops: String(depth) })}`, { signal }),
  evidence: (id: string, version: string, relationId: string, filters: GraphFilters, offset = 0, signal?: AbortSignal) => request<GraphPage<GraphGroup>>(
    `${graphBase(id)}/relations/${encodeURIComponent(relationId)}/evidence?${graphParams(version, filters, { offset: String(offset) })}`, { signal }),
}

export const api = {
  config: () => request<ConfigStatus>('/api/config'),

  novels: () => request<NovelPage>('/api/novels'),

  uploadNovel(file: File) {
    const form = new FormData()
    form.append('file', file)
    return request<NovelInfo>('/api/novels', { method: 'POST', body: form })
  },

  sections(novelId: string, offset = 0, limit = 50) {
    return request<SectionPage>(
      `/api/novels/${encodeURIComponent(novelId)}/sections?offset=${offset}&limit=${limit}`,
    )
  },

  retrievalStatus(novelId: string) {
    return request<RetrievalStatus>(
      `/api/novels/${encodeURIComponent(novelId)}/retrieval-status`,
    )
  },

  setEmbeddingEnabled(novelId: string, enabled: boolean) {
    return request<RetrievalStatus>(
      `/api/novels/${encodeURIComponent(novelId)}/embedding`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      },
    )
  },

  search(
    novelId: string,
    query: string,
    topK: number,
    range: { startChapterId?: string; endChapterId?: string } = {},
  ) {
    const params = new URLSearchParams({ q: query, top_k: String(topK) })
    if (range.startChapterId) params.set('startChapterId', range.startChapterId)
    if (range.endChapterId) params.set('endChapterId', range.endChapterId)
    return request<SearchHit[]>(`/api/novels/${encodeURIComponent(novelId)}/search?${params}`)
  },

  context(novelId: string, chunkId: string, before = 1, after = 1) {
    const params = new URLSearchParams({ before: String(before), after: String(after) })
    return request<NovelChunk[]>(
      `/api/novels/${encodeURIComponent(novelId)}/chunks/${encodeURIComponent(chunkId)}/context?${params}`,
    )
  },

  createRun(novelId: string, question: string, maxSteps: number) {
    return request<{ run_id: string; status: string }>('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ novel_id: novelId, question, max_steps: maxSteps }),
    })
  },

  conversations(novelId: string) {
    return request<ConversationPage>(
      `/api/novels/${encodeURIComponent(novelId)}/conversations`,
    )
  },

  conversation(conversationId: string) {
    return request<Conversation>(
      `/api/conversations/${encodeURIComponent(conversationId)}`,
    )
  },

  createConversation(novelId: string) {
    return request<Conversation>('/api/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ novel_id: novelId }),
    })
  },

  deleteConversation(conversationId: string) {
    return request<{ conversation_id: string; deleted: boolean }>(
      `/api/conversations/${encodeURIComponent(conversationId)}`,
      { method: 'DELETE' },
    )
  },

  createConversationRun(conversationId: string, question: string, maxSteps: number) {
    return request<{
      conversation_id: string
      turn_id: string
      run_id: string
      status: string
    }>(`/api/conversations/${encodeURIComponent(conversationId)}/runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, max_steps: maxSteps }),
    })
  },

  cancelRun(runId: string) {
    return request<{ run_id: string; status: string; cancel_requested: boolean }>(
      `/api/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST' },
    )
  },

  retryRun(runId: string) {
    return request<{
      run_id: string
      status: string
      failed_node: string
      manual_retry_count: number
    }>(`/api/runs/${encodeURIComponent(runId)}/retry`, { method: 'POST' })
  },
}
