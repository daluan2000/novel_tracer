export interface ConfigStatus {
  ready: boolean
  model_name: string
  default_max_steps: number
  structured_output_retries: number | null
  error: string | null
}

export interface StructureReport {
  strategy: 'detected_headings' | 'mixed_headings' | 'fallback_chunks'
  confidence: number
  heading_count: number
  detectors_used: string[]
  rejected_candidate_count: number
  fallback_used: boolean
  warnings: string[]
}

export interface NovelInfo {
  novel_id: string
  filename: string
  encoding: string
  character_count: number
  line_count: number
  section_count: number
  chunk_count: number
  structure: StructureReport
  elapsed_seconds: number
}

export interface SectionItem {
  section_id: string
  title: string | null
  detected: boolean
  confidence: number
  start_line: number
  end_line: number
  chunk_count: number
}

export interface SectionPage {
  items: SectionItem[]
  offset: number
  limit: number
  total: number
}

export interface SearchHit {
  chunk_id: string
  section_id: string
  section_title: string | null
  start_line: number
  end_line: number
  score: number
  matched_terms: string[]
  snippet: string
}

export interface NovelChunk {
  chunk_id: string
  section_id: string
  section_title: string | null
  start_line: number
  end_line: number
  text: string
}

export interface PlanTask {
  task_id: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'blocked'
}

export interface Evidence {
  evidence_id: string
  task_id: string
  claim: string
  quote: string
  section_title: string | null
  start_line: number
  end_line: number
  chunk_id: string
  supports: boolean
  interpretation: string
}

export interface RunMetrics {
  final_answer_completed?: boolean
  tool_call_count?: number
  unique_tool_call_ratio?: number
  evidence_count?: number
  counter_evidence_found?: boolean
  evidence_coverage?: number
  replan_count?: number
  resolved_task_count?: number
  step_count?: number
  termination_reason?: string | null
  structured_retry_count?: number
  content_fallback_count?: number
  model_call_count?: number
  token_usage?: {
    input_tokens: number
    output_tokens: number
    total_tokens: number
    reasoning_tokens: number
    cached_tokens: number
    elapsed_seconds: number
    reported_call_count: number
    unknown_call_count: number
    by_node: Record<string, Record<string, number>>
  }
}

export interface RunSnapshot {
  plan: PlanTask[]
  current_task_id: string | null
  evidence: Evidence[]
  hypotheses: Array<Record<string, unknown>>
  unresolved_questions: string[]
  suggested_queries: string[]
  review: Record<string, unknown> | null
  step_count: number
  max_steps: number
  replan_count: number
  termination_reason: string | null
  final_answer: string | null
  limitations: string[]
  metrics: RunMetrics
}

export type RunStatus = 'idle' | 'queued' | 'running' | 'stopping' | 'completed' | 'cancelled' | 'failed'
export type RunEventType = 'status' | 'update' | 'complete' | 'cancelled' | 'error'

export interface RunEvent {
  sequence: number
  timestamp: string
  type: RunEventType
  status: Exclude<RunStatus, 'idle' | 'stopping'>
  node: string | null
  detail: {
    label?: string
    task_count?: number
    tool_calls?: Array<{ name: string; args: Record<string, unknown> }>
    tools?: string[]
    evidence_count?: number
    sufficient?: boolean
    rationale?: string
    replan_count?: number
    answer_ready?: boolean
    diagnostic_code?: 'structured_output_retry' | 'content_json_fallback' | 'structured_output_failed'
    schema?: string
    attempt?: number
    max_attempts?: number
    retry_number?: number
    max_retries?: number
    failure_reason?: string
    code?: string
    retryable?: boolean
  }
  snapshot: RunSnapshot | Record<string, never>
  error: string | null
}
