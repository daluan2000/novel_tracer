import type { NovelChunk } from './types'

export interface GraphSpan {
  chunk_id: string; section_id: string; section_title: string | null
  start_char: number; end_char: number; start_line: number; end_line: number; quote: string
}
export interface GraphGroup { id: string; spans: GraphSpan[] }
export interface GraphNode {
  id: string; name: string; type: string; aliases: string[]
  first_mention?: GraphSpan; match_kind?: string
}
export interface GraphEdge {
  id: string; source: string; target: string; type: string
  polarity: 'affirmed' | 'denied'; statement_mode: 'narration' | 'character_claim'
  evidence_total: number; evidence_groups: GraphGroup[]
}
export interface GraphMeta {
  graph_version: string; truncated: boolean; truncation_reason: string | null; search_complete: boolean
}
export interface GraphResult extends GraphMeta {
  nodes: GraphNode[]; edges: GraphEdge[]; paths: Array<{ node_ids: string[]; edge_ids: string[] }>
}
export interface GraphPage<T> extends GraphMeta { items: T[]; offset: number; limit: number; total: number }
export interface GraphJob {
  job_id: string; graph_version: string
  status: 'queued' | 'building' | 'stopping' | 'completed' | 'failed' | 'stopped'
  phase: 'extracting' | 'resolving' | 'materializing' | 'publishing'
  stop_requested: boolean; can_resume: boolean; error_code: string | null
  extraction: { completed: number; total: number; cached?: number }; resolution: { completed: number; total: number }
  entity_count: number; relation_count: number; elapsed_seconds: number
  phase_elapsed_seconds?: Partial<Record<GraphJob['phase'], number>>
  phase_model_seconds?: Partial<Record<GraphJob['phase'], number>>
  usage: { request_count: number; reported_input_tokens: number; reported_output_tokens: number; unknown_request_count: number; failed_request_count: number }
  events: Array<{ code: string; level: string; created_at: string }>
}
export interface GraphStatus {
  available: boolean; active_version: string | null; error_code: string | null
  configured: boolean; model_name: string | null; source_characters: number; estimated_extraction_units: number
  request_timeout_seconds?: number | null; timeout_retries?: number | null; max_output_tokens?: number | null
  extraction_concurrency?: number | null; resolution_concurrency?: number | null; rate_limit_retries?: number | null
  job: GraphJob | null; counts?: { entity_count: number; relation_count: number }
  ontology: { entity_types: string[]; relation_types: Array<{ type: string; label: string; symmetric: boolean }> }
}
export interface GraphFilters { startChapterId?: string; endChapterId?: string; entityTypes?: string[]; relationTypes?: string[] }
export interface GraphContext { span: GraphSpan; chunks: NovelChunk[] }
