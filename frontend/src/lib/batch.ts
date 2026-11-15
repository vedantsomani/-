/** Batch triage: API types, requests, sorting. */
import { apiFetch, apiUrl } from './api'

export type RowStatus = 'queued' | 'building' | 'measuring' | 'complete' | 'error'

export interface BatchRow {
  i: number
  label: string
  input: string
  ra: number | null
  dec: number | null
  pmra_masyr: number | null
  pmdec_masyr: number | null
  status: RowStatus
  error: string | null
  stage?: string | null
  progress?: [number, number]
  field_key: string | null
  n_exposures: number | null
  n_seasons: number | null
  verdict: string | null
  evidence: string | null
  measured_pm_masyr: [number, number] | null
  flux_4p5_ujy: number | null
  flux_err_4p5_ujy: number | null
  snr_4p5: number | null
  ch4_index: number | null
  ch4_sigma: number | null
  class_label: string | null
  /** The classifier reports low | medium | high (rule-based, not a probability); numbers are tolerated. */
  class_confidence: string | number | null
  blend_warning: boolean | null
  thumb_url: string | null
  view_url: string | null
}

export interface BatchData {
  id: string
  created: number
  fov: number
  status: 'running' | 'complete'
  cancelled: boolean
  done: number
  total: number
  rows: BatchRow[]
}

/** Epoch of the positions for rows that carry a proper motion. */
export const EPOCHS = [
  { mjd: 61000, label: 'SPHEREx epoch (2026), as in the examples' },
  { mjd: 51544.5, label: 'J2000 catalogue position' },
]

export const EXAMPLE_LIST = [
  '133.76002 -7.24221 -8123.7 673.2 # WISE 0855',
  "269.44629 4.76788 -801.6 10362.4 # Barnard's Star",
  '331.09642 -56.80058 3981.9 -2466.8 # eps Ind B',
  '76.95653 30.40144 # RW Aur',
  '248.16761 66.75498 # R Dra',
].join('\n')

/** Number of lines that will become rows (blank lines and # comments are skipped). */
export const countTargets = (text: string) =>
  text.split(/\r?\n/).filter((l) => l.trim() !== '' && !l.trim().startsWith('#')).length

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let detail = r.statusText
    try {
      detail = (await r.json()).detail ?? detail
    } catch {
      /* not JSON */
    }
    throw new HttpError(r.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return r.json() as Promise<T>
}

export class HttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export const createBatch = (targets: string, fov: number, epochMjd: number) =>
  apiFetch('/api/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ targets, fov, epoch_mjd: epochMjd }),
  }).then((r) => json<{ id: string; n: number }>(r))

export const fetchBatch = (id: string, signal?: AbortSignal) =>
  apiFetch(`/api/batch/${encodeURIComponent(id)}`, { signal }).then((r) => json<BatchData>(r))

export const cancelBatch = (id: string) =>
  apiFetch(`/api/batch/${encodeURIComponent(id)}`, { method: 'DELETE' }).then((r) => json<BatchData>(r))

export const downloadUrl = (id: string, ext: 'csv' | 'json') => apiUrl(`/api/batch/${encodeURIComponent(id)}.${ext}`)

// ---- sorting ----------------------------------------------------------------------------------

export type SortKey = 'i' | 'label' | 'verdict' | 'class_label' | 'ch4_sigma' | 'flux_4p5_ujy' | 'blend_warning' | 'status'
export interface Sort {
  key: SortKey
  dir: 'asc' | 'desc'
}

/** Strongest first by methane index (sigma), then by 4.5 um S/N. */
export const DEFAULT_SORT: Sort = { key: 'ch4_sigma', dir: 'desc' }

/** Columns that sort high-to-low on the first click. */
const NUMERIC_FIRST_DESC: SortKey[] = ['ch4_sigma', 'flux_4p5_ujy', 'verdict', 'blend_warning']

export const firstDir = (key: SortKey): Sort['dir'] => (NUMERIC_FIRST_DESC.includes(key) ? 'desc' : 'asc')

const VERDICT_RANK: Record<string, number> = {
  mover: 5,
  'possible mover': 4,
  variable: 3,
  'possible variable': 2,
  static: 1,
}
const STATUS_RANK: Record<RowStatus, number> = { error: 0, queued: 1, building: 2, measuring: 3, complete: 4 }

function sortValue(r: BatchRow, key: SortKey): number | string | null {
  switch (key) {
    case 'i':
      return r.i
    case 'label':
      return r.label.toLowerCase()
    case 'verdict':
      return r.verdict ? (VERDICT_RANK[r.verdict] ?? 0) : null
    case 'class_label':
      return r.class_label ? r.class_label.toLowerCase() : null
    case 'ch4_sigma':
      return r.ch4_sigma
    case 'flux_4p5_ujy':
      return r.snr_4p5
    case 'blend_warning':
      return r.blend_warning === null ? null : r.blend_warning ? 1 : 0
    case 'status':
      return STATUS_RANK[r.status]
  }
}

/** Rows with no value for the sort column always go last, whichever way it is sorted. */
export function sortRows(rows: BatchRow[], sort: Sort): BatchRow[] {
  const sign = sort.dir === 'asc' ? 1 : -1
  const cmp = (a: BatchRow, b: BatchRow, key: SortKey): number => {
    const x = sortValue(a, key)
    const y = sortValue(b, key)
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1
    return (x < y ? -1 : x > y ? 1 : 0) * sign
  }
  return [...rows].sort((a, b) => {
    const c = cmp(a, b, sort.key)
    if (c !== 0) return c
    if (sort.key === 'ch4_sigma') {
      const s = cmp(a, b, 'flux_4p5_ujy')
      if (s !== 0) return s
    }
    return a.i - b.i
  })
}

export const fmt = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || !Number.isFinite(v) ? '–' : v.toFixed(digits)

/** Flux with its error in a readable unit (the CSV always has uJy). */
export function fmtFlux(v: number | null, err: number | null): string {
  if (v === null) return '–'
  const [div, unit] = Math.abs(v) >= 1e6 ? [1e6, 'Jy'] : Math.abs(v) >= 1e4 ? [1e3, 'mJy'] : [1, 'µJy']
  return `${fmt(v / div, div === 1 ? 1 : 2)} ± ${fmt(err === null ? null : err / div, div === 1 ? 1 : 2)} ${unit}`
}

export const fmtConfidence = (c: string | number | null) =>
  c === null ? '' : typeof c === 'number' ? `${Math.round(100 * c)}% confidence` : `${c} confidence`
