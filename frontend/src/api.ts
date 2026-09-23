import type {
  ConfigStatus,
  NovelChunk,
  NovelInfo,
  SearchHit,
  SectionPage,
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message)
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = (await response.json()) as { detail?: string }
      message = body.detail || message
    } catch {
      // Keep the status-based fallback when the response is not JSON.
    }
    throw new ApiError(message, response.status)
  }
  return response.json() as Promise<T>
}

export const api = {
  config: () => request<ConfigStatus>('/api/config'),

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

  search(novelId: string, query: string, topK: number) {
    const params = new URLSearchParams({ q: query, top_k: String(topK) })
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

  cancelRun(runId: string) {
    return request<{ run_id: string; status: string; cancel_requested: boolean }>(
      `/api/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST' },
    )
  },
}
