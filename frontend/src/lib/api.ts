// Typed client for the FastAPI backend (proxied at /api by Vite in dev).
import type { OverlayResponse } from './overlay'

export interface FrameMeta {
  id: string
  obs_id: string
  visit: string
  detector: number
  collection: string
  season: number
  png: string
  source_url: string
  mjd: number
  date: string | null
  exptime_s: number | null
  wavelength_um: number | null
  bandwidth_um: number | null
  coverage: number
  excluded_frac: number
  artifact_frac: Record<string, number>
  target_flags: string[]
  target_quality: 'good' | 'suspect' | 'bad'
  background_mjysr: number
  noise_mjysr: number | null
  zodi_mjysr: number | null
  psf_fwhm: number | null
  l2_qa: string | null
}

export interface StackMeta {
  id: string
  season: number
  detector: number
  /** SPHEREx visit id (e.g. "2025W19_1B_0116") for per-visit stacks; null for season-long stacks */
  visit?: string | null
  n_frames: number
  frame_ids: string[]
  mjd: number
  mjd_min: number
  mjd_max: number
  date: string
  wavelength_um: number | null
  wavelength_min_um: number | null
  wavelength_max_um: number | null
  noise_mjysr: number | null
  depth_gain: number | null
  n_at_target: number
  /** wavelength bin id (e.g. "d6b1") for wavelength-matched stacks; null for whole-detector stacks */
  bin: string | null
  png: string
}

export interface WaveBin {
  id: string
  detector: number
  index: number
  ch_lo: number
  ch_hi: number
  n_channels: number
  lam_lo_um: number
  lam_hi_um: number
  lam_center_um: number
  width_um: number
  resolving_power: number
  counts: Record<string, number>
  seasons: number[]
}

export interface DiffMeta {
  id: string
  detector: number
  bin: string | null
  season_a: number
  season_b: number
  mjd_a: number
  mjd_b: number
  dt_days: number
  wavelength_a_um: number | null
  wavelength_b_um: number | null
  n_bins: number
  noise_scale: number
  png: string
}

export type ChangeKind = 'mover' | 'variable' | 'appeared' | 'vanished' | 'uncertain' | 'static'

export interface EpochMeasure {
  centroid: [number, number, number] | null
  flux_ujy: number | null
  flux_err_ujy: number | null
}

export interface ChangeEvent {
  detector: number
  season_a: number
  season_b: number
  diff?: string
  kind: ChangeKind
  x?: number
  y?: number
  max_z?: number
  snr_a?: number
  snr_b?: number
  flux_ratio?: number
  dflux_sigma?: number
  dx_px?: number
  dy_px?: number
  shift_px?: number
  shift_sigma?: number
  pmra_masyr?: number
  pmdec_masyr?: number
  reason?: string
  method?: string
  /** wavelength bins both seasons share (the comparison uses only these, equally weighted) */
  bins?: string[]
  bins_missing?: string[]
  lam_lo_um?: number
  lam_hi_um?: number
  wavelength_a_um?: number | null
  wavelength_b_um?: number | null
  /** true when the two seasons share no wavelength bin, so nothing could be compared */
  no_comparison?: boolean
  measure?: { a: EpochMeasure; b: EpochMeasure; aperture?: string }
}

export interface Candidate {
  id: string
  kind: ChangeKind
  x: number
  y: number
  ra: number
  dec: number
  max_z: number
  detectors: number[]
  n_events: number
  /** Evidence level from explicit checks. A ranking aid, not a calibrated probability. */
  evidence: EvidenceLevel
  n_independent: number
  n_detectors_agree: number
  checks: EvidenceCheck[]
  pmra_masyr?: number
  pmdec_masyr?: number
  pm_total_masyr?: number
  pm_consistent?: boolean
  flux_ratio_min?: number
  flux_ratio_max?: number
  is_target?: boolean
  reason?: string
  motion_check?: MotionCheck
  events: ChangeEvent[]
}

export type EvidenceLevel = 'strong' | 'moderate' | 'weak'

export interface EvidenceCheck {
  label: string
  ok: boolean
  note?: string
}

export interface MotionCheck {
  n_movers: number
  coherent: boolean | null
  coherence?: number
  speed_scatter?: number
}

export interface FastMover {
  kind: 'fast_mover' | 'stationary_transient' | 'transient'
  visit: string
  mjd: number
  date: string
  rate_arcsec_per_min: number | null
  pa_deg: number | null
  max_z: number
  points: { x: number; y: number; ra: number; dec: number; t: number; z: number; frame_id: string; detector: number }[]
}

export interface Analysis {
  version: string
  bins: WaveBin[]
  diffs: DiffMeta[]
  candidates: Candidate[]
  target: {
    summary: string
    rows: ChangeEvent[]
    pmra_masyr?: number
    pmdec_masyr?: number
    pm_consistent?: boolean
    motion_check?: MotionCheck
    evidence: EvidenceLevel
    n_independent: number
    n_detectors_agree: number
    checks: EvidenceCheck[]
  }
  fast_movers: FastMover[]
}

export interface Season {
  season: number
  start: string
  end: string
  mjd_start: number
  mjd_end: number
  n_frames: number
  detectors: number[]
}

export type FieldStatus = 'pending' | 'searching' | 'processing' | 'stacking' | 'complete' | 'error'

export interface FieldResponse {
  key: string
  status: FieldStatus
  error: string | null
  target: { ra: number; dec: number }
  grid: { n: number; pixscale_arcsec: number; fov_arcmin: number; wcs: Record<string, unknown> }
  collections: string[]
  total: number
  done: number
  failed: Record<string, string>
  seasons: Season[]
  elapsed_s: number
  frames: FrameMeta[]
  stacks?: StackMeta[]
  stack_version?: string | null
  analysis?: Analysis | Record<string, never>
  /** ISO time the exposure list was last obtained from the archive. */
  last_checked?: string | null
  /** Exposures found by the latest `update=1` check (null when none was made this session). */
  new_frames?: number | null
}

export function hasAnalysis(f: FieldResponse): f is FieldResponse & { analysis: Analysis } {
  return !!f.analysis && 'bins' in f.analysis
}

export interface Resolved {
  name: string
  ra: number
  dec: number
  source: string
  otype: string | null
  pm_ra_masyr: number | null
  pm_dec_masyr: number | null
}

export type Scale = 'auto' | 'noise' | 'detector' | 'frame'

export interface RenderOpts {
  stretch: 'linear' | 'asinh' | 'log' | 'sqrt'
  /** auto: stacks share one sigma-based stretch per detector (so seasons are
   *  comparable), exposures use their own noise. noise: limits in units of each
   *  image's noise (sigLo..sigHi). detector: stacks -> sigma shared by all seasons
   *  of a detector; exposures -> percentiles shared by the detector. frame:
   *  percentiles (lo..hi) of each image. */
  scale: Scale
  lo: number
  hi: number
  sigLo: number
  sigHi: number
}

export const DEFAULT_RENDER: RenderOpts = { stretch: 'asinh', scale: 'auto', lo: 1, hi: 99.5, sigLo: -2, sigHi: 30 }

export function effectiveScale(scale: Scale, kind: 'frame' | 'stack'): Exclude<Scale, 'auto'> {
  if (scale !== 'auto') return scale
  return kind === 'stack' ? 'detector' : 'noise'
}

/** Whether the sigma (true) or percentile (false) limits apply. */
export function usesSigma(scale: Scale, kind: 'frame' | 'stack'): boolean {
  const s = effectiveScale(scale, kind)
  return s === 'noise' || (s === 'detector' && kind === 'stack')
}

export function renderQuery(opts: RenderOpts, kind: 'frame' | 'stack'): string {
  const scale = effectiveScale(opts.scale, kind)
  const q = new URLSearchParams({ stretch: opts.stretch, scale })
  if (usesSigma(opts.scale, kind)) {
    q.set('sig_lo', String(opts.sigLo))
    q.set('sig_hi', String(opts.sigHi))
  } else {
    q.set('lo', String(opts.lo))
    q.set('hi', String(opts.hi))
  }
  return q.toString()
}

/** Absolute backend origin for direct (CORS) deployments, from the build-time VITE_API_BASE.
 *  Empty (the default) = same origin: `/api` is proxied by Vite in dev and by the Pages Function in production. */
export const API_BASE = ((import.meta.env.VITE_API_BASE as string | undefined) ?? '').trim().replace(/\/+$/, '')

/** Every backend URL (JSON and images) goes through here. */
export const apiUrl = (path: string): string => (API_BASE && path.startsWith('/api/') ? API_BASE + path : path)

/** `fetch` for backend paths. */
export const apiFetch = (path: string, init?: RequestInit): Promise<Response> => fetch(apiUrl(path), init)

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await apiFetch(url, { signal })
  if (!r.ok) {
    let detail = r.statusText
    try {
      detail = (await r.json()).detail ?? detail
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return r.json() as Promise<T>
}

export function fetchField(ra: number, dec: number, fov: number, signal?: AbortSignal) {
  const q = new URLSearchParams({ ra: ra.toFixed(6), dec: dec.toFixed(6), fov: String(fov) })
  return getJson<FieldResponse>(`/api/frames?${q}`, signal)
}

export function resolveName(name: string, signal?: AbortSignal) {
  return getJson<Resolved>(`/api/resolve?${new URLSearchParams({ name })}`, signal)
}

// ---- Source fingerprint (spectrum + light curve) -------------------------------------------

export type FingerprintQuality = 'good' | 'suspect' | 'bad'

export interface FingerprintSource {
  id: string
  role: 'target' | 'neighbour'
  ra: number
  dec: number
  pmra_masyr: number | null
  pmdec_masyr: number | null
  sep_arcsec: number
  /** Gaia ids exceed 2^53, so the backend may send a string. */
  gaia_id: string | number | null
  g_mag: number | null
  variable?: boolean | null
}

export interface FingerprintNeighbourFlux {
  flux_ujy: number | null
  flux_err_ujy: number | null
}

/** One measurement per exposure (fit flux plus the naive aperture flux for comparison). */
export interface FingerprintPoint {
  frame_id: string
  mjd: number
  date: string | null
  season: number
  detector: number
  wavelength_um: number | null
  bandwidth_um: number | null
  channel: number | null
  flux_ujy: number | null
  flux_err_ujy: number | null
  aperture_flux_ujy: number | null
  aperture_err_ujy: number | null
  chi2_red: number | null
  n_pix: number | null
  flags: string[]
  quality: FingerprintQuality
  neighbours: Record<string, FingerprintNeighbourFlux>
}

export interface FingerprintSeasonFlux {
  flux_ujy: number | null
  flux_err_ujy: number | null
  n: number
}

/** One spectral channel, combined over exposures. */
export interface FingerprintChannel {
  channel: number
  wavelength_um: number
  detector: number
  flux_ujy: number | null
  flux_err_ujy: number | null
  aperture_flux_ujy: number | null
  aperture_err_ujy: number | null
  n: number
  seasons: Record<string, FingerprintSeasonFlux>
}

export interface FingerprintNeighbourChannel {
  channel: number
  wavelength_um: number
  flux_ujy: number | null
  flux_err_ujy: number | null
  n: number
}

export interface FingerprintLightCurvePoint {
  season: number
  mjd: number
  flux_ujy: number | null
  flux_err_ujy: number | null
  n: number
}

export interface FingerprintLightCurve {
  bin: string
  detector: number
  lam_lo_um: number
  lam_hi_um: number
  chi2_const: number | null
  dof: number | null
  variable: boolean
  points: FingerprintLightCurvePoint[]
}

export interface FingerprintMethod {
  psf?: string
  background?: string
  fit_box_px?: number
  neighbour_radius_arcsec?: number
  notes?: string[]
}

export type ClassificationConfidence = 'high' | 'medium' | 'low'

export interface SpectralIndex {
  /** Flux ratio (band / continuum, or numerator / denominator for colours); null when not measurable. */
  value: number | null
  err: number | null
  /** Absorption: significance of the depth (1 - value) / err. Colour: value / err. */
  sigma: number | null
  kind: 'absorption' | 'colour'
  feature: string
  band_um: [number, number]
  continuum_um: [number, number][]
  note: string | null
}

export interface FingerprintClassification {
  label: string
  /** Outcome of fixed rules, not a probability. */
  confidence: ClassificationConfidence
  reasons: string[]
  indices: Record<string, SpectralIndex>
  caveats: string[]
  confidence_note: string
  version: string
}

export interface FingerprintComplete {
  status: 'complete'
  field: string
  version: string
  target: {
    ra: number
    dec: number
    pmra_masyr: number | null
    pmdec_masyr: number | null
    epoch_mjd: number | null
  }
  sources: FingerprintSource[]
  blend_warning: boolean
  blend_note: string
  catalog: { name: string; ok: boolean; note: string | null }
  points: FingerprintPoint[]
  spectrum: FingerprintChannel[]
  neighbour_spectra: Record<string, FingerprintNeighbourChannel[]>
  /** Absent in results cached before fingerprint version 2. */
  classification?: FingerprintClassification
  light_curves: FingerprintLightCurve[]
  method: FingerprintMethod
  units: { flux: string; wavelength: string; time: string }
}

export interface FingerprintProcessing {
  status: 'processing'
  stage: string
  done: number
  total: number
}

export interface FingerprintFailed {
  status: 'error'
  error: string
}

export type FingerprintResponse = FingerprintComplete | FingerprintProcessing | FingerprintFailed

export interface FingerprintRequest {
  ra: number
  dec: number
  fov: number
  /** Proper motion in mas/yr at epochMjd (same shape as Target.pm). */
  pm?: { pmra: number; pmdec: number; epochMjd: number }
}

/** Query string for /api/fingerprint; also serves as the hook's change key. */
export function fingerprintQuery(r: FingerprintRequest): string {
  const q = new URLSearchParams({ ra: r.ra.toFixed(6), dec: r.dec.toFixed(6), fov: String(r.fov) })
  if (r.pm) {
    q.set('pmra', String(r.pm.pmra))
    q.set('pmdec', String(r.pm.pmdec))
    q.set('epoch', String(r.pm.epochMjd))
  }
  return q.toString()
}

export function fetchFingerprint(query: string, signal?: AbortSignal) {
  return getJson<FingerprintResponse>(`/api/fingerprint?${query}`, signal)
}

export function fetchOverlay(key: string, signal?: AbortSignal) {
  return getJson<OverlayResponse>(`/api/overlay?${new URLSearchParams({ key })}`, signal)
}
