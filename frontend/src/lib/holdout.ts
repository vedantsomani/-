// Types and fetch for GET /api/holdout ("Predict, then reveal").
// Pixel convention of every x/y below: field grid, 0-based, +x WEST, +y NORTH, 6.15 arcsec per pixel.

import { apiFetch } from './api'
import { mjdToDate, type ProperMotion } from './coords'

export interface HoldoutTarget {
  ra: number
  dec: number
  /** Present for fast movers: ra/dec are then the position at pm.epochMjd. */
  pm?: ProperMotion
}

export interface HoldoutRequest extends HoldoutTarget {
  /** Field of view, arcmin (the same field as the viewer). */
  fov: number
  /** Season index to set aside; the server default is the latest. */
  holdout?: number | null
  /** Parallax (mas) to fix in the parallax model; omitted by default (the server then tries Gaia DR3). */
  plx?: number | null
}

/** Query string for /api/holdout; also the hook's change key. */
export function holdoutQuery(r: HoldoutRequest): string {
  const q = new URLSearchParams({ ra: r.ra.toFixed(6), dec: r.dec.toFixed(6), fov: String(r.fov) })
  if (r.pm) {
    q.set('pmra', String(r.pm.pmra))
    q.set('pmdec', String(r.pm.pmdec))
    q.set('epoch', String(r.pm.epochMjd))
  }
  if (r.holdout != null) q.set('holdout', String(r.holdout))
  if (r.plx != null) q.set('plx', String(r.plx))
  return q.toString()
}

/** 1-sigma error ellipse: semi-axes in arcsec, major-axis angle in degrees east of north. */
export interface HoldoutEllipse {
  a_arcsec: number
  b_arcsec: number
  pa_deg: number
}

export interface HoldoutSeason {
  season: number
  role: 'train' | 'holdout'
  mjd_mean: number
  n_frames: number
  detectors: number[]
}

export interface HoldoutMeasurement {
  group: string
  season: number
  detector: number
  status: string
  used_in_fit: boolean
  reason?: string | null
  ra?: number | null
  dec?: number | null
  x?: number | null
  y?: number | null
  mjd_mean?: number | null
  sigma_east_arcsec?: number | null
  sigma_north_arcsec?: number | null
  ellipse?: HoldoutEllipse | null
  snr?: number | null
  flags?: unknown
}

export interface HoldoutFit {
  ref_mjd: number
  ra: number
  dec: number
  x: number
  y: number
  /** Proper motion; pmra includes cos(dec), positive = east. */
  pmra_masyr: number
  pmdec_masyr: number
  sigma_pmra_masyr?: number | null
  sigma_pmdec_masyr?: number | null
  pm_total_masyr: number
  chi2_red?: number | null
  n_measurements: number
  n_seasons: number
  /** Training seasons that entered the fit. */
  seasons?: number[]
  static_model?: { x: number; y: number; chi2_red?: number | null }
}

export interface HoldoutPrediction {
  mjd: number
  ra: number
  dec: number
  x: number
  y: number
  ellipse: HoldoutEllipse
  extrapolation_yr: number
}

export interface HoldoutNoMotion {
  offset_arcsec: number
  offset_sigma: number
}

export interface HoldoutDetectorRow {
  detector: number
  offset_arcsec: number
  offset_east_arcsec: number
  offset_north_arcsec: number
  offset_sigma: number
  no_motion: HoldoutNoMotion
}

export interface HoldoutCombined {
  offset_arcsec: number
  offset_east_arcsec: number
  offset_north_arcsec: number
  offset_sigma: number
  no_motion: HoldoutNoMotion
}

/** "linear" = straight line; "linear+parallax" = straight line plus a FIXED parallax term. */
export type HoldoutModelName = 'linear' | 'linear+parallax'

export interface HoldoutComparison {
  model?: HoldoutModelName
  per_detector: HoldoutDetectorRow[]
  combined: HoldoutCombined | null
  /** "prediction confirmed" | "marginal" | "prediction failed" | "not testable" */
  verdict: string
  no_motion_verdict?: string
  summary?: string
}

export interface HoldoutModel {
  model: HoldoutModelName
  parallax_mas?: number | null
  pmra_masyr: number
  pmdec_masyr: number
  prediction: HoldoutPrediction
  comparison?: Pick<HoldoutComparison, 'combined' | 'verdict' | 'no_motion_verdict' | 'summary'>
}

export interface HoldoutParallax {
  /** Parallax fixed in the parallax model, mas (null: no value available, straight line only). */
  used_mas: number | null
  used_err_mas?: number | null
  source: 'query' | 'gaia' | null
  factors: { group: string; role: 'train' | 'holdout'; mjd: number; f_east: number; f_north: number }[]
  /** Parallax the straight-line model's held-out residual alone would imply: uses the reveal, NOT a fit. */
  implied_mas: number | null
  implied_err_mas: number | null
  implied_label?: string
  gaia?: { matched: boolean; gaia_id?: string; reason?: string } | null
  free_fit?: { fitted: boolean; reason?: string; parallax_mas?: number; parallax_err_mas?: number }
}

export interface HoldoutComplete {
  status: 'complete'
  testable: boolean
  /** Why the test cannot be run (when testable is false). */
  reason?: string | null
  holdout_season?: number
  seasons?: HoldoutSeason[]
  train?: HoldoutMeasurement[]
  reveal?: HoldoutMeasurement[]
  fit?: HoldoutFit
  prediction?: HoldoutPrediction
  comparison?: HoldoutComparison
  parallax?: HoldoutParallax
  models?: { linear: HoldoutModel; parallax: HoldoutModel | null }
  notes?: string[]
  method?: Record<string, string>
  grid?: { pixscale_arcsec?: number }
}

/** A complete response that carries everything the three steps need. */
export interface HoldoutTestable extends HoldoutComplete {
  testable: true
  holdout_season: number
  fit: HoldoutFit
  prediction: HoldoutPrediction
  comparison: HoldoutComparison
}

export interface HoldoutProcessing {
  status: 'processing'
  stage?: string
  done?: number
  total?: number
}

export interface HoldoutFailed {
  status: 'error'
  error?: string
}

export type HoldoutResponse = HoldoutComplete | HoldoutProcessing | HoldoutFailed

export const isTestable = (r: HoldoutComplete): r is HoldoutTestable =>
  r.testable === true && r.fit != null && r.prediction != null && r.comparison != null && r.holdout_season != null

export const DEFAULT_PIXSCALE_ARCSEC = 6.15

export async function fetchHoldout(query: string, signal?: AbortSignal): Promise<HoldoutResponse> {
  const r = await apiFetch(`/api/holdout?${query}`, { signal })
  if (!r.ok) {
    if (r.status === 404) throw new Error('The prediction test is not available on this server yet.')
    let detail = r.statusText
    try {
      const body = (await r.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* not JSON */
    }
    throw new Error(detail || `HTTP ${r.status}`)
  }
  return (await r.json()) as HoldoutResponse
}

// ---- small formatting helpers -------------------------------------------------------------

/** "Nov 2025" for an MJD. */
export const monthYear = (mjd: number) =>
  mjdToDate(mjd).toLocaleDateString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' })

/** "+0.123" / "−0.045" with a real minus sign. */
export const signed = (v: number, digits: number) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(digits)}`

/** [1, 2, 3] -> "1, 2 and 3". */
export function joinAnd(items: string[]): string {
  if (items.length <= 1) return items.join('')
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

/** Seasons are 0-based indices in the API and 1-based for people. */
export const seasonName = (season: number) => `Season ${season + 1}`
