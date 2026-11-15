// Types and fetch for GET /api/explain ("Explain this candidate").
import { apiFetch } from './api'

export type ExplainSectionKey = 'supports' | 'uncertain' | 'next_check' | 'conclusion'

export const EXPLAIN_SECTIONS: readonly { key: ExplainSectionKey; title: string }[] = [
  { key: 'supports', title: 'What supports it' },
  { key: 'uncertain', title: 'What makes it uncertain' },
  { key: 'next_check', title: 'A check you can do next' },
  { key: 'conclusion', title: 'What we can reasonably conclude' },
]

/** The detector / season pair a sentence comes from. Seasons are 0-based indices into `facts.seasons`. */
export interface ExplainRef {
  detector: number
  season_a: number
  season_b: number
  /** id of the matching difference image (e.g. "d6_s1-s2"), when one exists */
  diff_id: string | null
}

export interface ExplainItem {
  text: string
  refs: ExplainRef[]
}

export type ExplainSections = Record<ExplainSectionKey, ExplainItem[]>

/** The compact numeric dict every sentence was built from (also the AI's only input). */
export interface ExplainFacts {
  /** season index -> "Nov 2025" */
  seasons: Record<string, string>
  [key: string]: unknown
}

export interface ExplainAi {
  text: string
  model: string
}

export interface ExplainReady {
  status: 'complete'
  field: string
  cand: string
  kind: string
  evidence: 'strong' | 'moderate' | 'weak'
  sections: ExplainSections
  facts: ExplainFacts
  ai: ExplainAi | null
  ai_available: boolean
  ai_error: string | null
}

export interface ExplainProcessing {
  status: 'processing'
}

export interface ExplainFailed {
  status: 'error'
  error: string
}

export type ExplainResponse = ExplainReady | ExplainProcessing | ExplainFailed

export const isReady = (r: ExplainResponse | null): r is ExplainReady => r !== null && r.status === 'complete'

export async function fetchExplain(
  fieldKey: string,
  cand: string,
  ai: boolean,
  signal?: AbortSignal,
): Promise<ExplainResponse> {
  const q = new URLSearchParams({ key: fieldKey, cand, ai: ai ? '1' : '0' })
  const r = await apiFetch(`/api/explain?${q}`, { signal })
  if (!r.ok) {
    let detail = r.statusText
    try {
      const body = (await r.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* not JSON */
    }
    throw new Error(detail || `HTTP ${r.status}`)
  }
  return (await r.json()) as ExplainResponse
}

export const seasonName = (facts: ExplainFacts, season: number) => facts.seasons[String(season)] ?? `season ${season + 1}`

/** "D6 · Nov 2025 → Apr 2026" */
export const refLabel = (ref: ExplainRef, facts: ExplainFacts) =>
  `D${ref.detector} · ${seasonName(facts, ref.season_a)} → ${seasonName(facts, ref.season_b)}`
